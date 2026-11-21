/**
 * Decimal ZQWX in, integer base units out.
 *
 * Amounts on the wire are integers, so this is the one place a typed number is
 * scaled. Getting it wrong sends a hundred-millionth of what was meant, which is
 * why the refusals matter as much as the conversions.
 */

import { describe, expect, it } from "vitest";

import { UNITS_PER_COIN, formatAmount, formatCoins, formatTokenAmount, parseAmount, parseTokenAmount } from "./format";

describe("parseAmount", () => {
  it("scales whole and fractional coins exactly", () => {
    expect(parseAmount("1")).toBe(UNITS_PER_COIN);
    expect(parseAmount("0.00000001")).toBe(1);
    expect(parseAmount("12.34")).toBe(1_234_000_000);
    expect(parseAmount("1,000.5")).toBe(100_050_000_000);
    expect(parseAmount(" 2.00000001 ")).toBe(200_000_001);
  });

  it("refuses anything that would round", () => {
    expect(() => parseAmount("1.123456789")).toThrow(/8 decimals/);
    expect(() => parseAmount("1e8")).toThrow(/not a ZQWX amount/);
    expect(() => parseAmount("-1")).toThrow(/not a ZQWX amount/);
    expect(() => parseAmount("")).toThrow(/not a ZQWX amount/);
    expect(() => parseAmount("1.")).toThrow(/not a ZQWX amount/);
    expect(() => parseAmount("0.1.2")).toThrow(/not a ZQWX amount/);
  });

  it("refuses an amount too large to be exact", () => {
    expect(() => parseAmount("999999999999999999999")).toThrow(/larger than/);
  });

  it("round-trips through formatAmount", () => {
    for (const text of ["0.00001", "1", "12.34", "1,000.5"]) {
      expect(formatAmount(parseAmount(text))).toBe(
        text.includes(",") ? "1,000.5 ZQWX" : `${text} ZQWX`,
      );
    }
  });

  it("formats a faucet grant in whole coins, not base units", () => {
    expect(formatCoins(1000)).toBe("1,000 ZQWX");
    expect(formatCoins(1)).toBe("1 ZQWX");
    expect(formatCoins(null)).toBe("-");
  });
});

describe("variant token amounts", () => {
  it("scales and prints a token with its own symbol", () => {
    expect(formatTokenAmount(1_234_000_000, "C960", 8)).toBe("12.34 C960");
    expect(parseTokenAmount("12.34", "C960", 8)).toBe(1_234_000_000);
  });

  it("names the token in a parse error", () => {
    expect(() => parseTokenAmount("1.123456789", "C960", 8)).toThrow(/C960/);
    expect(() => parseTokenAmount("1.1", "C960", 0)).toThrow(/C960/);
  });
});
