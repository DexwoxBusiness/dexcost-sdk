"""Durable, atomic client for DexCost catalog releases.

The control plane publishes one immutable manifest that binds every SDK catalog
family.  This module downloads and validates the complete release before one
SQLite transaction makes it active. Provider calls never perform catalog I/O.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import urllib.error
import urllib.request
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode, urljoin, urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from dexcost._user_agent import sdk_user_agent
from dexcost.service_catalog import SUPPORTED_SAFETY_POLICY_VERSION, ServiceCatalog

_LOG = logging.getLogger(__name__)

CATALOG_SDK_CONTRACT_VERSION = 1
CATALOG_MANIFEST_MAX_BYTES = 256 * 1024
CATALOG_ARTIFACT_MAX_BYTES = 5 * 1024 * 1024
CATALOG_RELEASE_MAX_BYTES = 20 * 1024 * 1024
CATALOG_OVERLAY_MAX_BYTES = 5 * 1024 * 1024
CATALOG_BUNDLE_MAX_BYTES = 32 * 1024 * 1024
CATALOG_SIGNATURE_DOMAIN = b"dexcost.catalog-release.v1\0"
CATALOG_KINDS = (
    "observer_rules",
    "llm_prices",
    "service_prices",
    "compute_prices",
    "gpu_prices",
    "egress_prices",
    "server_pricing_reference",
)

CatalogChannel = Literal["stable", "canary"]
CatalogRefreshStatus = Literal["activated", "not_modified", "failed"]
CatalogTrustedKeys = Mapping[str, str | bytes]

_RELEASE_ID = re.compile(r"^catalog-release-[a-z0-9][a-z0-9._:-]{0,127}$")
_VERSION = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_SCHEMA_VERSION = re.compile(r"^[1-9][0-9]{0,8}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACTIVATION_ID = re.compile(r"^[1-9][0-9]*$")
_DECIMAL_RATE = re.compile(r"^(0|[1-9][0-9]{0,17})(\.[0-9]{1,18})?$")
_OVERLAY_UNITS = {
    "compute": {
        "request",
        "execution",
        "gb_second",
        "gib_second",
        "vcpu_second",
        "active_cpu_hour",
        "memory_gb_hour",
        "invocation",
        "vcpu_hour",
    },
    "gpu": {"gpu_second", "gpu_hour", "instance_hour", "vgpu_hour"},
    "egress": {"gb_egress", "gb_transferred"},
}


class CatalogError(RuntimeError):
    """Base class for catalog release failures."""


class CatalogValidationError(CatalogError):
    """Raised when a manifest or artifact violates the SDK contract."""


class CatalogDowngradeError(CatalogValidationError):
    """Raised when a server attempts to activate an older release sequence."""


@dataclass(frozen=True)
class CatalogSdkContract:
    minimum: int
    maximum: int


@dataclass(frozen=True)
class CatalogArtifactDescriptor:
    kind: str
    schema_version: str
    sha256: str
    byte_size: int
    item_count: int
    media_type: str
    path: str
    sdk_contract: CatalogSdkContract


@dataclass(frozen=True)
class CatalogManifest:
    schema_version: str
    release_id: str
    release_sequence: int
    channel: CatalogChannel
    published_at: datetime
    expires_at: datetime
    safety_policy_version: str
    sdk_contract: CatalogSdkContract
    server_pricing_catalog_version: str
    server_pricing_activation_id: str
    artifacts: dict[str, CatalogArtifactDescriptor]
    signatures: tuple[dict[str, str], ...]
    raw: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()

    def is_expired(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        return self.expires_at <= current


@dataclass(frozen=True)
class CatalogSnapshot:
    manifest: CatalogManifest
    artifacts: dict[str, Any]
    source: Literal["active", "previous"]
    stale: bool


@dataclass(frozen=True)
class CatalogRefreshResult:
    status: CatalogRefreshStatus
    snapshot: CatalogSnapshot | None
    error: str | None = None


@dataclass(frozen=True)
class WorkspaceRateOverride:
    kind: Literal["service", "compute", "gpu", "egress"]
    key: str
    rate_usd: Decimal
    per: str
    notes: str | None
    updated_at: datetime


@dataclass(frozen=True)
class CatalogWorkspaceOverlay:
    base_release_id: str
    base_release_sequence: int
    generated_at: datetime
    overrides: tuple[WorkspaceRateOverride, ...]
    raw: bytes


@dataclass(frozen=True)
class CatalogOverlayRefreshResult:
    status: CatalogRefreshStatus
    overlay: CatalogWorkspaceOverlay | None
    error: str | None = None


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _exact_object(value: Any, keys: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise CatalogValidationError(f"{name} must contain exactly {sorted(keys)}")
    return value


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CatalogValidationError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise CatalogValidationError(f"{name} is outside the supported range")
    return int(value)


def _datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise CatalogValidationError(f"{name} must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CatalogValidationError(f"{name} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CatalogValidationError(f"{name} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _sdk_contract(value: Any, name: str) -> CatalogSdkContract:
    raw = _exact_object(value, {"min", "max"}, name)
    minimum = _integer(raw["min"], f"{name}.min", 1, 10_000)
    maximum = _integer(raw["max"], f"{name}.max", 1, 10_000)
    if maximum < minimum:
        raise CatalogValidationError(f"{name}.max must be at least min")
    return CatalogSdkContract(minimum, maximum)


def parse_catalog_manifest(raw: bytes) -> CatalogManifest:
    """Parse and strictly validate one manifest without mutating SDK state."""
    if len(raw) > CATALOG_MANIFEST_MAX_BYTES:
        raise CatalogValidationError("catalog manifest exceeds the 256 KiB limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogValidationError("catalog manifest is not valid UTF-8 JSON") from exc
    top = _exact_object(
        payload,
        {
            "schema_version",
            "release_id",
            "release_sequence",
            "channel",
            "published_at",
            "expires_at",
            "safety_policy_version",
            "sdk_contract",
            "server_pricing_reference",
            "artifacts",
            "signatures",
        },
        "catalog manifest",
    )
    if top["schema_version"] != "1":
        raise CatalogValidationError("unsupported catalog manifest schema version")
    release_id = top["release_id"]
    if not isinstance(release_id, str) or _RELEASE_ID.fullmatch(release_id) is None:
        raise CatalogValidationError("catalog release_id is invalid")
    sequence = _integer(top["release_sequence"], "release_sequence", 1, 2**53 - 1)
    channel = top["channel"]
    if channel not in {"stable", "canary"}:
        raise CatalogValidationError("catalog channel is invalid")
    published_at = _datetime(top["published_at"], "published_at")
    expires_at = _datetime(top["expires_at"], "expires_at")
    if expires_at <= published_at:
        raise CatalogValidationError("catalog expires_at must be after published_at")
    safety_policy = top["safety_policy_version"]
    if not isinstance(safety_policy, str) or _VERSION.fullmatch(safety_policy) is None:
        raise CatalogValidationError("catalog safety_policy_version is invalid")
    if safety_policy != SUPPORTED_SAFETY_POLICY_VERSION:
        raise CatalogValidationError(f"unsupported catalog safety policy {safety_policy!r}")
    contract = _sdk_contract(top["sdk_contract"], "sdk_contract")
    if not contract.minimum <= CATALOG_SDK_CONTRACT_VERSION <= contract.maximum:
        raise CatalogValidationError("active release does not support SDK catalog contract 1")

    server_reference = _exact_object(
        top["server_pricing_reference"],
        {"catalog_version", "activation_id"},
        "server_pricing_reference",
    )
    catalog_version = server_reference["catalog_version"]
    activation_id = server_reference["activation_id"]
    if not isinstance(catalog_version, str) or not 1 <= len(catalog_version) <= 128:
        raise CatalogValidationError("server pricing catalog_version is invalid")
    if not isinstance(activation_id, str) or _ACTIVATION_ID.fullmatch(activation_id) is None:
        raise CatalogValidationError("server pricing activation_id is invalid")

    artifact_map = _exact_object(top["artifacts"], set(CATALOG_KINDS), "artifacts")
    artifacts: dict[str, CatalogArtifactDescriptor] = {}
    release_bytes = 0
    for kind in CATALOG_KINDS:
        descriptor = _exact_object(
            artifact_map[kind],
            {
                "kind",
                "schema_version",
                "sha256",
                "byte_size",
                "item_count",
                "media_type",
                "path",
                "sdk_contract",
            },
            f"artifacts.{kind}",
        )
        schema_version = descriptor["schema_version"]
        sha256 = descriptor["sha256"]
        byte_size = _integer(
            descriptor["byte_size"], f"artifacts.{kind}.byte_size", 2, CATALOG_ARTIFACT_MAX_BYTES
        )
        item_count = _integer(
            descriptor["item_count"], f"artifacts.{kind}.item_count", 0, 1_000_000
        )
        path = descriptor["path"]
        if descriptor["kind"] != kind:
            raise CatalogValidationError(f"artifact descriptor kind must equal {kind}")
        if (
            not isinstance(schema_version, str)
            or _SCHEMA_VERSION.fullmatch(schema_version) is None
        ):
            raise CatalogValidationError(f"artifacts.{kind}.schema_version is invalid")
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            raise CatalogValidationError(f"artifacts.{kind}.sha256 is invalid")
        if descriptor["media_type"] != "application/json":
            raise CatalogValidationError(f"artifacts.{kind}.media_type is unsupported")
        expected_path = f"/v1/catalogs/artifacts/sha256/{sha256}"
        if path != expected_path:
            raise CatalogValidationError(f"artifacts.{kind}.path is not content addressed")
        artifact_contract = _sdk_contract(
            descriptor["sdk_contract"], f"artifacts.{kind}.sdk_contract"
        )
        if (
            not artifact_contract.minimum
            <= CATALOG_SDK_CONTRACT_VERSION
            <= artifact_contract.maximum
        ):
            raise CatalogValidationError(f"artifact {kind} does not support SDK contract 1")
        release_bytes += byte_size
        artifacts[kind] = CatalogArtifactDescriptor(
            kind=kind,
            schema_version=schema_version,
            sha256=sha256,
            byte_size=byte_size,
            item_count=item_count,
            media_type="application/json",
            path=path,
            sdk_contract=artifact_contract,
        )
    if release_bytes > CATALOG_RELEASE_MAX_BYTES:
        raise CatalogValidationError("catalog release exceeds the 20 MiB aggregate limit")

    raw_signatures = top["signatures"]
    if not isinstance(raw_signatures, list) or len(raw_signatures) > 8:
        raise CatalogValidationError("catalog signatures must be an array of at most 8 entries")
    signatures: list[dict[str, str]] = []
    for index, value in enumerate(raw_signatures):
        signature = _exact_object(
            value, {"algorithm", "key_id", "signature"}, f"signatures[{index}]"
        )
        if signature["algorithm"] != "ed25519":
            raise CatalogValidationError("catalog signature algorithm is unsupported")
        if (
            not isinstance(signature["key_id"], str)
            or _VERSION.fullmatch(signature["key_id"]) is None
        ):
            raise CatalogValidationError("catalog signature key_id is invalid")
        encoded = signature["signature"]
        if not isinstance(encoded, str) or re.fullmatch(r"[A-Za-z0-9_-]+", encoded) is None:
            raise CatalogValidationError("catalog signature encoding is invalid")
        signatures.append(signature)

    return CatalogManifest(
        schema_version="1",
        release_id=release_id,
        release_sequence=sequence,
        channel=channel,
        published_at=published_at,
        expires_at=expires_at,
        safety_policy_version=safety_policy,
        sdk_contract=contract,
        server_pricing_catalog_version=catalog_version,
        server_pricing_activation_id=activation_id,
        artifacts=artifacts,
        signatures=tuple(signatures),
        raw=raw,
    )


def catalog_manifest_signing_payload(manifest: CatalogManifest) -> bytes:
    """Return the domain-separated canonical bytes covered by Ed25519."""
    try:
        payload = json.loads(manifest.raw.decode("utf-8"))
        payload["signatures"] = []
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CatalogValidationError(
            "catalog manifest cannot be canonicalized for signing"
        ) from exc
    return CATALOG_SIGNATURE_DOMAIN + canonical


def _base64url_decode(value: str, *, name: str, size: int) -> bytes:
    if re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise CatalogValidationError(f"{name} is not unpadded base64url")
    try:
        decoded = urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise CatalogValidationError(f"{name} is not valid base64url") from exc
    if len(decoded) != size:
        raise CatalogValidationError(f"{name} has the wrong byte length")
    return decoded


def _trusted_public_key(key_id: str, value: str | bytes) -> Ed25519PublicKey:
    if _VERSION.fullmatch(key_id) is None:
        raise CatalogValidationError("catalog trusted key ID is invalid")
    raw = (
        _base64url_decode(value, name=f"catalog trusted key {key_id}", size=32)
        if isinstance(value, str)
        else bytes(value)
    )
    if len(raw) != 32:
        raise CatalogValidationError(f"catalog trusted key {key_id} has the wrong byte length")
    return Ed25519PublicKey.from_public_bytes(raw)


def verify_catalog_manifest_signature(
    manifest: CatalogManifest,
    trusted_keys: CatalogTrustedKeys | None,
    *,
    require_signature: bool,
) -> str | None:
    """Verify at least one signature from a trusted rotated key.

    Signed manifests are never silently treated as unsigned.  During the
    migration window an unsigned manifest may be accepted only when the caller
    explicitly leaves ``require_signature`` false.
    """
    if not manifest.signatures:
        if require_signature:
            raise CatalogValidationError("catalog manifest requires a trusted signature")
        return None
    keys = dict(trusted_keys or {})
    if not keys:
        raise CatalogValidationError("signed catalog manifest has no configured trusted keys")
    parsed_keys = {key_id: _trusted_public_key(key_id, value) for key_id, value in keys.items()}
    payload = catalog_manifest_signing_payload(manifest)
    matched_key = False
    for entry in manifest.signatures:
        key = parsed_keys.get(entry["key_id"])
        if key is None:
            continue
        matched_key = True
        signature = _base64url_decode(
            entry["signature"],
            name=f"catalog signature {entry['key_id']}",
            size=64,
        )
        try:
            key.verify(signature, payload)
        except InvalidSignature:
            continue
        return entry["key_id"]
    if not matched_key:
        raise CatalogValidationError("catalog manifest is not signed by a configured trusted key")
    raise CatalogValidationError("catalog manifest signature verification failed")


def _bundle_decode(value: Any, *, name: str, maximum: int) -> bytes:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise CatalogValidationError(f"{name} is not unpadded base64url")
    try:
        decoded = urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise CatalogValidationError(f"{name} is not valid base64url") from exc
    if len(decoded) > maximum:
        raise CatalogValidationError(f"{name} exceeds its byte limit")
    return decoded


def encode_catalog_bundle(
    manifest: CatalogManifest,
    artifact_payloads: Mapping[str, bytes],
) -> bytes:
    """Encode exact validated release bytes for authenticated offline import."""
    if set(artifact_payloads) != set(CATALOG_KINDS):
        raise CatalogValidationError("catalog bundle requires every artifact kind")
    for kind, descriptor in manifest.artifacts.items():
        validate_catalog_artifact(manifest, descriptor, artifact_payloads[kind])
    bundle = {
        "schema_version": "1",
        "manifest_base64url": urlsafe_b64encode(manifest.raw).rstrip(b"=").decode(),
        "artifacts_base64url": {
            kind: urlsafe_b64encode(artifact_payloads[kind]).rstrip(b"=").decode()
            for kind in CATALOG_KINDS
        },
    }
    raw = json.dumps(
        bundle,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(raw) > CATALOG_BUNDLE_MAX_BYTES:
        raise CatalogValidationError("catalog bundle exceeds the 32 MiB limit")
    return raw


def parse_catalog_bundle(raw: bytes) -> tuple[CatalogManifest, dict[str, bytes]]:
    """Decode a bounded bundle without weakening normal activation checks."""
    if len(raw) > CATALOG_BUNDLE_MAX_BYTES:
        raise CatalogValidationError("catalog bundle exceeds the 32 MiB limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogValidationError("catalog bundle is not valid UTF-8 JSON") from exc
    top = _exact_object(
        value,
        {"schema_version", "manifest_base64url", "artifacts_base64url"},
        "catalog bundle",
    )
    if top["schema_version"] != "1":
        raise CatalogValidationError("unsupported catalog bundle schema version")
    manifest_raw = _bundle_decode(
        top["manifest_base64url"],
        name="catalog bundle manifest",
        maximum=CATALOG_MANIFEST_MAX_BYTES,
    )
    manifest = parse_catalog_manifest(manifest_raw)
    encoded_artifacts = _exact_object(
        top["artifacts_base64url"],
        set(CATALOG_KINDS),
        "catalog bundle artifacts",
    )
    artifacts = {
        kind: _bundle_decode(
            encoded_artifacts[kind],
            name=f"catalog bundle artifact {kind}",
            maximum=manifest.artifacts[kind].byte_size,
        )
        for kind in CATALOG_KINDS
    }
    for kind, descriptor in manifest.artifacts.items():
        validate_catalog_artifact(manifest, descriptor, artifacts[kind])
    return manifest, artifacts


def parse_catalog_overlay(
    raw: bytes,
    manifest: CatalogManifest,
) -> CatalogWorkspaceOverlay:
    """Strictly validate one authenticated overlay bound to *manifest*."""
    if len(raw) > CATALOG_OVERLAY_MAX_BYTES:
        raise CatalogValidationError("catalog overlay exceeds the 5 MiB limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogValidationError("catalog overlay is not valid UTF-8 JSON") from exc
    top = _exact_object(
        payload,
        {
            "schema_version",
            "base_release_id",
            "base_release_sequence",
            "generated_at",
            "overrides",
        },
        "catalog overlay",
    )
    if top["schema_version"] != "1":
        raise CatalogValidationError("unsupported catalog overlay schema version")
    if (
        top["base_release_id"] != manifest.release_id
        or top["base_release_sequence"] != manifest.release_sequence
    ):
        raise CatalogValidationError("catalog overlay is not bound to the active release")
    generated_at = _datetime(top["generated_at"], "overlay.generated_at")
    raw_overrides = top["overrides"]
    if not isinstance(raw_overrides, list) or len(raw_overrides) > 100_000:
        raise CatalogValidationError("catalog overlay overrides are invalid")
    overrides: list[WorkspaceRateOverride] = []
    identities: set[tuple[str, str, str]] = set()
    for index, value in enumerate(raw_overrides):
        rate = _exact_object(
            value,
            {"kind", "key", "rate_usd", "per", "notes", "updated_at"},
            f"overlay.overrides[{index}]",
        )
        kind = rate["kind"]
        key = rate["key"]
        per = rate["per"]
        raw_rate = rate["rate_usd"]
        notes = rate["notes"]
        if kind not in {"service", "compute", "gpu", "egress"}:
            raise CatalogValidationError("catalog overlay kind is invalid")
        if not isinstance(key, str) or not 1 <= len(key) <= 512:
            raise CatalogValidationError("catalog overlay key is invalid")
        if (
            not isinstance(per, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", per) is None
            or (kind != "service" and per not in _OVERLAY_UNITS[kind])
        ):
            raise CatalogValidationError(f"unsupported {kind} overlay billing unit")
        if not isinstance(raw_rate, str) or _DECIMAL_RATE.fullmatch(raw_rate) is None:
            raise CatalogValidationError("catalog overlay rate_usd is invalid")
        amount = Decimal(raw_rate)
        if not amount.is_finite() or amount < 0:
            raise CatalogValidationError("catalog overlay rate_usd is unsafe")
        if notes is not None and (not isinstance(notes, str) or len(notes) > 1000):
            raise CatalogValidationError("catalog overlay notes are invalid")
        updated_at = _datetime(rate["updated_at"], "overlay override updated_at")
        if updated_at > generated_at:
            raise CatalogValidationError("catalog overlay generated_at precedes an override")
        identity = (kind, key, per)
        if identity in identities:
            raise CatalogValidationError("catalog overlay contains a duplicate component")
        identities.add(identity)
        overrides.append(
            WorkspaceRateOverride(
                kind=kind,
                key=key,
                rate_usd=amount,
                per=per,
                notes=notes,
                updated_at=updated_at,
            )
        )
    return CatalogWorkspaceOverlay(
        base_release_id=manifest.release_id,
        base_release_sequence=manifest.release_sequence,
        generated_at=generated_at,
        overrides=tuple(overrides),
        raw=raw,
    )


def _validate_money_tree(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise CatalogValidationError(f"catalog key at {path} must be a string")
            if isinstance(child, (int, float, str)) and any(
                token in key.lower() for token in ("usd", "cost", "rate")
            ):
                try:
                    amount = Decimal(str(child))
                except (InvalidOperation, ValueError) as exc:
                    raise CatalogValidationError(
                        f"catalog money at {path}.{key} is invalid"
                    ) from exc
                if not amount.is_finite() or amount < 0:
                    raise CatalogValidationError(f"catalog money at {path}.{key} is unsafe")
            _validate_money_tree(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_money_tree(child, f"{path}[{index}]")


def validate_catalog_artifact(
    manifest: CatalogManifest,
    descriptor: CatalogArtifactDescriptor,
    raw: bytes,
) -> Any:
    """Validate integrity plus the runtime shape of a release artifact."""
    if len(raw) != descriptor.byte_size:
        raise CatalogValidationError(
            f"artifact {descriptor.kind} byte size does not match manifest"
        )
    if hashlib.sha256(raw).hexdigest() != descriptor.sha256:
        raise CatalogValidationError(f"artifact {descriptor.kind} SHA-256 does not match manifest")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogValidationError(
            f"artifact {descriptor.kind} is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(data, dict):
        raise CatalogValidationError(f"artifact {descriptor.kind} must be a JSON object")

    kind = descriptor.kind
    if kind == "service_prices":
        entries = ServiceCatalog._parse_catalog_entries(data)
        if descriptor.item_count != len(entries):
            raise CatalogValidationError("service_prices item_count does not match catalog")
    elif kind == "observer_rules":
        from dexcost.service_usage_observers import ServiceUsageObservers

        observers = ServiceUsageObservers(data=data)
        if descriptor.item_count != observers.observer_count:
            raise CatalogValidationError("observer_rules item_count does not match catalog")
    elif kind == "llm_prices":
        entries = {
            key: value for key, value in data.items() if key not in {"_meta", "sample_spec"}
        }
        if not entries or not all(isinstance(value, dict) for value in entries.values()):
            raise CatalogValidationError("llm_prices must contain model objects")
        if descriptor.item_count != len(entries):
            raise CatalogValidationError("llm_prices item_count does not match catalog")
        _validate_money_tree(data)
    elif kind in {"compute_prices", "gpu_prices", "egress_prices"}:
        if not isinstance(data.get("_meta"), dict):
            raise CatalogValidationError(f"{kind} requires _meta")
        if not any(key != "_meta" and isinstance(value, dict) for key, value in data.items()):
            raise CatalogValidationError(f"{kind} contains no provider pricing")
        _validate_money_tree(data)
    elif kind == "server_pricing_reference":
        if (
            data.get("catalog_version") != manifest.server_pricing_catalog_version
            or str(data.get("activation_id")) != manifest.server_pricing_activation_id
        ):
            raise CatalogValidationError("server_pricing_reference does not match manifest")
    else:  # pragma: no cover - guarded by manifest parsing
        raise CatalogValidationError(f"unsupported catalog artifact kind {kind}")
    return data


class CatalogReleaseStore:
    """SQLite-backed last-known-good and previous catalog releases."""

    def __init__(
        self,
        db_path: str | Path | None,
        *,
        trusted_keys: CatalogTrustedKeys | None = None,
        require_signature: bool = False,
    ) -> None:
        from dexcost.storage.sqlite import SQLiteStorage

        bootstrap = SQLiteStorage(db_path=db_path)
        resolved_path = bootstrap._path
        bootstrap.close()
        self._conn = sqlite3.connect(str(resolved_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.Lock()
        self._trusted_keys = dict(trusted_keys or {})
        self._require_signature = require_signature
        for key_id, value in self._trusted_keys.items():
            _trusted_public_key(key_id, value)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def manifest_etag(self, channel: CatalogChannel) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT manifest_etag FROM sdk_catalog_state WHERE channel=?", (channel,)
            ).fetchone()
        return None if row is None else row[0]

    def artifact_bytes(self, sha256: str) -> bytes | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT byte_size, payload FROM sdk_catalog_artifacts WHERE sha256=?", (sha256,)
            ).fetchone()
        if row is None:
            return None
        payload = bytes(row["payload"])
        if len(payload) != row["byte_size"] or hashlib.sha256(payload).hexdigest() != sha256:
            raise CatalogValidationError(f"durable catalog artifact {sha256} is corrupt")
        return payload

    def _load_slot(self, channel: CatalogChannel, slot: str) -> CatalogSnapshot | None:
        if slot not in {"active_release_sequence", "previous_release_sequence"}:
            raise ValueError("invalid catalog state slot")
        with self._lock:
            row = self._conn.execute(
                f"""SELECT r.manifest_json
                    FROM sdk_catalog_state s
                    JOIN sdk_catalog_releases r ON r.release_sequence=s.{slot}
                    WHERE s.channel=?""",
                (channel,),
            ).fetchone()
        if row is None:
            return None
        manifest = parse_catalog_manifest(bytes(row["manifest_json"]))
        verify_catalog_manifest_signature(
            manifest,
            self._trusted_keys,
            require_signature=self._require_signature,
        )
        artifacts: dict[str, Any] = {}
        for kind, descriptor in manifest.artifacts.items():
            raw = self.artifact_bytes(descriptor.sha256)
            if raw is None:
                raise CatalogValidationError(f"durable catalog release is missing {kind}")
            artifacts[kind] = validate_catalog_artifact(manifest, descriptor, raw)
        source: Literal["active", "previous"] = (
            "active" if slot == "active_release_sequence" else "previous"
        )
        return CatalogSnapshot(
            manifest=manifest,
            artifacts=artifacts,
            source=source,
            stale=manifest.is_expired(),
        )

    def active(self, channel: CatalogChannel = "stable") -> CatalogSnapshot | None:
        return self._load_slot(channel, "active_release_sequence")

    def previous(self, channel: CatalogChannel = "stable") -> CatalogSnapshot | None:
        return self._load_slot(channel, "previous_release_sequence")

    def best_available(self, channel: CatalogChannel = "stable") -> CatalogSnapshot | None:
        try:
            active = self.active(channel)
        except CatalogValidationError:
            _LOG.warning("active durable catalog is corrupt; trying previous", exc_info=True)
        else:
            if active is not None:
                return active
        try:
            return self.previous(channel)
        except CatalogValidationError:
            _LOG.warning("previous durable catalog is corrupt", exc_info=True)
            return None

    def export_bundle(
        self,
        channel: CatalogChannel = "stable",
        *,
        source: Literal["active", "previous"] = "active",
    ) -> bytes:
        snapshot = self.active(channel) if source == "active" else self.previous(channel)
        if snapshot is None:
            raise CatalogValidationError(f"no {source} {channel} catalog release is available")
        artifacts: dict[str, bytes] = {}
        for kind, descriptor in snapshot.manifest.artifacts.items():
            raw = self.artifact_bytes(descriptor.sha256)
            if raw is None:  # pragma: no cover - snapshot validation already guarantees this
                raise CatalogValidationError(f"catalog bundle is missing {kind}")
            artifacts[kind] = raw
        return encode_catalog_bundle(snapshot.manifest, artifacts)

    def import_bundle(self, raw: bytes, etag: str | None = None) -> CatalogSnapshot:
        manifest, artifacts = parse_catalog_bundle(raw)
        return self.activate(manifest, artifacts, etag)

    def mark_checked(self, channel: CatalogChannel, etag: str | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """INSERT INTO sdk_catalog_state(
                       channel, manifest_etag, last_checked_at, last_error
                   )
                   VALUES (?, ?, ?, NULL)
                   ON CONFLICT(channel) DO UPDATE SET
                     manifest_etag=COALESCE(excluded.manifest_etag, manifest_etag),
                     last_checked_at=excluded.last_checked_at,
                     last_error=NULL""",
                (channel, etag, now),
            )
            self._conn.commit()

    def record_error(self, channel: CatalogChannel, error: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """INSERT INTO sdk_catalog_state(channel, last_checked_at, last_error)
                   VALUES (?, ?, ?)
                   ON CONFLICT(channel) DO UPDATE SET
                     last_checked_at=excluded.last_checked_at,
                     last_error=excluded.last_error""",
                (channel, now, error[:2048]),
            )
            self._conn.commit()

    def overlay_etag(self, principal_sha256: str, base_release_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT etag FROM sdk_catalog_overlays
                   WHERE principal_sha256=? AND base_release_id=?""",
                (principal_sha256, base_release_id),
            ).fetchone()
        return None if row is None else row[0]

    def load_overlay(
        self,
        principal_sha256: str,
        manifest: CatalogManifest,
    ) -> CatalogWorkspaceOverlay | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT payload, payload_sha256 FROM sdk_catalog_overlays
                   WHERE principal_sha256=? AND base_release_id=?""",
                (principal_sha256, manifest.release_id),
            ).fetchone()
        if row is None:
            return None
        raw = bytes(row["payload"])
        if hashlib.sha256(raw).hexdigest() != row["payload_sha256"]:
            raise CatalogValidationError("durable catalog overlay is corrupt")
        return parse_catalog_overlay(raw, manifest)

    def save_overlay(
        self,
        principal_sha256: str,
        overlay: CatalogWorkspaceOverlay,
        etag: str | None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """INSERT INTO sdk_catalog_overlays
                   (principal_sha256, base_release_id, base_release_sequence,
                    payload, payload_sha256, etag, generated_at, stored_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(principal_sha256, base_release_id) DO UPDATE SET
                     base_release_sequence=excluded.base_release_sequence,
                     payload=excluded.payload,
                     payload_sha256=excluded.payload_sha256,
                     etag=excluded.etag,
                     generated_at=excluded.generated_at,
                     stored_at=excluded.stored_at""",
                (
                    principal_sha256,
                    overlay.base_release_id,
                    overlay.base_release_sequence,
                    overlay.raw,
                    hashlib.sha256(overlay.raw).hexdigest(),
                    etag,
                    overlay.generated_at.isoformat(),
                    now,
                ),
            )
            self._conn.commit()

    def activate(
        self,
        manifest: CatalogManifest,
        artifact_payloads: dict[str, bytes],
        etag: str | None,
    ) -> CatalogSnapshot:
        if set(artifact_payloads) != set(CATALOG_KINDS):
            raise CatalogValidationError("catalog activation requires every artifact kind")
        verify_catalog_manifest_signature(
            manifest,
            self._trusted_keys,
            require_signature=self._require_signature,
        )
        if manifest.is_expired():
            raise CatalogValidationError("an expired catalog release cannot be activated")
        for kind, descriptor in manifest.artifacts.items():
            validate_catalog_artifact(manifest, descriptor, artifact_payloads[kind])
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                state = self._conn.execute(
                    "SELECT active_release_sequence FROM sdk_catalog_state WHERE channel=?",
                    (manifest.channel,),
                ).fetchone()
                active_sequence = None if state is None else state[0]
                if active_sequence is not None and manifest.release_sequence < active_sequence:
                    raise CatalogDowngradeError(
                        f"catalog release {manifest.release_sequence} is older than "
                        f"active {active_sequence}"
                    )
                existing = self._conn.execute(
                    "SELECT manifest_sha256 FROM sdk_catalog_releases WHERE release_sequence=?",
                    (manifest.release_sequence,),
                ).fetchone()
                if existing is not None and existing[0] != manifest.sha256:
                    raise CatalogValidationError(
                        "release sequence was reused with different content"
                    )

                for kind, descriptor in manifest.artifacts.items():
                    payload = artifact_payloads[kind]
                    self._conn.execute(
                        """INSERT OR IGNORE INTO sdk_catalog_artifacts
                           (sha256, kind, schema_version, byte_size, payload, validated_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            descriptor.sha256,
                            kind,
                            descriptor.schema_version,
                            descriptor.byte_size,
                            payload,
                            now,
                        ),
                    )
                    stored = self._conn.execute(
                        "SELECT byte_size, payload FROM sdk_catalog_artifacts WHERE sha256=?",
                        (descriptor.sha256,),
                    ).fetchone()
                    if stored is None or stored[0] != len(payload) or bytes(stored[1]) != payload:
                        raise CatalogValidationError(
                            "content-addressed artifact collision detected"
                        )

                self._conn.execute(
                    """INSERT OR IGNORE INTO sdk_catalog_releases
                       (release_sequence, release_id, channel, manifest_json, manifest_sha256,
                        published_at, expires_at, safety_policy_version, stored_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        manifest.release_sequence,
                        manifest.release_id,
                        manifest.channel,
                        manifest.raw,
                        manifest.sha256,
                        manifest.published_at.isoformat(),
                        manifest.expires_at.isoformat(),
                        manifest.safety_policy_version,
                        now,
                    ),
                )
                for kind, descriptor in manifest.artifacts.items():
                    self._conn.execute(
                        """INSERT OR IGNORE INTO sdk_catalog_release_artifacts
                           (release_sequence, kind, sha256) VALUES (?, ?, ?)""",
                        (manifest.release_sequence, kind, descriptor.sha256),
                    )

                previous = (
                    active_sequence if active_sequence != manifest.release_sequence else None
                )
                self._conn.execute(
                    """INSERT INTO sdk_catalog_state
                       (channel, active_release_sequence, previous_release_sequence,
                        manifest_etag, last_checked_at, last_error)
                       VALUES (?, ?, ?, ?, ?, NULL)
                       ON CONFLICT(channel) DO UPDATE SET
                         previous_release_sequence=CASE
                           WHEN sdk_catalog_state.active_release_sequence =
                                excluded.active_release_sequence
                           THEN sdk_catalog_state.previous_release_sequence
                           ELSE sdk_catalog_state.active_release_sequence
                         END,
                         active_release_sequence=excluded.active_release_sequence,
                         manifest_etag=excluded.manifest_etag,
                         last_checked_at=excluded.last_checked_at,
                         last_error=NULL""",
                    (manifest.channel, manifest.release_sequence, previous, etag, now),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        snapshot = self.active(manifest.channel)
        if snapshot is None:  # pragma: no cover - transaction guarantees this
            raise CatalogError("catalog activation completed without an active release")
        return snapshot


class CatalogReleaseClient:
    """Fetch complete catalog releases and activate them in the durable store."""

    def __init__(
        self,
        endpoint: str,
        store: CatalogReleaseStore,
        *,
        channel: CatalogChannel = "stable",
        timeout_seconds: float = 10.0,
    ) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("catalog endpoint must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("catalog endpoint must not contain credentials, query, or fragment")
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError("catalog timeout must be greater than 0 and at most 60 seconds")
        self._endpoint = endpoint.rstrip("/")
        self._origin = (parsed.scheme.lower(), parsed.hostname.lower(), parsed.port)
        self._store = store
        self._channel = channel
        self._timeout_seconds = timeout_seconds
        self._opener = urllib.request.build_opener(_NoRedirects())

    def _request(self, url: str, headers: dict[str, str]) -> Any:
        request = urllib.request.Request(url, headers=headers)
        return self._opener.open(request, timeout=self._timeout_seconds)

    @staticmethod
    def _read_bounded(response: Any, limit: int) -> bytes:
        content_encoding = response.headers.get("Content-Encoding")
        if content_encoding not in {None, "", "identity"}:
            raise CatalogValidationError("compressed catalog responses are not supported")
        raw_length = response.headers.get("Content-Length")
        if raw_length is not None:
            try:
                content_length = int(raw_length)
            except ValueError as exc:
                raise CatalogValidationError("catalog Content-Length is invalid") from exc
            if content_length < 0 or content_length > limit:
                raise CatalogValidationError("catalog response exceeds its byte limit")
        body = bytes(response.read(limit + 1))
        if len(body) > limit:
            raise CatalogValidationError("catalog response exceeds its byte limit")
        return body

    def _artifact_url(self, path: str) -> str:
        url = urljoin(f"{self._endpoint}/", path)
        parsed = urlparse(url)
        origin = (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port)
        if origin != self._origin or parsed.query or parsed.fragment:
            raise CatalogValidationError("catalog artifact URL crossed the configured origin")
        return url

    def refresh(self) -> CatalogRefreshResult:
        """Perform one fail-safe refresh; never replace a valid release on error."""
        query = urlencode({"channel": self._channel, "sdk_contract": CATALOG_SDK_CONTRACT_VERSION})
        manifest_url = f"{self._endpoint}/v1/catalogs/manifest?{query}"
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": sdk_user_agent(),
        }
        etag = self._store.manifest_etag(self._channel)
        if etag:
            headers["If-None-Match"] = etag
        try:
            try:
                response_context = self._request(manifest_url, headers)
            except urllib.error.HTTPError as exc:
                if exc.code == 304:
                    self._store.mark_checked(self._channel, etag)
                    snapshot = self._store.best_available(self._channel)
                    if snapshot is None:
                        raise CatalogValidationError(
                            "server returned 304 without a durable release"
                        ) from None
                    return CatalogRefreshResult("not_modified", snapshot)
                raise
            with response_context as response:
                content_type = response.headers.get_content_type()
                if content_type != "application/json":
                    raise CatalogValidationError(
                        "catalog manifest Content-Type is not application/json"
                    )
                manifest_raw = self._read_bounded(response, CATALOG_MANIFEST_MAX_BYTES)
                response_etag = response.headers.get("ETag")
            manifest = parse_catalog_manifest(manifest_raw)
            if manifest.channel != self._channel:
                raise CatalogValidationError("catalog manifest channel does not match request")
            if manifest.is_expired():
                raise CatalogValidationError("catalog manifest is already expired")
            active = self._store.active(self._channel)
            if active is not None:
                if manifest.release_sequence < active.manifest.release_sequence:
                    raise CatalogDowngradeError("catalog server attempted a release downgrade")
                if manifest.release_sequence == active.manifest.release_sequence:
                    if manifest.sha256 != active.manifest.sha256:
                        raise CatalogValidationError(
                            "active release sequence changed without increasing"
                        )
                    self._store.mark_checked(self._channel, response_etag)
                    return CatalogRefreshResult("not_modified", active)

            artifact_payloads: dict[str, bytes] = {}
            total_bytes = 0
            for kind, descriptor in manifest.artifacts.items():
                raw = self._store.artifact_bytes(descriptor.sha256)
                if raw is None:
                    artifact_headers = {
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                        "User-Agent": sdk_user_agent(),
                    }
                    with self._request(
                        self._artifact_url(descriptor.path), artifact_headers
                    ) as response:
                        if response.headers.get_content_type() != "application/json":
                            raise CatalogValidationError(
                                f"artifact {kind} Content-Type is not application/json"
                            )
                        raw = self._read_bounded(response, descriptor.byte_size)
                validate_catalog_artifact(manifest, descriptor, raw)
                artifact_payloads[kind] = raw
                total_bytes += len(raw)
            if total_bytes > CATALOG_RELEASE_MAX_BYTES:
                raise CatalogValidationError("catalog release exceeds aggregate byte limit")
            snapshot = self._store.activate(manifest, artifact_payloads, response_etag)
            return CatalogRefreshResult("activated", snapshot)
        except Exception as exc:
            self._store.record_error(self._channel, str(exc))
            _LOG.warning("catalog release refresh failed; keeping last-known-good", exc_info=True)
            return CatalogRefreshResult(
                "failed", self._store.best_available(self._channel), str(exc)
            )


class CatalogOverlayClient:
    """Fetch and durably cache the private overlay for one API-key principal."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        store: CatalogReleaseStore,
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("catalog endpoint must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("catalog endpoint must not contain credentials, query, or fragment")
        if not api_key:
            raise ValueError("catalog overlay requires an API key")
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError("catalog timeout must be greater than 0 and at most 60 seconds")
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._principal_sha256 = hashlib.sha256(api_key.encode()).hexdigest()
        self._store = store
        self._timeout_seconds = timeout_seconds
        self._opener = urllib.request.build_opener(_NoRedirects())

    def cached(self, manifest: CatalogManifest) -> CatalogWorkspaceOverlay | None:
        return self._store.load_overlay(self._principal_sha256, manifest)

    def refresh(self, manifest: CatalogManifest) -> CatalogOverlayRefreshResult:
        query = urlencode({"base_release_id": manifest.release_id})
        url = f"{self._endpoint}/v1/api/catalogs/overlay?{query}"
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": sdk_user_agent(),
        }
        etag = self._store.overlay_etag(self._principal_sha256, manifest.release_id)
        if etag:
            headers["If-None-Match"] = etag
        request = urllib.request.Request(url, headers=headers)
        try:
            try:
                response_context = self._opener.open(
                    request, timeout=self._timeout_seconds
                )
            except urllib.error.HTTPError as exc:
                if exc.code == 304:
                    overlay = self.cached(manifest)
                    if overlay is None:
                        raise CatalogValidationError(
                            "server returned 304 without a durable overlay"
                        ) from None
                    return CatalogOverlayRefreshResult("not_modified", overlay)
                raise
            with response_context as response:
                if response.headers.get_content_type() != "application/json":
                    raise CatalogValidationError(
                        "catalog overlay Content-Type is not application/json"
                    )
                raw = CatalogReleaseClient._read_bounded(
                    response, CATALOG_OVERLAY_MAX_BYTES
                )
                response_etag = response.headers.get("ETag")
            overlay = parse_catalog_overlay(raw, manifest)
            self._store.save_overlay(self._principal_sha256, overlay, response_etag)
            return CatalogOverlayRefreshResult("activated", overlay)
        except Exception as exc:
            _LOG.warning(
                "workspace catalog overlay refresh failed; keeping cached overlay",
                exc_info=True,
            )
            try:
                cached = self.cached(manifest)
            except CatalogValidationError:
                cached = None
            return CatalogOverlayRefreshResult("failed", cached, str(exc))
