import { CatalogOverlayClient, CatalogReleaseClient, CatalogReleaseStore } from "./catalog-releases.js";
import type {
  CatalogChannel,
  CatalogOverlayRefreshResult,
  CatalogRefreshResult,
  CatalogSnapshot,
  CatalogWorkspaceOverlay,
} from "./catalog-releases.js";
import { PricingEngine } from "./engine.js";
import { ComputePricingEngine } from "./compute-pricing.js";
import { GpuPricingEngine } from "./gpu-pricing.js";
import { EgressPricingEngine } from "./egress-pricing.js";
import { ServiceCatalog } from "./service-catalog.js";
import { ServiceUsageObservers, setServiceUsageObservers } from "./service-usage-observers.js";
import { getServiceCatalog, setServiceCatalog } from "../adapters/http.js";
import { setEgressPricingEngine } from "../core/network-finalize.js";
import { PricingProvenance, registerPricingProvenance } from "./explain.js";

export interface CatalogRuntimeTargets {
  pricing: PricingEngine;
  replaceCompute(engine: ComputePricingEngine): void;
  replaceGpu(engine: GpuPricingEngine): void;
  trackHttp: boolean;
}

export interface CatalogRuntimeOptions {
  endpoint: string;
  storePath?: string;
  apiKey?: string;
  refreshIntervalMs?: number;
  refreshJitterRatio?: number;
  channel?: CatalogChannel;
  trustedKeys?: Readonly<Record<string, string>>;
  requireSignature?: boolean;
  timeoutMs?: number;
}

export interface CatalogRuntimeStatus {
  releaseId?: string;
  releaseSequence?: number;
  source: "bootstrap" | "active" | "previous";
  stale: boolean;
  lastRefreshStatus?: CatalogRefreshResult["status"];
  lastError?: string;
  overlayActive: boolean;
  overlayOverrideCount: number;
  overlayLastRefreshStatus?: CatalogOverlayRefreshResult["status"];
  overlayLastError?: string;
}

interface GroupedOverrides {
  service: Map<string, { rateUsd: string; per: string }>;
  compute: Map<string, string>;
  gpu: Map<string, string>;
  egress: Map<string, { rateUsd: string; per: string }>;
}

/**
 * Owns durable catalog releases and applies one complete in-memory snapshot.
 * Refreshing is always outside provider hot paths and failures retain the LKG.
 */
export class CatalogRuntime {
  private readonly store: CatalogReleaseStore;
  private readonly client: CatalogReleaseClient;
  private overlayClient?: CatalogOverlayClient;
  private overlayGeneration = 0;
  private readonly intervalMs: number;
  private readonly jitterRatio: number;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private stopped = true;
  private refreshPromise: Promise<CatalogRefreshResult> | null = null;
  private snapshot?: CatalogSnapshot;
  private overlay?: CatalogWorkspaceOverlay;
  private lastResult?: CatalogRefreshResult;
  private lastOverlayResult?: CatalogOverlayRefreshResult;

  constructor(private readonly targets: CatalogRuntimeTargets, private readonly options: CatalogRuntimeOptions) {
    this.intervalMs = options.refreshIntervalMs ?? 86_400_000;
    this.jitterRatio = options.refreshJitterRatio ?? 0.1;
    if (!Number.isFinite(this.intervalMs) || this.intervalMs <= 0) {
      throw new TypeError("catalog refresh interval must be positive");
    }
    if (!Number.isFinite(this.jitterRatio) || this.jitterRatio < 0 || this.jitterRatio > 0.5) {
      throw new TypeError("catalog refresh jitter ratio must be between 0 and 0.5");
    }
    this.store = new CatalogReleaseStore(options.storePath, {
      trustedKeys: options.trustedKeys,
      requireSignature: options.requireSignature,
    });
    this.client = new CatalogReleaseClient(
      options.endpoint,
      this.store,
      options.channel ?? "stable",
      options.timeoutMs,
    );
    if (options.apiKey) this.overlayClient = new CatalogOverlayClient(options.endpoint, options.apiKey, this.store);
  }

