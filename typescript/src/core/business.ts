import { randomUUID } from "node:crypto";
import { Decimal, canonicalDecimal, isoCanonical } from "./models.js";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const CANONICAL_NAME = /^[a-z0-9][a-z0-9._-]{0,127}$/;
const INTEGER = /^-?(?:0|[1-9]\d{0,25})$/;
const DECIMAL = /^-?(?:0|[1-9]\d{0,25})(?:\.\d{1,12})?$/;
const AMOUNT = /^(?:0|[1-9]\d{0,25})(?:\.\d{1,12})?$/;

function requireUuid(value: string, name: string): string {
  if (!UUID.test(value)) throw new Error(`${name} must be a valid UUID`);
  return value.toLowerCase();
}

function requireRevision(value: number, name: string): number {
  if (!Number.isInteger(value) || value < 1 || value > 2_147_483_647) {
    throw new Error(`${name} revision must be between 1 and 2147483647`);
  }
  return value;
}

export type OutcomeState = "pending" | "achieved" | "missed" | "voided";
export type OutcomeValueType = "string" | "boolean" | "integer" | "decimal";
export type OutcomeInput = string | boolean | bigint | number | Decimal;

export interface OutcomeValue {
  type: OutcomeValueType;
  value: string | boolean;
}

export function outcomeValue(value: OutcomeInput): OutcomeValue {
  if (typeof value === "boolean") return { type: "boolean", value };
  if (typeof value === "string") {
    if (value.length < 1 || value.length > 1024) {
      throw new Error("string outcome values must contain 1 to 1024 characters");
    }
    return { type: "string", value };
  }
  if (typeof value === "bigint" || (typeof value === "number" && Number.isSafeInteger(value))) {
    const encoded = String(value);
    if (!INTEGER.test(encoded)) throw new Error("invalid integer outcome value");
    return { type: "integer", value: encoded };
  }
  if (value instanceof Decimal) {
    if (!value.isFinite()) throw new Error("decimal outcome values must be finite");
    const encoded = canonicalDecimal(value);
    if (!DECIMAL.test(encoded)) throw new Error("invalid decimal outcome value");
    return { type: "decimal", value: encoded };
  }
  throw new TypeError("outcome value must be a string, boolean, safe integer, bigint, or Decimal");
}

export interface OutcomeRevisionOptions {
  taskId: string;
  name: string;
  state?: OutcomeState;
  outcomeId?: string;
  revision?: number;
  effectiveAt?: Date;
  observedAt?: Date;
  value?: OutcomeInput | OutcomeValue;
}

export class OutcomeRevision {
  readonly schemaVersion = "1";
  readonly outcomeId: string;
  readonly taskId: string;
  readonly name: string;
  readonly state: OutcomeState;
  readonly revision: number;
  readonly effectiveAt: Date;
  readonly observedAt: Date;
  readonly value?: OutcomeValue;

  constructor(options: OutcomeRevisionOptions) {
    this.taskId = requireUuid(options.taskId, "taskId");
    this.outcomeId = requireUuid(options.outcomeId ?? randomUUID(), "outcomeId");
    if (!CANONICAL_NAME.test(options.name)) throw new Error("outcome name must be a canonical identifier");
    this.name = options.name;
    this.state = options.state ?? "achieved";
    if (!["pending", "achieved", "missed", "voided"].includes(this.state)) {
      throw new Error(`unsupported outcome state ${this.state}`);
    }
    this.revision = requireRevision(options.revision ?? 1, "outcome");
    this.effectiveAt = options.effectiveAt ?? new Date();
    this.observedAt = options.observedAt ?? new Date();
    if (options.value !== undefined) {
      this.value = typeof options.value === "object" && !(options.value instanceof Decimal) &&
        "type" in options.value && "value" in options.value
        ? options.value as OutcomeValue
        : outcomeValue(options.value as OutcomeInput);
    }
    if ((this.state === "pending" || this.state === "voided") && this.value !== undefined) {
      throw new Error(`${this.state} outcomes cannot assert a value`);
    }
    if (this.state === "voided" && this.revision === 1) {
      throw new Error("voided outcomes must supersede an earlier revision");
    }
  }

