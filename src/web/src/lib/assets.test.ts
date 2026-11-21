import { describe, expect, it } from "vitest";

import type { AccountView, NetworkParams } from "../types";
import {
  tokenBalance,
  tokenForAsset,
  tokenForVariant,
  tokensFor,
  variantLabel,
  variantShort,
} from "./assets";

const params = {
  variants: [
    {
      name: "standard",
      rules: "chess",
      asset: null,
      symbol: "ZQWX",
      display_name: "Standard chess",
      decimals: 8,
      subsidy: 1,
      halving_interval: null,
      max_supply: null,
      min_plies: 1,
      max_games_per_block: 1,
      opening_book: "standard",
    },
    {
      name: "chess960",
      rules: "chess960",
      asset: "a".repeat(64),
      symbol: "C960",
      display_name: "Chess960",
      decimals: 8,
      subsidy: 1,
      halving_interval: 1,
      max_supply: 1,
      min_plies: 20,
      max_games_per_block: 1,
      opening_book: "none",
    },
  ],
} as unknown as NetworkParams;

describe("tokens", () => {
  it("puts native ZQWX first and keeps C960 addressable by id, variant and symbol", () => {
    const tokens = tokensFor(params);
    expect(tokens.map((token) => token.symbol)).toStrictEqual(["ZQWX", "C960"]);
    expect(tokenForAsset(tokens, "a".repeat(64))?.symbol).toBe("C960");
    expect(tokenForVariant(tokens, "chess960")?.symbol).toBe("C960");
    expect(tokenForAsset(tokens, null)?.native).toBe(true);
  });

  it("reads native and asset balances from their separate maps", () => {
    const account = {
      balance: 12,
      immature: 3,
      assets: { ["a".repeat(64)]: 98 },
      immature_assets: { ["a".repeat(64)]: 7 },
    } as unknown as AccountView;
    const [sccp, c960] = tokensFor(params);
    expect(tokenBalance(account, sccp as NonNullable<typeof sccp>)).toBe(12);
    expect(tokenBalance(account, c960 as NonNullable<typeof c960>)).toBe(98);
  });

  it("labels variants for tables before the node has answered", () => {
    expect(variantLabel("standard")).toBe("Standard");
    expect(variantLabel("chess960")).toBe("Chess960");
    expect(variantShort("standard")).toBe("ZQWX");
    expect(variantShort("chess960")).toBe("C960");
    expect(variantLabel(undefined)).toBe("Unknown");
  });
});
