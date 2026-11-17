import { describe, expect, it } from "vitest";

import {
  addedShares,
  initialShares,
  normalizePair,
  poolId,
  proportionalAmount,
  removedAmounts,
  sharePercent,
  swapOutput,
} from "./dex";

const C960 = "a".repeat(64);

describe("DEX arithmetic", () => {
  it("orders a pair by asset key and carries each amount", () => {
    expect(normalizePair(C960, 200, null, 100)).toEqual({
      assetA: "",
      amountA: 100,
      assetB: C960,
      amountB: 200,
    });
    expect(poolId(C960, null)).toBe(poolId(null, C960));
    expect(poolId(C960, null)).toHaveLength(64);
  });

  it("mints initial shares with floor sqrt", () => {
    expect(initialShares(10_000, 40_000)).toBe(20_000);
    expect(initialShares(1, 1)).toBe(1);
    expect(initialShares(1, 2)).toBe(1);
  });

  it("bounds added shares by the scarcer side", () => {
    expect(addedShares(100, 200, 1_000, 10, 20)).toBe(100);
    expect(addedShares(100, 200, 1_000, 10, 10)).toBe(50);
  });

  it("pays a proportional redemption, rounding down", () => {
    expect(removedAmounts(101, 203, 10, 5)).toEqual({ amountA: 50, amountB: 101 });
    expect(removedAmounts(101, 203, 10, 10)).toEqual({ amountA: 101, amountB: 203 });
  });

  it("matches the constant-product formula with a 30 bp fee", () => {
    // 1_000 in, 10_000/20_000 reserves, 0.30% fee:
    // floor(1000*9970*20000 / (10000*10000 + 1000*9970)) = 1813
    expect(swapOutput(10_000, 20_000, 1_000, 30)).toBe(1_813);
    expect(swapOutput(10_000, 20_000, 1_000, 30)).toBeLessThan(2_000);
  });

  it("computes the proportional second side and a readable share", () => {
    expect(proportionalAmount(1_000, 10_000, 20_000)).toBe(2_000);
    expect(proportionalAmount(1, 3, 10)).toBe(4);
    expect(sharePercent(25, 100)).toBe(25);
    expect(sharePercent(0, 0)).toBe(0);
  });

  it("refuses a zero or unsafe amount rather than signing it", () => {
    expect(() => initialShares(0, 1)).toThrow();
    expect(() => swapOutput(0, 1, 1, 30)).toThrow();
    expect(() => swapOutput(1, 1, 1, 10_000)).toThrow();
  });
});
