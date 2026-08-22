/**
 * Shared network finalization — v1 byte aggregates + v2 egress pricing.
 *
 * Extracted from TrackedTask._finalizeNetwork so that EVERY task kind can
 * drain its NetworkAccountant into priced byte aggregates, not just explicit
 * `tracker.track()` tasks:
 *
 * - TrackedTask.end()            → explicit tasks
 * - SessionManager finalization  → ambient "agent_session" tasks
 * - finalizeAutoTask()           → instrument / HTTP-adapter auto-tasks
 *
 * Before this existed, only TrackedTask ran egress pricing; auto and session
 * tasks (the default mode for un-wrapped apps) always shipped with
 * network_cost_usd = 0 even though their bytes were recorded.
 */

import { Decimal } from "./models.js";
import type { Task, CostConfidence } from "./models.js";
import { unregisterAccountant } from "../adapters/network-accountant.js";
import { EgressPricingEngine } from "../pricing/egress-pricing.js";
import type { RateRegistry } from "../pricing/rates.js";
import { getCloudEnv } from "../cloud-detect.js";
import type { EventBuffer } from "../transport/buffer.js";

/** 1 GB = 10^9 bytes (decimal, per spec §6.3 — NOT 2^30). */
const GB_BYTES = new Decimal("1000000000");

/**
 * Module-level engine singleton. TrackedTask.end() used to construct a
 * fresh engine (catalog read) per task, which was fine at track() cadence;
 * auto-tasks finalize per LLM/HTTP call, so the catalog is loaded once.
 */
let _egressEngine: EgressPricingEngine | null = null;
const _rateRegistries = new WeakMap<EventBuffer, RateRegistry>();

/** Atomically install the egress engine from a validated catalog release. */
export function setEgressPricingEngine(engine: EgressPricingEngine | null): void {
  _egressEngine = engine;
}

/** Bind a tracker's exact rate registry to its durable buffer. */
export function setNetworkRateRegistry(buffer: EventBuffer, registry: RateRegistry): void {
  _rateRegistries.set(buffer, registry);
}

function _engine(): EgressPricingEngine {
  if (_egressEngine === null) {
    _egressEngine = new EgressPricingEngine();
  }
  return _egressEngine;
}

/**
 * Drain the task's NetworkAccountant into the task's v1 byte fields and
 * compute v2 egress dollars.
 *
 * No-op when no accountant was ever registered for the task. When a
 * `buffer` is provided, `cost_pending` network events for the task are
 * back-filled with their egress cost and re-synced.
 *
 * Callers are responsible for persisting the task afterwards (upsertTask)
 * and for wrapping this in their own fail-silent shell where required.
 */