  loadCached(): CatalogSnapshot | undefined {
    const snapshot = this.store.bestAvailable(this.options.channel ?? "stable");
    if (snapshot) {
      const overlay = this.cachedOverlay(snapshot);
      this.apply(snapshot, overlay);
    }
    return snapshot;
  }

  importBundle(raw: Uint8Array): CatalogSnapshot {
    const snapshot = this.store.importBundle(raw);
    this.apply(snapshot, this.cachedOverlay(snapshot), this.overlayGeneration);
    return snapshot;
  }

  exportBundle(source: "active" | "previous" = "active"): Uint8Array {
    return this.store.exportBundle(this.options.channel ?? "stable", source);
  }

  private cachedOverlay(snapshot: CatalogSnapshot): CatalogWorkspaceOverlay | undefined {
    if (!this.overlayClient) return undefined;
    try { return this.overlayClient.cached(snapshot.manifest); }
    catch { return undefined; }
  }

  private version(snapshot: CatalogSnapshot, kind: keyof CatalogSnapshot["artifacts"]): string {
    const digest = snapshot.manifest.artifacts[kind].sha256.slice(0, 12);
    return `catalog-release:${snapshot.manifest.release_sequence}:${digest}${snapshot.stale ? ":stale" : ""}`;
  }

  private groupOverrides(overlay?: CatalogWorkspaceOverlay): GroupedOverrides {
    const grouped: GroupedOverrides = {
      service: new Map(), compute: new Map(), gpu: new Map(), egress: new Map(),
    };
    for (const rate of overlay?.overrides ?? []) {
      if (rate.kind === "service") grouped.service.set(rate.key, { rateUsd: rate.rateUsd, per: rate.per });
      else if (rate.kind === "compute") grouped.compute.set(`${rate.key}\0${rate.per}`, rate.rateUsd);
      else if (rate.kind === "gpu") grouped.gpu.set(`${rate.key}\0${rate.per}`, rate.rateUsd);
      else grouped.egress.set(rate.key, { rateUsd: rate.rateUsd, per: rate.per });
    }
    return grouped;
  }

  private apply(snapshot: CatalogSnapshot, overlay?: CatalogWorkspaceOverlay, generation?: number): boolean {
    const artifacts = snapshot.artifacts;
    const rates = this.groupOverrides(overlay);

    // Construct and validate every consumer before swapping any live state.
    const pricingCandidate = new PricingEngine();
    pricingCandidate.replaceCatalog(artifacts.llm_prices, this.version(snapshot, "llm_prices"));
    const compute = new ComputePricingEngine({
      catalog: artifacts.compute_prices,
      catalogVersion: this.version(snapshot, "compute_prices"),
      rateOverrides: rates.compute,
    });
    const gpu = new GpuPricingEngine({
      catalog: artifacts.gpu_prices,
      catalogVersion: this.version(snapshot, "gpu_prices"),
      rateOverrides: rates.gpu,
    });
    const egress = new EgressPricingEngine(
      artifacts.egress_prices,
      this.version(snapshot, "egress_prices"),
      rates.egress,
    );
    const service = new ServiceCatalog(
      undefined,
      artifacts.service_prices,
      this.version(snapshot, "service_prices"),
    );
    const observers = new ServiceUsageObservers(artifacts.observer_rules);
    const observerRulesSha256 = snapshot.manifest.artifacts.observer_rules.sha256;
    const provenance = (kind: keyof CatalogSnapshot["artifacts"]): PricingProvenance => {
      const descriptor = snapshot.manifest.artifacts[kind];
      return new PricingProvenance({
        catalogSource: snapshot.source,
        stale: snapshot.stale,
        releaseId: snapshot.manifest.release_id,
        releaseSequence: snapshot.manifest.release_sequence,
        artifactKind: kind,
        artifactSha256: descriptor.sha256,
        artifactSchemaVersion: descriptor.schema_version,
        observerRulesSha256: kind === "service_prices" ? observerRulesSha256 : undefined,
        safetyPolicyVersion: snapshot.manifest.safety_policy_version,
        workspaceOverlay: overlay !== undefined,
      });
    };
    registerPricingProvenance(pricingCandidate.pricingVersion, provenance("llm_prices"));
    registerPricingProvenance(`compute:${compute.catalogVersion}`, provenance("compute_prices"));
    registerPricingProvenance(`gpu:${gpu.catalogVersion}`, provenance("gpu_prices"));
    registerPricingProvenance(`egress:${egress.catalogVersion}`, provenance("egress_prices"));
    registerPricingProvenance(service.catalogVersion, provenance("service_prices"));
    service.inheritOverrides(getServiceCatalog());
    service.setWorkspaceOverrides(rates.service);

    if (generation !== undefined && generation !== this.overlayGeneration) return false;
    this.targets.pricing.replaceCatalog(artifacts.llm_prices, pricingCandidate.pricingVersion);
    this.targets.replaceCompute(compute);
    this.targets.replaceGpu(gpu);
    setEgressPricingEngine(egress);
    if (this.targets.trackHttp) {
      setServiceCatalog(service);
      setServiceUsageObservers(observers);
    }
    this.snapshot = snapshot;
    this.overlay = overlay;
    return true;
  }

