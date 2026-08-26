"""Non-blocking runtime integration for durable catalog releases."""

from __future__ import annotations

import logging
import os
import random
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from dexcost.catalog_releases import (
    CatalogOverlayClient,
    CatalogOverlayRefreshResult,
    CatalogRefreshResult,
    CatalogReleaseClient,
    CatalogReleaseStore,
    CatalogSnapshot,
    CatalogValidationError,
    CatalogWorkspaceOverlay,
)

if TYPE_CHECKING:
    from dexcost.tracker import CostTracker

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class CatalogRuntimeStatus:
    release_id: str | None
    release_sequence: int | None
    source: str
    stale: bool
    last_refresh_status: str | None
    last_error: str | None
    overlay_active: bool = False
    overlay_override_count: int = 0
    overlay_last_refresh_status: str | None = None
    overlay_last_error: str | None = None
    signature_verification: Literal[
        "verified", "unsigned_override", "disabled_no_trust", "unavailable"
    ] = "unavailable"
    trusted_key_ids: tuple[str, ...] = ()
    remote_refresh_enabled: bool = False


class CatalogRuntime:
    """Own the durable store, refresh worker, and in-process catalog snapshot."""

    def __init__(
        self,
        *,
        endpoint: str,
        db_path: str | Path | None,
        tracker: CostTracker,
        track_http: bool,
        api_key: str | None = None,
        refresh_interval_seconds: float = 86_400,
        refresh_jitter_ratio: float = 0.1,
        trusted_keys: Mapping[str, str | bytes] | None = None,
        require_signature: bool = False,
        remote_refresh_enabled: bool = True,
    ) -> None:
        if refresh_interval_seconds <= 0:
            raise ValueError("catalog refresh interval must be positive")
        if not 0 <= refresh_jitter_ratio <= 0.5:
            raise ValueError("catalog refresh jitter ratio must be between 0 and 0.5")
        self._endpoint = endpoint
        self._trusted_keys = dict(trusted_keys or {})
        self._require_signature = require_signature
        self._remote_refresh_enabled = remote_refresh_enabled
        self._signature_verification: Literal[
            "verified", "unsigned_override", "disabled_no_trust"
        ] = (
            "verified"
            if require_signature and self._trusted_keys
            else "unsigned_override"
            if remote_refresh_enabled
            else "disabled_no_trust"
        )
        self._store = CatalogReleaseStore(
            db_path,
            trusted_keys=self._trusted_keys,
            require_signature=require_signature,
        )
        self._client = CatalogReleaseClient(endpoint, self._store)
        self._overlay_client = (
            CatalogOverlayClient(endpoint, api_key, self._store) if api_key else None
        )
        self._overlay_generation = 0
        self._tracker = tracker
        self._track_http = track_http
        self._refresh_interval_seconds = refresh_interval_seconds
        self._refresh_jitter_ratio = refresh_jitter_ratio
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Bundle import holds the runtime lock across durable activation and the
        # in-memory swap.  _apply() also owns the swap lock, so imports require
        # re-entrant acquisition to preserve that ordering without deadlocking.
        self._lock = threading.RLock()
        self._snapshot: CatalogSnapshot | None = None
        self._overlay: CatalogWorkspaceOverlay | None = None
        self._last_result: CatalogRefreshResult | None = None
        self._last_overlay_result: CatalogOverlayRefreshResult | None = None
        self._close_when_stopped = False
        self._store_closed = False

    def load_cached(self) -> CatalogSnapshot | None:
        """Apply active durable state, falling back to the previous release."""
        if not self._remote_refresh_enabled:
            return None
        snapshot = self._store.best_available()
        if snapshot is not None:
            overlay = self._cached_overlay(snapshot)
            self._apply(snapshot, overlay)
        return snapshot

    def import_bundle(
        self,
        bundle: bytes | bytearray | memoryview | str | os.PathLike[str],
    ) -> CatalogSnapshot:
        """Validate, activate, and atomically apply an offline release bundle."""
        raw = (
            Path(bundle).read_bytes() if isinstance(bundle, (str, os.PathLike)) else bytes(bundle)
        )
        if not self._remote_refresh_enabled:
            raise CatalogValidationError(
                "catalog trust is not bootstrapped; refusing offline bundle activation"
            )
        with self._lock:
            snapshot = self._store.import_bundle(raw)
            overlay = self._cached_overlay(snapshot)
            self._apply(snapshot, overlay)
            return snapshot

    def export_bundle(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        source: Literal["active", "previous"] = "active",
    ) -> bytes:
        """Export exact durable release bytes for transfer to an air-gapped host."""
        raw = self._store.export_bundle(source=source)
        if path is not None:
            Path(path).write_bytes(raw)
        return raw

    def _cached_overlay(
        self,
        snapshot: CatalogSnapshot,
        client: CatalogOverlayClient | None = None,
    ) -> CatalogWorkspaceOverlay | None:
        candidate = client if client is not None else self._overlay_client
        if candidate is None:
            return None
        try:
            return candidate.cached(snapshot.manifest)
        except CatalogValidationError:
            _LOG.warning(
                "durable workspace catalog overlay is invalid; ignoring it",
                exc_info=True,
            )
            return None

    @staticmethod
    def _version(snapshot: CatalogSnapshot, kind: str) -> str:
        descriptor = snapshot.manifest.artifacts[kind]
        stale_suffix = ":stale" if snapshot.stale else ""
        return (
            f"catalog-release:{snapshot.manifest.release_sequence}:"
            f"{descriptor.sha256[:12]}{stale_suffix}"
        )

    @staticmethod
    def _group_overrides(
        overlay: CatalogWorkspaceOverlay | None,
    ) -> tuple[
        dict[str, tuple[Decimal, str]],
        dict[tuple[str, str], Decimal],
        dict[tuple[str, str], Decimal],
        dict[str, tuple[Decimal, str]],
    ]:
        service: dict[str, tuple[Decimal, str]] = {}
        compute: dict[tuple[str, str], Decimal] = {}
        gpu: dict[tuple[str, str], Decimal] = {}
        egress: dict[str, tuple[Decimal, str]] = {}
        if overlay is None:
            return service, compute, gpu, egress
        for rate in overlay.overrides:
            if rate.kind == "service":
                service[rate.key] = (rate.rate_usd, rate.per)
            elif rate.kind == "compute":
                compute[(rate.key, rate.per)] = rate.rate_usd
            elif rate.kind == "gpu":
                gpu[(rate.key, rate.per)] = rate.rate_usd
            else:
                egress[rate.key] = (rate.rate_usd, rate.per)
        return service, compute, gpu, egress

    def _apply(
        self,
        snapshot: CatalogSnapshot,
        overlay: CatalogWorkspaceOverlay | None = None,
        *,
        overlay_generation: int | None = None,
    ) -> bool:
        """Construct every consumer first, then swap the validated release in."""
        from dexcost.adapters.http import get_catalog, set_catalog
        from dexcost.compute_pricing import ComputePricingEngine
        from dexcost.egress_pricing import EgressPricingEngine
        from dexcost.gpu_pricing import GpuPricingEngine
        from dexcost.models.pricing_explanation import PricingProvenance
        from dexcost.pricing import PricingEngine
        from dexcost.pricing_explain import register_pricing_provenance
        from dexcost.service_catalog import ServiceCatalog
        from dexcost.service_usage_observers import (
            ServiceUsageObservers,
            set_service_usage_observers,
        )

        artifacts = snapshot.artifacts
        service_rates, compute_rates, gpu_rates, egress_rates = self._group_overrides(overlay)
        pricing_candidate = PricingEngine(
            catalog_data=artifacts["llm_prices"],
            catalog_version=self._version(snapshot, "llm_prices"),
        )
        compute = ComputePricingEngine(
            catalog_data=artifacts["compute_prices"],
            catalog_version=self._version(snapshot, "compute_prices"),
            rate_overrides=compute_rates,
        )
        gpu = GpuPricingEngine(
            catalog_data=artifacts["gpu_prices"],
            catalog_version=self._version(snapshot, "gpu_prices"),
            rate_overrides=gpu_rates,
        )
        egress = EgressPricingEngine(
            catalog_data=artifacts["egress_prices"],
            catalog_version=self._version(snapshot, "egress_prices"),
            rate_overrides=egress_rates,
        )
        service = ServiceCatalog(
            data=artifacts["service_prices"],
            catalog_version=self._version(snapshot, "service_prices"),
        )
        observers = ServiceUsageObservers(data=artifacts["observer_rules"])

        observer_hash = snapshot.manifest.artifacts["observer_rules"].sha256

        def provenance(kind: str) -> PricingProvenance:
            descriptor = snapshot.manifest.artifacts[kind]
            return PricingProvenance(
                catalog_source=snapshot.source,
                stale=snapshot.stale,
                release_id=snapshot.manifest.release_id,
                release_sequence=snapshot.manifest.release_sequence,
                artifact_kind=kind,
                artifact_sha256=descriptor.sha256,
                artifact_schema_version=descriptor.schema_version,
                observer_rules_sha256=(observer_hash if kind == "service_prices" else None),
                safety_policy_version=snapshot.manifest.safety_policy_version,
                workspace_overlay=overlay is not None,
            )

        register_pricing_provenance(pricing_candidate.pricing_version, provenance("llm_prices"))
        register_pricing_provenance(
            f"compute:{compute.catalog_version}", provenance("compute_prices")
        )
        register_pricing_provenance(f"gpu:{gpu.catalog_version}", provenance("gpu_prices"))
        register_pricing_provenance(
            f"egress:{egress.catalog_version}", provenance("egress_prices")
        )
        register_pricing_provenance(service.catalog_version, provenance("service_prices"))

        if self._track_http:
            service.inherit_overrides(get_catalog())
            service.set_workspace_overrides(service_rates)

        with self._lock:
            if overlay_generation is not None and overlay_generation != self._overlay_generation:
                pricing_candidate.close()
                return False
            self._tracker._pricing.replace_catalog(
                artifacts["llm_prices"],
                pricing_candidate.pricing_version,
            )
            self._tracker._compute_pricing = compute
            self._tracker._gpu_pricing = gpu
            self._tracker._egress_pricing = egress
            # MCP pricing aliases belong to the same signed service artifact.
            # Keep the active catalog tracker-local even when raw HTTP capture
            # is disabled, so tool instrumentation never needs a second fetch
            # or a process-global catalog to resolve those aliases.
            self._tracker._service_catalog = service
            if self._track_http:
                set_catalog(service)
                set_service_usage_observers(observers)
            self._snapshot = snapshot
            self._overlay = overlay
        pricing_candidate.close()
        return True

    def refresh_once(self) -> CatalogRefreshResult:
        """Refresh once and apply a newly activated or revalidated snapshot."""
        if not self._remote_refresh_enabled:
            result = CatalogRefreshResult(
                "failed",
                None,
                "catalog trust is not bootstrapped; remote refresh is disabled",
            )
            with self._lock:
                self._last_result = result
            return result
        result = self._client.refresh()
        if result.snapshot is not None:
            with self._lock:
                overlay_client = self._overlay_client
                overlay_generation = self._overlay_generation
            overlay_result = (
                overlay_client.refresh(result.snapshot.manifest)
                if overlay_client is not None
                else None
            )
            overlay = None if overlay_result is None else overlay_result.overlay
            with self._lock:
                current = self._snapshot
                current_overlay = self._overlay
            if (
                current is None
                or current.manifest.sha256 != result.snapshot.manifest.sha256
                or current.stale != result.snapshot.stale
                or current_overlay != overlay
            ):
                applied = self._apply(
                    result.snapshot,
                    overlay,
                    overlay_generation=overlay_generation,
                )
                if not applied:
                    with self._lock:
                        overlay_client = self._overlay_client
                        overlay_generation = self._overlay_generation
                    overlay = self._cached_overlay(result.snapshot, overlay_client)
                    self._apply(
                        result.snapshot,
                        overlay,
                        overlay_generation=overlay_generation,
                    )
            with self._lock:
                if overlay_generation == self._overlay_generation:
                    self._last_overlay_result = overlay_result
        with self._lock:
            self._last_result = result
        return result

    def set_api_key(self, api_key: str | None) -> None:
        """Rotate overlay identity without ever reusing another principal's rates."""
        candidate = CatalogOverlayClient(self._endpoint, api_key, self._store) if api_key else None
        with self._lock:
            self._overlay_client = candidate
            self._overlay_generation += 1
            generation = self._overlay_generation
            snapshot = self._snapshot
            self._last_overlay_result = None
        if snapshot is not None:
            overlay = self._cached_overlay(snapshot, candidate)
            self._apply(snapshot, overlay, overlay_generation=generation)

    def start(self) -> None:
        """Start immediate and periodic refreshes on a daemon thread."""
        if not self._remote_refresh_enabled:
            _LOG.warning(
                "catalog remote refresh is disabled until trusted production keys "
                "are configured; bundled pricing remains active"
            )
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()

        def run() -> None:
            try:
                while not self._stop.is_set():
                    try:
                        self.refresh_once()
                    except Exception:
                        _LOG.warning("catalog runtime refresh failed", exc_info=True)
                    jitter = random.uniform(
                        1 - self._refresh_jitter_ratio,
                        1 + self._refresh_jitter_ratio,
                    )
                    self._stop.wait(self._refresh_interval_seconds * jitter)
            finally:
                with self._lock:
                    close_store = self._close_when_stopped
                if close_store:
                    self._close_store()

        self._thread = threading.Thread(
            target=run,
            daemon=True,
            name="dexcost-catalog-refresh",
        )
        self._thread.start()

    def status(self) -> CatalogRuntimeStatus:
        with self._lock:
            snapshot = self._snapshot
            overlay = self._overlay
            result = self._last_result
            overlay_result = self._last_overlay_result
        if snapshot is None:
            return CatalogRuntimeStatus(
                release_id=None,
                release_sequence=None,
                source="bootstrap",
                stale=False,
                last_refresh_status=None if result is None else result.status,
                last_error=None if result is None else result.error,
                overlay_active=False,
                overlay_override_count=0,
                overlay_last_refresh_status=(
                    None if overlay_result is None else overlay_result.status
                ),
                overlay_last_error=(None if overlay_result is None else overlay_result.error),
                signature_verification=self._signature_verification,
                trusted_key_ids=tuple(sorted(self._trusted_keys)),
                remote_refresh_enabled=self._remote_refresh_enabled,
            )
        return CatalogRuntimeStatus(
            release_id=snapshot.manifest.release_id,
            release_sequence=snapshot.manifest.release_sequence,
            source=snapshot.source,
            stale=snapshot.stale,
            last_refresh_status=None if result is None else result.status,
            last_error=None if result is None else result.error,
            overlay_active=overlay is not None,
            overlay_override_count=0 if overlay is None else len(overlay.overrides),
            overlay_last_refresh_status=(
                None if overlay_result is None else overlay_result.status
            ),
            overlay_last_error=None if overlay_result is None else overlay_result.error,
            signature_verification=self._signature_verification,
            trusted_key_ids=tuple(sorted(self._trusted_keys)),
            remote_refresh_enabled=self._remote_refresh_enabled,
        )

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        with self._lock:
            self._close_when_stopped = True
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._thread = None
        if thread is None or not thread.is_alive():
            self._close_store()

    def _close_store(self) -> None:
        with self._lock:
            if self._store_closed:
                return
            self._store.close()
            self._store_closed = True