export function finalizeTaskNetwork(
  task: Task,
  buffer?: EventBuffer,
  rateRegistry?: RateRegistry,
): void {
  rateRegistry ??= buffer === undefined ? undefined : _rateRegistries.get(buffer);
  // v1 — drain the accountant into task fields. Lookup-then-unregister:
  // late HTTP calls attributed to this task_id won't find an accountant
  // (no orphan rows; matches Python frozen-then-snapshot).
  const accountant = unregisterAccountant(task.taskId);
  if (!accountant) {
    // No accountant was registered (ad-hoc task creation outside the
    // SDK's creation paths); v1 fields stay at zero.
    return;
  }
  const snapshot = accountant.finalize();
  task.networkBytesIn = snapshot.bytesIn;
  task.networkBytesOut = snapshot.bytesOut;
  task.networkCallCount = snapshot.callCount;
  task.networkByHost = snapshot.byHost as Record<string, unknown>;

  // Fast path: no HTTP call ever touched this task (common for
  // instrument auto-tasks in un-patched-fetch environments). Zero bytes
  // means zero egress and no cost_pending network events to back-fill —
  // skip the cloud-env probe and catalog resolution entirely.
  if (snapshot.callCount === 0 && snapshot.bytesIn === 0 && snapshot.bytesOut === 0) {
    task.networkCostUsd = new Decimal(0);
    return;
  }

  // v2 — egress pricing.
  const env = getCloudEnv();
  let customRate = undefined;
  const networkKeys = env.provider === null
    ? ["local"]
    : [env.region ? `${env.provider}:${env.region}` : "", env.provider];
  for (const key of networkKeys) {
    if (!key) continue;
    customRate = rateRegistry?.getInfrastructure("network", key);
    if (customRate !== undefined) break;
  }

  let ratePerGb: Decimal;
  let billingUnit: "gb_transferred" | "gb_egress";
  let pricingSource: string;
  let costConfidence: CostConfidence;
  let pricingVersion: string | undefined;
  if (customRate !== undefined) {
    ratePerGb = customRate.costUsd;
    billingUnit = customRate.per as "gb_transferred" | "gb_egress";
    pricingSource = "rate_registry";
    costConfidence = "computed";
    pricingVersion = rateRegistry!.pricingVersion;
  } else if (env.provider === null) {
    // A local transfer has no defensible public cloud egress rate. Keep the
    // bytes observable and leave dollars at zero until the user supplies an
    // explicit `network.local` infrastructure rate.
    ratePerGb = new Decimal(0);
    billingUnit = "gb_egress";
    pricingSource = "unpriced:local_network";
    costConfidence = "unknown";
    pricingVersion = undefined;
  } else {
    const engine = _engine();
    const rate = engine.resolveRate(env.provider, env.region);
    ratePerGb = new Decimal(rate.ratePerGb);
    billingUnit = (rate.billingUnit ?? "gb_egress") as "gb_transferred" | "gb_egress";
    pricingSource = rate.pricingSource;
    costConfidence = rate.costConfidence as CostConfidence;
    pricingVersion = `egress:${engine.catalogVersion}`;
  }

  // Convert external_bytes_out (number) to GB. Per spec §6.3 — 1 GB
  // = 10^9 bytes, NOT 2^30. Exact decimal math: the catalog stores rates
  // as strings (exactness at rest), bytes are integers, and the GB divisor
  // is exact — so every egress dollar is an exact Decimal (mirrors Python).
  const billableBytes = billingUnit === "gb_transferred"
    ? snapshot.bytesIn + snapshot.bytesOut
    : snapshot.externalBytesOut;
  const networkCostUsd = new Decimal(billableBytes)
    .dividedBy(GB_BYTES)
    .times(ratePerGb);
  task.networkCostUsd = networkCostUsd;

  // Stamp per-host egress_cost_usd into network_by_host[].hosts. The
  // per-host external_bytes_out survives the LIVE_CAP overflow + top-N
  // cap; sum(per-host egress_cost_usd) == network_cost_usd by
  // construction (v2 §10.3 property invariant 2).
  const byHost = task.networkByHost as {
    hosts?: Array<Record<string, unknown>>;
  };
  if (Array.isArray(byHost.hosts)) {
    for (const host of byHost.hosts) {
      const hostBillable = billingUnit === "gb_transferred"
        ? ((host["bytes_in"] as number) ?? 0) + ((host["bytes_out"] as number) ?? 0)
        : ((host["external_bytes_out"] as number) ?? 0);
      const hostCost = new Decimal(hostBillable)
        .dividedBy(GB_BYTES)
        .times(ratePerGb);
      host["egress_cost_usd"] = hostCost.toString();
    }
  }

  // v2 §6.4 — back-fill each network event for this task. Walk the
  // buffer's stored events, find any with details.cost_pending ===
  // true, compute their cost, strip the marker, and updateEvent to
  // re-sync.
  if (buffer) {
    const stored = buffer.queryEvents(task.taskId);
    for (const ev of stored) {
      if (ev.eventType !== "network") continue;
      if (ev.details?.cost_pending !== true) continue;
      const reqBytes = (ev.details?.request_bytes as number) ?? 0;
      const respBytes = (ev.details?.response_bytes as number) ?? 0;
      const isInternal = ev.details?.is_internal_traffic === true;
      // From the customer's HTTP-client perspective request bytes leave the
      // cloud and response bytes enter it. Public egress rates apply only to
      // the outbound request bytes; the server will independently price the
      // disjoint bytes_out usage bucket.
      const billable = billingUnit === "gb_transferred"
        ? reqBytes + respBytes
        : (isInternal ? 0 : reqBytes);
      const evCost = new Decimal(billable).dividedBy(GB_BYTES).times(ratePerGb);

      ev.costUsd = evCost;
      ev.costConfidence = isInternal
        && billingUnit === "gb_egress"
        ? "exact"
        : costConfidence;
      ev.pricingSource = (
        isInternal && billingUnit === "gb_egress"
          ? "egress_catalog:internal"
          : pricingSource
      ) as typeof ev.pricingSource;
      ev.pricingVersion = pricingVersion;
      // Strip cost_pending marker so the back-filled event is no longer
      // "deferred-cost".
      delete (ev.details as Record<string, unknown>).cost_pending;
      // Stamp egress_pricing_source so the wire payload carries the v2
      // source detail (egress_catalog:aws:us-east-1).
      (ev.details as Record<string, unknown>).egress_pricing_source =
        isInternal && billingUnit === "gb_egress"
          ? "egress_catalog:internal"
          : pricingSource;
      (ev.details as Record<string, unknown>).cloud_provider = env.provider;
      (ev.details as Record<string, unknown>).cloud_region = env.region;

      buffer.updateEvent(ev);

      // The event is transfer-level evidence for the same dollars represented
      // by task.networkCostUsd. Do not add it here: the authoritative scalar
      // below is rolled up exactly once (Python parity; no double charge).
    }
  }

  // Network egress is a cost dimension of the task; roll it into the
  // total so LLM + External + Compute + Network + GPU sum holds.
  task.totalCostUsd = task.totalCostUsd.plus(task.networkCostUsd);
}
