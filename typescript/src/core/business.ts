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

function requireDate(value: Date | undefined, name: string): Date {
  const resolved = value ?? new Date();
  if (!(resolved instanceof Date) || !Number.isFinite(resolved.getTime())) {
    throw new Error(`${name} must be a valid Date`);
  }
  return new Date(resolved.getTime());
}

function requireRecord(value: unknown, name: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${name} must be an object`);
  }
  return value as Record<string, unknown>;
}

function requireWireDate(value: unknown, name: string): Date {
  if (typeof value !== "string") throw new Error(`${name} must be an RFC3339 string`);
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) throw new Error(`${name} must be an RFC3339 string`);
  return parsed;
}

export type OutcomeState = "pending" | "achieved" | "missed" | "voided";
export type OutcomeValueType = "string" | "boolean" | "integer" | "decimal";
export type OutcomeInput = string | boolean | bigint | number | Decimal;

export interface OutcomeValue {
  type: OutcomeValueType;
  value: string | boolean;
}

function normalizeOutcomeValue(value: OutcomeInput | OutcomeValue): OutcomeValue {
  if (value instanceof Decimal || typeof value !== "object" || value === null) {
    return outcomeValue(value as OutcomeInput);
  }
  const candidate = value as { type?: unknown; value?: unknown };
  if (!(["string", "boolean", "integer", "decimal"] as const).includes(candidate.type as OutcomeValueType)) {
    throw new Error("invalid outcome value type");
  }
  const type = candidate.type as OutcomeValueType;
  if (type === "string") {
    if (typeof candidate.value !== "string" || candidate.value.length < 1 || candidate.value.length > 1024) {
      throw new Error("string outcome values must contain 1 to 1024 characters");
    }
  } else if (type === "boolean") {
    if (typeof candidate.value !== "boolean") {
      throw new Error("boolean outcome values must contain a boolean");
    }
  } else {
    if (typeof candidate.value !== "string") {
      throw new Error(`${type} outcome values must use a plain decimal string`);
    }
    const pattern = type === "integer" ? INTEGER : DECIMAL;
    if (!pattern.test(candidate.value)) throw new Error(`invalid ${type} outcome value`);
  }
  return { type, value: candidate.value as string | boolean };
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
    this.effectiveAt = requireDate(options.effectiveAt, "effectiveAt");
    this.observedAt = requireDate(options.observedAt, "observedAt");
    if (options.value !== undefined) {
      this.value = normalizeOutcomeValue(options.value);
    }
    if ((this.state === "pending" || this.state === "voided") && this.value !== undefined) {
      throw new Error(`${this.state} outcomes cannot assert a value`);
    }
    if (this.state === "voided" && this.revision === 1) {
      throw new Error("voided outcomes must supersede an earlier revision");
    }
  }

  static fromDict(data: Record<string, unknown>): OutcomeRevision {
    if (data["schema_version"] !== "1") throw new Error("outcome schema_version must be '1'");
    const lifecycle = requireRecord(data["lifecycle"], "outcome lifecycle");
    const rawValue = data["value"];
    return new OutcomeRevision({
      outcomeId: String(data["outcome_id"]),
      taskId: String(data["task_id"]),
      name: String(data["name"]),
      state: String(lifecycle["state"]) as OutcomeState,
      revision: Number(lifecycle["revision"]),
      effectiveAt: requireWireDate(data["effective_at"], "effective_at"),
      observedAt: requireWireDate(data["observed_at"], "observed_at"),
      value: rawValue === undefined
        ? undefined
        : normalizeOutcomeValue(requireRecord(rawValue, "outcome value") as unknown as OutcomeValue),
    });
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
  if ((typeof value === "number" && !Number.isSafeInteger(value)) ||
      (typeof value !== "string" && typeof value !== "number" && typeof value !== "bigint" && !(value instanceof Decimal))) {
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
    this.effectiveAt = requireDate(options.effectiveAt, "effectiveAt");
    this.observedAt = requireDate(options.observedAt, "observedAt");
    this.amount = options.amount === undefined
      ? undefined
      : revenueAmount(options.amount.value as unknown as RevenueInput, options.amount.currency);
    const source = options.source ?? { type: "sdk" };
    this.source = { type: source.type, recordId: source.recordId };
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

  static fromDict(data: Record<string, unknown>): RevenueRevision {
    if (data["schema_version"] !== "1") throw new Error("revenue schema_version must be '1'");
    const lifecycle = requireRecord(data["lifecycle"], "revenue lifecycle");
    const source = requireRecord(data["source"], "revenue source");
    const rawAmount = data["amount"];
    let amount: RevenueAmount | undefined;
    if (rawAmount !== undefined) {
      const encoded = requireRecord(rawAmount, "revenue amount");
      if (typeof encoded["value"] !== "string" || typeof encoded["currency"] !== "string") {
        throw new Error("revenue amount must contain string value and currency");
      }
      amount = revenueAmount(encoded["value"], encoded["currency"]);
    }
    const recordId = source["record_id"];
    if (recordId !== undefined && typeof recordId !== "string") {
      throw new Error("revenue source record_id must be a string");
    }
    return new RevenueRevision({
      revenueId: String(data["revenue_id"]),
      taskId: String(data["task_id"]),
      outcomeId: data["outcome_id"] === undefined ? undefined : String(data["outcome_id"]),
      state: String(lifecycle["state"]) as RevenueState,
      revision: Number(lifecycle["revision"]),
      effectiveAt: requireWireDate(data["effective_at"], "effective_at"),
      observedAt: requireWireDate(data["observed_at"], "observed_at"),
      amount,
      source: { type: String(source["type"]) as RevenueSourceType, recordId },
    });
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
