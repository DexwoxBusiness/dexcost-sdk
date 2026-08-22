"""Atomic server-backed catalog release tests."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
from base64 import urlsafe_b64encode
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.message import Message
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from dexcost.catalog_releases import (
    CATALOG_KINDS,
    CatalogOverlayClient,
    CatalogReleaseClient,
    CatalogReleaseStore,
    CatalogValidationError,
    catalog_manifest_signing_payload,
    encode_catalog_bundle,
    parse_catalog_manifest,
    parse_catalog_overlay,
)
from dexcost.catalog_runtime import CatalogRuntime
from dexcost.service_catalog import SUPPORTED_SAFETY_POLICY_VERSION
from dexcost.storage.migrations import TARGET_SCHEMA_VERSION
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker

_DATA = Path(__file__).resolve().parents[1] / "src" / "dexcost" / "data"
_TEST_SEED = bytes.fromhex(
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
)
_TEST_PUBLIC_KEY = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
_TEST_PUBLIC_KEY_B64 = urlsafe_b64encode(bytes.fromhex(_TEST_PUBLIC_KEY)).rstrip(b"=").decode()
_TEST_KEY_ID = "dexcost-test-rfc8032-1"
_ROTATED_SEED = bytes.fromhex(
    "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb"
)
_ROTATED_PUBLIC_KEY = "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c"
_ROTATED_PUBLIC_KEY_B64 = (
    urlsafe_b64encode(bytes.fromhex(_ROTATED_PUBLIC_KEY)).rstrip(b"=").decode()
)
_ROTATED_KEY_ID = "dexcost-test-rfc8032-2"


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        etag: str | None = None,
        content_type: str = "application/json",
    ) -> None:
        self._body = body
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(len(body))
        if etag is not None:
            self.headers["ETag"] = etag

    def read(self, amount: int = -1) -> bytes:
        return self._body if amount < 0 else self._body[:amount]

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _artifact_payloads() -> dict[str, bytes]:
    filenames = {
        "observer_rules": "service_usage_observers.json",
        "llm_prices": "model_cost_map.json",
        "service_prices": "service_prices.json",
        "compute_prices": "compute_prices.json",
        "gpu_prices": "gpu_prices.json",
        "egress_prices": "egress_prices.json",
    }
    payloads = {
        kind: _canonical(json.loads((_DATA / filename).read_text(encoding="utf-8")))
        for kind, filename in filenames.items()
    }
    payloads["server_pricing_reference"] = _canonical(
        {
            "catalog_version": "server-catalog-v7",
            "activation_id": "42",
            "source": "test",
            "rule_count": 1,
        }
    )
    return payloads


def _release(
    sequence: int,
    *,
    payloads: dict[str, bytes] | None = None,
    expires_delta: timedelta = timedelta(days=30),
) -> tuple[bytes, dict[str, bytes]]:
    import hashlib

    artifacts = payloads or _artifact_payloads()
    now = datetime.now(timezone.utc)
    descriptors: dict[str, Any] = {}
    for kind in CATALOG_KINDS:
        raw = artifacts[kind]
        data = json.loads(raw)
        if kind == "observer_rules":
            item_count = len(data["observers"])
        elif kind == "server_pricing_reference":
            item_count = 1
        else:
            item_count = len([key for key in data if key not in {"_meta", "sample_spec"}])
        digest = hashlib.sha256(raw).hexdigest()
        descriptors[kind] = {
            "kind": kind,
            "schema_version": "1",
            "sha256": digest,
            "byte_size": len(raw),
            "item_count": item_count,
            "media_type": "application/json",
            "path": f"/v1/catalogs/artifacts/sha256/{digest}",
            "sdk_contract": {"min": 1, "max": 1},
        }
    manifest = {
        "schema_version": "1",
        "release_id": f"catalog-release-test-{sequence}",
        "release_sequence": sequence,
        "channel": "stable",
        "published_at": (now - timedelta(days=1)).isoformat(),
        "expires_at": (now + expires_delta).isoformat(),
        "safety_policy_version": SUPPORTED_SAFETY_POLICY_VERSION,
        "sdk_contract": {"min": 1, "max": 1},
        "server_pricing_reference": {
            "catalog_version": "server-catalog-v7",
            "activation_id": "42",
        },
        "artifacts": descriptors,
        "signatures": [],
    }
    return _canonical(manifest), artifacts


def _activate(store: CatalogReleaseStore, sequence: int) -> None:
    raw, payloads = _release(sequence)
    store.activate(parse_catalog_manifest(raw), payloads, f'"release-{sequence}"')


def _signed_release(
    sequence: int,
    keys: tuple[tuple[str, bytes], ...] = ((_TEST_KEY_ID, _TEST_SEED),),
    *,
    expires_delta: timedelta = timedelta(days=30),
) -> tuple[bytes, dict[str, bytes]]:
    raw, payloads = _release(sequence, expires_delta=expires_delta)
    manifest = parse_catalog_manifest(raw)
    value = json.loads(raw)
    value["signatures"] = [
        {
            "algorithm": "ed25519",
            "key_id": key_id,
            "signature": urlsafe_b64encode(
                Ed25519PrivateKey.from_private_bytes(seed).sign(
                    catalog_manifest_signing_payload(manifest)
                )
            ).rstrip(b"=").decode(),
        }
        for key_id, seed in keys
    ]
    return _canonical(value), payloads


def _overlay(
    manifest: Any,
    overrides: list[dict[str, Any]],
) -> bytes:
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return _canonical(
        {
            "schema_version": "1",
            "base_release_id": manifest.release_id,
            "base_release_sequence": manifest.release_sequence,
            "generated_at": generated_at,
            "overrides": [
                {
                    "notes": None,
                    "updated_at": generated_at,
                    **override,
                }
                for override in overrides
            ],
        }
    )


def test_schema_v14_creates_catalog_revenue_and_provider_job_tables(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(tmp_path / "catalog.db")
    try:
        tables = {
            row[0]
            for row in storage._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert storage.get_schema_version() == TARGET_SCHEMA_VERSION == 14
        assert {
            "sdk_catalog_artifacts",
            "sdk_catalog_releases",
            "sdk_catalog_release_artifacts",
            "sdk_catalog_state",
            "sdk_catalog_overlays",
            "revenues",
            "provider_job_revisions",
        } <= tables
    finally:
        storage.close()


def test_store_activates_atomically_and_keeps_previous(tmp_path: Path) -> None:
    store = CatalogReleaseStore(tmp_path / "catalog.db")
    try:
        _activate(store, 10)
        _activate(store, 11)
        assert store.active() is not None
        assert store.active().manifest.release_sequence == 11  # type: ignore[union-attr]
        assert store.previous() is not None
        assert store.previous().manifest.release_sequence == 10  # type: ignore[union-attr]
    finally:
        store.close()

    reopened = CatalogReleaseStore(tmp_path / "catalog.db")
    try:
        snapshot = reopened.active()
        assert snapshot is not None
        assert snapshot.manifest.release_sequence == 11
        assert not snapshot.stale
    finally:
        reopened.close()


def test_store_falls_back_to_previous_when_only_active_manifest_is_corrupt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.db"
    store = CatalogReleaseStore(path)
    try:
        _activate(store, 10)
        _activate(store, 11)
    finally:
        store.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE sdk_catalog_releases SET manifest_json=? WHERE release_sequence=?",
            (b"{", 11),
        )
        connection.commit()
    finally:
        connection.close()

    reopened = CatalogReleaseStore(path)
    try:
        snapshot = reopened.best_available()
        assert snapshot is not None
        assert snapshot.source == "previous"
        assert snapshot.manifest.release_sequence == 10
    finally:
        reopened.close()


def test_store_verifies_signed_release_and_rejects_unsigned_or_tampered(
    tmp_path: Path,
) -> None:
    path = tmp_path / "signed-catalog.db"
    store = CatalogReleaseStore(
        path,
        trusted_keys={_TEST_KEY_ID: _TEST_PUBLIC_KEY_B64},
        require_signature=True,
    )
    try:
        raw, payloads = _signed_release(12)
        snapshot = store.activate(parse_catalog_manifest(raw), payloads, None)
        assert snapshot.manifest.release_sequence == 12

        unsigned, unsigned_payloads = _release(13)
        with pytest.raises(CatalogValidationError, match="requires a trusted signature"):
            store.activate(parse_catalog_manifest(unsigned), unsigned_payloads, None)

        changed = json.loads(raw)
        changed["release_sequence"] = 13
        changed["release_id"] = "catalog-release-test-13"
        with pytest.raises(CatalogValidationError, match="verification failed"):
            store.activate(parse_catalog_manifest(_canonical(changed)), payloads, None)
    finally:
        store.close()


def test_dual_signed_rotation_accepts_either_overlapping_trust_key(
    tmp_path: Path,
) -> None:
    raw, payloads = _signed_release(
        13,
        ((_TEST_KEY_ID, _TEST_SEED), (_ROTATED_KEY_ID, _ROTATED_SEED)),
    )
    stores = [
        CatalogReleaseStore(
            tmp_path / "old-key.db",
            trusted_keys={_TEST_KEY_ID: _TEST_PUBLIC_KEY_B64},
            require_signature=True,
        ),
        CatalogReleaseStore(
            tmp_path / "new-key.db",
            trusted_keys={_ROTATED_KEY_ID: _ROTATED_PUBLIC_KEY_B64},
            require_signature=True,
        ),
        CatalogReleaseStore(
            tmp_path / "unknown-key.db",
            trusted_keys={"dexcost-test-unknown": _ROTATED_PUBLIC_KEY_B64},
            require_signature=True,
        ),
    ]
    try:
        assert stores[0].activate(
            parse_catalog_manifest(raw), payloads, None
        ).manifest.release_sequence == 13
        assert stores[1].activate(
            parse_catalog_manifest(raw), payloads, None
        ).manifest.release_sequence == 13
        with pytest.raises(
            CatalogValidationError,
            match="not signed by a configured trusted key",
        ):
            stores[2].activate(parse_catalog_manifest(raw), payloads, None)
    finally:
        for store in stores:
            store.close()


def test_signed_bundle_round_trips_without_an_offline_trust_bypass(tmp_path: Path) -> None:
    policy = {
        "trusted_keys": {_TEST_KEY_ID: _TEST_PUBLIC_KEY_B64},
        "require_signature": True,
    }
    source = CatalogReleaseStore(tmp_path / "bundle-source.db", **policy)
    target = CatalogReleaseStore(tmp_path / "bundle-target.db", **policy)
    try:
        raw, payloads = _signed_release(14)
        source.activate(parse_catalog_manifest(raw), payloads, None)
        bundle = source.export_bundle()
        imported = target.import_bundle(bundle)
        assert imported.manifest.release_sequence == 14
        assert set(imported.artifacts) == set(CATALOG_KINDS)

        incomplete = json.loads(bundle)
        incomplete["artifacts_base64url"].pop("gpu_prices")
        with pytest.raises(CatalogValidationError, match="exactly"):
            target.import_bundle(_canonical(incomplete))
    finally:
        source.close()
        target.close()


def test_store_rejects_incomplete_activation_without_replacing_active(
    tmp_path: Path,
) -> None:
    store = CatalogReleaseStore(tmp_path / "catalog.db")
    try:
        _activate(store, 20)
        raw, payloads = _release(21)
        payloads.pop("gpu_prices")
        with pytest.raises(CatalogValidationError, match="every artifact"):
            store.activate(parse_catalog_manifest(raw), payloads, None)
        snapshot = store.active()
        assert snapshot is not None
        assert snapshot.manifest.release_sequence == 20
    finally:
        store.close()


def test_client_downloads_release_then_uses_conditional_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_raw, payloads = _release(30)
    manifest = parse_catalog_manifest(manifest_raw)
    store = CatalogReleaseStore(tmp_path / "catalog.db")
    client = CatalogReleaseClient("https://api.dexcost.test", store)
    calls: list[tuple[str, dict[str, str]]] = []

    def request(url: str, headers: dict[str, str]) -> _Response:
        calls.append((url, headers))
        if "/manifest?" in url:
            return _Response(manifest_raw, etag='"release-30"')
        descriptor = next(
            value for value in manifest.artifacts.values() if url.endswith(value.sha256)
        )
        return _Response(payloads[descriptor.kind])

    monkeypatch.setattr(client, "_request", request)
    try:
        result = client.refresh()
        assert result.status == "activated"
        assert result.snapshot is not None
        assert result.snapshot.manifest.release_sequence == 30
        assert len(calls) == 1 + len(CATALOG_KINDS)

        def not_modified(url: str, headers: dict[str, str]) -> _Response:
            assert headers["If-None-Match"] == '"release-30"'
            raise urllib.error.HTTPError(url, 304, "Not Modified", Message(), None)

        monkeypatch.setattr(client, "_request", not_modified)
        result = client.refresh()
        assert result.status == "not_modified"
        assert result.snapshot is not None
        assert result.snapshot.manifest.release_sequence == 30
    finally:
        store.close()


def test_corrupt_download_never_replaces_last_known_good(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CatalogReleaseStore(tmp_path / "catalog.db")
    _activate(store, 40)
    changed_payloads = _artifact_payloads()
    changed_gpu = json.loads(changed_payloads["gpu_prices"])
    changed_gpu["_meta"]["version"] = "corrupt-download-test"
    changed_payloads["gpu_prices"] = _canonical(changed_gpu)
    manifest_raw, payloads = _release(41, payloads=changed_payloads)
    manifest = parse_catalog_manifest(manifest_raw)
    client = CatalogReleaseClient("https://api.dexcost.test", store)

    def request(url: str, _headers: dict[str, str]) -> _Response:
        if "/manifest?" in url:
            return _Response(manifest_raw, etag='"release-41"')
        descriptor = next(
            value for value in manifest.artifacts.values() if url.endswith(value.sha256)
        )
        raw = payloads[descriptor.kind]
        if descriptor.kind == "gpu_prices":
            raw += b"corrupt"
        return _Response(raw)

    monkeypatch.setattr(client, "_request", request)
    try:
        result = client.refresh()
        assert result.status == "failed"
        assert result.snapshot is not None
        assert result.snapshot.manifest.release_sequence == 40
        active = store.active()
        assert active is not None
        assert active.manifest.release_sequence == 40
    finally:
        store.close()


def test_expired_manifest_is_rejected(tmp_path: Path) -> None:
    raw, _payloads = _release(50, expires_delta=timedelta(minutes=-2))
    manifest = parse_catalog_manifest(raw)
    store = CatalogReleaseStore(tmp_path / "catalog.db")
    try:
        with pytest.raises(CatalogValidationError, match="expired"):
            store.activate(manifest, _artifact_payloads(), None)
    finally:
        store.close()


def test_expired_signed_bundle_is_rejected_without_replacing_lkg(
    tmp_path: Path,
) -> None:
    store = CatalogReleaseStore(
        tmp_path / "catalog.db",
        trusted_keys={_TEST_KEY_ID: _TEST_PUBLIC_KEY_B64},
        require_signature=True,
    )
    try:
        current_raw, current_payloads = _signed_release(51)
        store.activate(parse_catalog_manifest(current_raw), current_payloads, None)
        expired_raw, expired_payloads = _signed_release(
            52,
            expires_delta=timedelta(minutes=-2),
        )
        expired_bundle = encode_catalog_bundle(
            parse_catalog_manifest(expired_raw),
            expired_payloads,
        )
        with pytest.raises(CatalogValidationError, match="expired"):
            store.import_bundle(expired_bundle)
        snapshot = store.best_available()
        assert snapshot is not None
        assert snapshot.source == "active"
        assert snapshot.manifest.release_sequence == 51
    finally:
        store.close()


def test_python_catalog_probe_exercises_all_offline_safety_operations(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "probe.db"
    script = Path(__file__).resolve().parents[1] / "scripts" / "catalog_release_probe.py"
    bundles: dict[int, Path] = {}
    for sequence, expires_delta in (
        (80, timedelta(days=30)),
        (81, timedelta(days=30)),
        (82, timedelta(minutes=-2)),
    ):
        raw, payloads = _signed_release(sequence, expires_delta=expires_delta)
        path = tmp_path / f"release-{sequence}.bundle.json"
        path.write_bytes(encode_catalog_bundle(parse_catalog_manifest(raw), payloads))
        bundles[sequence] = path

    def run_probe(
        operation: list[str],
        expected_release: int,
        expected_status: str,
        expected_source: str,
    ) -> dict[str, Any]:
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--store",
                str(store_path),
                "--key-id",
                _TEST_KEY_ID,
                "--public-key",
                _TEST_PUBLIC_KEY_B64,
                "--expect-release",
                f"catalog-release-test-{expected_release}",
                *operation,
            ],
            text=True,
            capture_output=True,
            check=False,
            env={
                **os.environ,
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            },
        )
        assert result.returncode == 0, result.stdout + result.stderr
        output = json.loads(result.stdout)
        assert output["status"] == expected_status
        assert output["source"] == expected_source
        assert output["stale"] is False
        assert set(output["artifact_sha256"]) == set(CATALOG_KINDS)
        return output

    run_probe(["--import-bundle", str(bundles[80])], 80, "imported", "active")
    exported = tmp_path / "exported.bundle.json"
    run_probe(
        [
            "--import-bundle",
            str(bundles[81]),
            "--export-bundle",
            str(exported),
        ],
        81,
        "imported",
        "active",
    )
    assert exported.read_bytes() == bundles[81].read_bytes()
    run_probe(
        ["--reject-corrupt-bundle", str(bundles[81])],
        81,
        "corrupt_rejected_lkg_preserved",
        "active",
    )
    run_probe(
        ["--reject-expired-bundle", str(bundles[82])],
        81,
        "expired_rejected_lkg_preserved",
        "active",
    )
    run_probe(
        ["--corrupt-active-store"],
        80,
        "active_corrupt_previous_preserved",
        "previous",
    )


def test_unsupported_safety_policy_is_rejected() -> None:
    raw, _payloads = _release(60)
    manifest = json.loads(raw)
    manifest["safety_policy_version"] = "future-policy-v99"
    with pytest.raises(CatalogValidationError, match="unsupported catalog safety policy"):
        parse_catalog_manifest(_canonical(manifest))


def test_runtime_applies_one_cached_release_to_every_pricing_engine(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "catalog.db"
    seed_store = CatalogReleaseStore(db_path)
    try:
        _activate(seed_store, 70)
    finally:
        seed_store.close()

    tracker_storage = SQLiteStorage(db_path)
    tracker = CostTracker(storage=tracker_storage, auto_instrument=[])
    runtime = CatalogRuntime(
        endpoint="https://api.dexcost.test",
        db_path=db_path,
        tracker=tracker,
        track_http=False,
    )
    try:
        snapshot = runtime.load_cached()
        assert snapshot is not None
        assert snapshot.manifest.release_sequence == 70
        assert tracker.pricing.pricing_version.startswith("catalog-release:70:")
        assert tracker._compute_pricing.catalog_version.startswith("catalog-release:70:")
        assert tracker._gpu_pricing.catalog_version.startswith("catalog-release:70:")
        assert tracker._egress_pricing.catalog_version.startswith("catalog-release:70:")
        status = runtime.status()
        assert status.release_sequence == 70
        assert status.source == "active"
        assert not status.stale
        with tracker.task(task_type="catalog.explain") as task:
            event = task.record_llm_call("openai", "gpt-4o", 10, 5)
        explanation = tracker.explain_pricing(event)
        assert explanation.provenance is not None
        assert explanation.provenance.release_id == snapshot.manifest.release_id
        assert explanation.provenance.release_sequence == 70
        assert explanation.provenance.artifact_kind == "llm_prices"
        assert explanation.provenance.artifact_sha256 == (
            snapshot.manifest.artifacts["llm_prices"].sha256
        )
        assert explanation.provenance.safety_policy_version == (
            snapshot.manifest.safety_policy_version
        )
    finally:
        runtime.close()
        tracker.pricing.close()
        tracker_storage.close()


def test_overlay_parser_is_release_bound_component_safe_and_decimal_safe() -> None:
    manifest = parse_catalog_manifest(_release(80)[0])
    valid = _overlay(
        manifest,
        [
            {
                "kind": "compute",
                "key": "lambda",
                "rate_usd": "0.0000002",
                "per": "request",
            },
            {
                "kind": "compute",
                "key": "lambda",
                "rate_usd": "0.0000166667",
                "per": "gb_second",
            },
        ],
    )
    parsed = parse_catalog_overlay(valid, manifest)
    assert len(parsed.overrides) == 2
    assert parsed.overrides[0].rate_usd == Decimal("0.0000002")

    duplicate = json.loads(valid)
    duplicate["overrides"].append(dict(duplicate["overrides"][0]))
    with pytest.raises(CatalogValidationError, match="duplicate component"):
        parse_catalog_overlay(_canonical(duplicate), manifest)

    wrong_unit = json.loads(valid)
    wrong_unit["overrides"][0]["per"] = "gpu_hour"
    with pytest.raises(CatalogValidationError, match="unsupported compute"):
        parse_catalog_overlay(_canonical(wrong_unit), manifest)

    unsafe_rate = json.loads(valid)
    unsafe_rate["overrides"][0]["rate_usd"] = "NaN"
    with pytest.raises(CatalogValidationError, match="rate_usd"):
        parse_catalog_overlay(_canonical(unsafe_rate), manifest)

    other_manifest = parse_catalog_manifest(_release(81)[0])
    with pytest.raises(CatalogValidationError, match="not bound"):
        parse_catalog_overlay(valid, other_manifest)


def test_overlay_cache_is_api_key_isolated_and_revalidates_with_etag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CatalogReleaseStore(tmp_path / "catalog.db")
    try:
        _activate(store, 90)
        snapshot = store.active()
        assert snapshot is not None
        raw = _overlay(
            snapshot.manifest,
            [
                {
                    "kind": "egress",
                    "key": "aws:us-east-1",
                    "rate_usd": "0.123",
                    "per": "gb_egress",
                }
            ],
        )
        client_a = CatalogOverlayClient(
            "https://api.dexcost.test", "dx_key_a", store
        )
        calls: list[Any] = []

        def first_open(request: Any, *, timeout: float) -> _Response:
            calls.append(request)
            assert timeout == 10
            assert request.get_header("Authorization") == "Bearer dx_key_a"
            assert "base_release_id=catalog-release-test-90" in request.full_url
            return _Response(raw, etag='"overlay-a"')

        monkeypatch.setattr(client_a._opener, "open", first_open)
        activated = client_a.refresh(snapshot.manifest)
        assert activated.status == "activated"
        assert activated.overlay is not None

        client_b = CatalogOverlayClient(
            "https://api.dexcost.test", "dx_key_b", store
        )
        assert client_b.cached(snapshot.manifest) is None
        principal = store._conn.execute(
            "SELECT principal_sha256 FROM sdk_catalog_overlays"
        ).fetchone()[0]
        assert principal == hashlib.sha256(b"dx_key_a").hexdigest()
        assert principal != "dx_key_a"

        def not_modified(request: Any, *, timeout: float) -> _Response:
            assert timeout == 10
            assert request.get_header("If-none-match") == '"overlay-a"'
            raise urllib.error.HTTPError(
                request.full_url, 304, "Not Modified", Message(), None
            )

        monkeypatch.setattr(client_a._opener, "open", not_modified)
        cached = client_a.refresh(snapshot.manifest)
        assert cached.status == "not_modified"
        assert cached.overlay == activated.overlay
    finally:
        store.close()


def test_runtime_applies_all_overlay_kinds_and_drops_them_on_key_rotation(
    tmp_path: Path,
) -> None:
    from dexcost.adapters.http import get_catalog, set_catalog
    from dexcost.cloud_detect import CloudEnv

    db_path = tmp_path / "catalog.db"
    api_key = "dx_overlay_principal"
    seed_store = CatalogReleaseStore(db_path)
    try:
        _activate(seed_store, 100)
        snapshot = seed_store.active()
        assert snapshot is not None
        raw = _overlay(
            snapshot.manifest,
            [
                {
                    "kind": "service",
                    "key": "exa_search",
                    "rate_usd": "0.123",
                    "per": "request",
                },
                {
                    "kind": "compute",
                    "key": "lambda",
                    "rate_usd": "0.01",
                    "per": "request",
                },
                {
                    "kind": "compute",
                    "key": "lambda",
                    "rate_usd": "0.02",
                    "per": "gb_second",
                },
                {
                    "kind": "gpu",
                    "key": "h100-80gb-sxm5",
                    "rate_usd": "0.5",
                    "per": "gpu_second",
                },
                {
                    "kind": "egress",
                    "key": "aws:us-east-1",
                    "rate_usd": "0.25",
                    "per": "gb_transferred",
                },
            ],
        )
        overlay = parse_catalog_overlay(raw, snapshot.manifest)
        seed_store.save_overlay(
            hashlib.sha256(api_key.encode()).hexdigest(),
            overlay,
            '"overlay-100"',
        )
    finally:
        seed_store.close()

    original_catalog = get_catalog()
    tracker_storage = SQLiteStorage(db_path)
    tracker = CostTracker(storage=tracker_storage, auto_instrument=[])
    runtime = CatalogRuntime(
        endpoint="https://api.dexcost.test",
        db_path=db_path,
        tracker=tracker,
        track_http=True,
        api_key=api_key,
    )
    try:
        assert runtime.load_cached() is not None

        compute = tracker._compute_pricing.resolve_compute_cost(
            {
                "billing_model": "lambda",
                "duration_ms": 1000,
                "memory_bytes_limit": 1_000_000_000,
                "vcpu_count": 1,
                "vcpu_seconds_used": 0,
                "invocation_count": 2,
                "region": "us-east-1",
                "architecture": "x86_64",
            },
            CloudEnv("aws", "us-east-1", "env"),
            {},
        )
        assert compute.cost_usd == Decimal("0.04")
        assert compute.pricing_source.endswith("gb_second+request")

        gpu = tracker._gpu_pricing.resolve_gpu_cost(
            {
                "billing_model": "per_gpu_second_active",
                "gpu_sku": "h100-80gb-sxm5",
                "gpu_seconds_used": 2,
                "duration_ms": 2000,
            },
            CloudEnv("modal", None, "env"),
        )
        assert gpu.cost_usd == Decimal("1.0")
        assert gpu.pricing_source.startswith("workspace_overlay:gpu")

        egress = tracker._egress_pricing.resolve_rate("aws", "us-east-1")
        assert egress.rate_per_gb == Decimal("0.25")
        assert egress.billing_unit == "gb_transferred"

        service = get_catalog()
        entry = service.lookup("https://api.exa.ai/search")
        assert entry is not None
        extracted = service.extract_cost(entry, {}, None)
        assert extracted is not None
        assert extracted.amount == Decimal("0.123")
        assert extracted.pricing_source == "workspace_overlay"
        service.register_override("exa_search", Decimal("0.456"))
        local = service.extract_cost(entry, {}, None)
        assert local is not None
        assert local.amount == Decimal("0.456")
        assert local.pricing_source == "user_override"

        status = runtime.status()
        assert status.overlay_active
        assert status.overlay_override_count == 5

        runtime.set_api_key("dx_different_principal")
        rotated = runtime.status()
        assert not rotated.overlay_active
        assert rotated.overlay_override_count == 0
        assert tracker._egress_pricing.resolve_rate(
            "aws", "us-east-1"
        ).rate_per_gb == Decimal("0.09")
    finally:
        runtime.close()
        tracker.pricing.close()
        tracker_storage.close()
        set_catalog(original_catalog)
