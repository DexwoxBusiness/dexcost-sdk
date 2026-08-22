import { Decimal, canonicalDecimal } from "./models.js";

export type ToolQuantityInput = Decimal | string | bigint | number;
export type ToolCostInput = Decimal | string | bigint | number;
export type ToolOperationStatus = "succeeded" | "failed" | "cancelled" | "unknown";
export type ToolDimensionInput = string | boolean | bigint | number | Decimal;

const CANONICAL_NAME = /^[a-z0-9][a-z0-9._-]{0,127}$/;
const UNIT = /^[A-Za-z0-9][A-Za-z0-9._{}/*^+\-]{0,63}$/;
const POSITIVE_DECIMAL = /^(?=.*[1-9])(?:0|[1-9]\d{0,25})(?:\.\d{1,12})?$/;

/** One exact, content-free usage meter asserted for a tool invocation. */
export class ToolUsage {
  readonly metric: string;
  readonly quantity: Decimal;
  readonly unit: string;

  constructor(
    metric = "call_count",
    quantity: Decimal = new Decimal(1),
    unit = "Calls",
  ) {
    if (!CANONICAL_NAME.test(metric)) {
      throw new Error("tool usage metric must be a canonical lowercase identifier");
    }
    if (!(quantity instanceof Decimal)) {
      throw new TypeError("tool usage quantity must use Decimal");
    }
    const rendered = canonicalDecimal(quantity);
    if (!quantity.isFinite() || !quantity.gt(0) || !POSITIVE_DECIMAL.test(rendered)) {
      throw new Error("tool usage quantity must be a positive 26.12 decimal");
    }
    if (!UNIT.test(unit)) throw new Error("tool usage unit must be a canonical unit");
    this.metric = metric;
    this.quantity = quantity;
    this.unit = unit;
  }

  static fromInput(
    quantity: ToolQuantityInput = 1,
    options: { metric?: string; unit?: string } = {},
  ): ToolUsage {
    if (typeof quantity === "number" && !Number.isSafeInteger(quantity)) {
      throw new TypeError(
        "tool usage quantity must be a Decimal, safe integer, bigint, or decimal string",
      );
    }
    let exact: Decimal;
    try {
      exact = quantity instanceof Decimal ? quantity : new Decimal(String(quantity));
    } catch {
      throw new Error("tool usage quantity is not a plain decimal");
    }
    return new ToolUsage(options.metric ?? "call_count", exact, options.unit ?? "Calls");
  }
}