  toDict(): Record<string, unknown> {
    const result: Record<string, unknown> = {
      schema_version: "1",
      outcome_id: this.outcomeId,
      task_id: this.taskId,
      name: this.name,
      effective_at: isoCanonical(this.effectiveAt),
      observed_at: isoCanonical(this.observedAt),
      lifecycle: { state: this.state, revision: this.revision },
    };
    if (this.value !== undefined) result["value"] = this.value;
    return result;
  }
}

export type RevenueState = "pending" | "provisional" | "recognized" | "voided";
export type RevenueSourceType = "sdk" | "workspace_api" | "import" | "manual";
export type RevenueInput = string | bigint | number | Decimal;

export interface RevenueSource {
  type: RevenueSourceType;
  recordId?: string;
}

export interface RevenueAmount {
  value: Decimal;
  currency: string;
}

export function revenueAmount(value: RevenueInput, currency: string): RevenueAmount {
  if (!/^[A-Z]{3}$/.test(currency)) {
    throw new Error("revenue currency must be a three-letter uppercase code");
  }
  if (typeof value === "number" && !Number.isSafeInteger(value)) {
    throw new TypeError("revenue amount must be a Decimal, safe integer, bigint, or decimal string");
  }
  const exact = value instanceof Decimal ? value : new Decimal(String(value));
  const encoded = canonicalDecimal(exact);
  if (!exact.isFinite() || exact.isNegative() || !AMOUNT.test(encoded)) {
    throw new Error("revenue amount must be a non-negative 26.12 decimal");
  }
  return { value: exact, currency };
}

export interface RevenueRevisionOptions {
  taskId: string;
  state: RevenueState;
  amount?: RevenueAmount;
  source?: RevenueSource;
  outcomeId?: string;
  revenueId?: string;
  revision?: number;
  effectiveAt?: Date;
  observedAt?: Date;
}

export class RevenueRevision {
  readonly schemaVersion = "1";
  readonly revenueId: string;
  readonly taskId: string;
  readonly outcomeId?: string;
  readonly state: RevenueState;
  readonly revision: number;
  readonly effectiveAt: Date;
  readonly observedAt: Date;
  readonly amount?: RevenueAmount;
  readonly source: RevenueSource;

  constructor(options: RevenueRevisionOptions) {
    this.taskId = requireUuid(options.taskId, "taskId");
    this.revenueId = requireUuid(options.revenueId ?? randomUUID(), "revenueId");
    this.outcomeId = options.outcomeId === undefined ? undefined : requireUuid(options.outcomeId, "outcomeId");
    this.state = options.state;
    if (!["pending", "provisional", "recognized", "voided"].includes(this.state)) {
      throw new Error(`unsupported revenue state ${this.state}`);
    }
    this.revision = requireRevision(options.revision ?? 1, "revenue");
    this.effectiveAt = options.effectiveAt ?? new Date();
    this.observedAt = options.observedAt ?? new Date();
    this.amount = options.amount;
    this.source = options.source ?? { type: "sdk" };
    if (!["sdk", "workspace_api", "import", "manual"].includes(this.source.type)) {
      throw new Error(`unsupported revenue source ${this.source.type}`);
    }
    if (this.source.recordId !== undefined &&
        (this.source.recordId.trim() !== this.source.recordId || this.source.recordId.length < 1 || this.source.recordId.length > 256)) {
      throw new Error("revenue source recordId must contain 1 to 256 characters");
    }
    const amountRequired = this.state === "provisional" || this.state === "recognized";
    if (amountRequired && this.amount === undefined) throw new Error(`${this.state} revenue requires an amount`);
    if (!amountRequired && this.amount !== undefined) throw new Error(`${this.state} revenue cannot assert an amount`);
    if (this.state === "voided" && this.revision === 1) {
      throw new Error("voided revenue must supersede an earlier revision");
    }
  }

  toDict(): Record<string, unknown> {
    const source: Record<string, string> = { type: this.source.type };
    if (this.source.recordId !== undefined) source["record_id"] = this.source.recordId;
    const result: Record<string, unknown> = {
      schema_version: "1",
      revenue_id: this.revenueId,
      task_id: this.taskId,
      effective_at: isoCanonical(this.effectiveAt),
      observed_at: isoCanonical(this.observedAt),
      lifecycle: { state: this.state, revision: this.revision },
      source,
    };
    if (this.outcomeId !== undefined) result["outcome_id"] = this.outcomeId;
    if (this.amount !== undefined) {
      result["amount"] = { value: canonicalDecimal(this.amount.value), currency: this.amount.currency };
    }
    return result;
  }
}