  async refreshOnce(): Promise<CatalogRefreshResult> {
    if (this.refreshPromise) return this.refreshPromise;
    const operation = this.refreshInternal();
    this.refreshPromise = operation;
    try { return await operation; }
    finally { this.refreshPromise = null; }
  }

  private async refreshInternal(): Promise<CatalogRefreshResult> {
    const result = await this.client.refresh();
    if (result.snapshot) {
      const generation = this.overlayGeneration;
      const overlayClient = this.overlayClient;
      const overlayResult = overlayClient
        ? await overlayClient.refresh(result.snapshot.manifest)
        : undefined;
      const overlay = overlayResult?.overlay;
      const changed = this.snapshot?.manifestSha256 !== result.snapshot.manifestSha256
        || this.snapshot?.stale !== result.snapshot.stale
        || this.overlay !== overlay;
      if (changed && !this.apply(result.snapshot, overlay, generation)) {
        this.apply(result.snapshot, this.cachedOverlay(result.snapshot), this.overlayGeneration);
      }
      if (generation === this.overlayGeneration) this.lastOverlayResult = overlayResult;
    }
    this.lastResult = result;
    return result;
  }

  setApiKey(apiKey?: string): void {
    this.overlayClient = apiKey
      ? new CatalogOverlayClient(this.options.endpoint, apiKey, this.store)
      : undefined;
    this.overlayGeneration += 1;
    this.lastOverlayResult = undefined;
    if (this.snapshot) this.apply(this.snapshot, this.cachedOverlay(this.snapshot), this.overlayGeneration);
  }

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    const run = async (): Promise<void> => {
      if (this.stopped) return;
      try { await this.refreshOnce(); } catch { /* refresh is fail-open */ }
      if (this.stopped) return;
      const jitter = 1 + ((Math.random() * 2 - 1) * this.jitterRatio);
      this.timer = setTimeout(() => void run(), this.intervalMs * jitter);
      this.timer.unref?.();
    };
    void run();
  }

  status(): CatalogRuntimeStatus {
    const snapshot = this.snapshot;
    return {
      ...(snapshot ? {
        releaseId: snapshot.manifest.release_id,
        releaseSequence: snapshot.manifest.release_sequence,
      } : {}),
      source: snapshot?.source ?? "bootstrap",
      stale: snapshot?.stale ?? false,
      ...(this.lastResult ? { lastRefreshStatus: this.lastResult.status } : {}),
      ...(this.lastResult?.error ? { lastError: this.lastResult.error } : {}),
      overlayActive: this.overlay !== undefined,
      overlayOverrideCount: this.overlay?.overrides.length ?? 0,
      ...(this.lastOverlayResult ? { overlayLastRefreshStatus: this.lastOverlayResult.status } : {}),
      ...(this.lastOverlayResult?.error ? { overlayLastError: this.lastOverlayResult.error } : {}),
    };
  }

  stop(): void {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }
}
