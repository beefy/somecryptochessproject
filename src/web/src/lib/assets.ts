/**
 * Tokens, variants and account balances.
 *
 * The network decides both the chess variants it settles and the asset each one
 * mints (`GET /v1/params`). Standard chess is native SCCP; every other variant
 * has an immutable asset id and a printable symbol such as C960. The explorer and
 * wallet keep those two namespaces apart: a transfer of C960 pays its fee in
 * SCCP, and an account's `assets` map is keyed by the asset id, never a nickname.
 */

import type {
  AccountView,
  NetworkParams,
  VariantName,
  VariantParams,
} from "../types";

/** One tradeable token, native SCCP included. */
export interface Token {
  /** The variant whose proofs mint this token. */
  variant: VariantName;
  /** `null` for native SCCP; a 64-hex asset id otherwise. */
  asset: string | null;
  symbol: string;
  name: string;
  decimals: number;
  native: boolean;
  subsidy: number;
  minPlies: number;
}

const SCCP: Token = {
  variant: "standard",
  asset: null,
  symbol: "SCCP",
  name: "Standard chess",
  decimals: 8,
  native: true,
  subsidy: 0,
  minPlies: 0,
};

/** Project one network variant onto the token the UI spends and displays. */
export function tokenForVariantParams(variant: VariantParams): Token {
  return {
    variant: variant.name,
    asset: variant.asset,
    symbol: variant.symbol,
    name: variant.display_name,
    decimals: variant.decimals,
    native: variant.asset === null,
    subsidy: variant.subsidy,
    minPlies: variant.min_plies,
  };
}

/** Every token this network supports, native SCCP first. */
export function tokensFor(params: NetworkParams | undefined): Token[] {
  if (params === undefined) return [SCCP];
  const tokens = (params.variants ?? []).map(tokenForVariantParams);
  if (!tokens.some((token) => token.native)) tokens.unshift(SCCP);
  return tokens.sort((left, right) => Number(right.native) - Number(left.native));
}

/** The token a variant mints, or `undefined` if this network does not settle it. */
export function tokenForVariant(
  tokens: readonly Token[],
  variant: VariantName,
): Token | undefined {
  return tokens.find((token) => token.variant === variant);
}

/** The token an asset id names; `null` is native SCCP. */
export function tokenForAsset(
  tokens: readonly Token[],
  asset: string | null | undefined,
): Token | undefined {
  if (asset === null || asset === undefined || asset === "") return tokens.find((token) => token.native);
  return tokens.find((token) => token.asset === asset);
}

/** Spendable balance of `token` for `account`, in that token's base units. */
export function tokenBalance(account: AccountView, token: Token): number {
  return token.native ? account.balance : (account.assets?.[token.asset ?? ""] ?? 0);
}

/** Immature (not yet spendable) balance of `token`, in its base units. */
export function tokenImmature(account: AccountView, token: Token): number {
  return token.native ? account.immature : (account.immature_assets?.[token.asset ?? ""] ?? 0);
}

/** A title-cased label for a variant name, used before params have loaded. */
export function variantLabel(variant: VariantName | null | undefined): string {
  if (variant === null || variant === undefined || variant === "") return "Unknown";
  if (variant === "standard") return "Standard";
  if (variant === "chess960") return "Chess960";
  return variant.replace(/(^|[_-])([a-z])/g, (_, prefix: string, letter: string) =>
    `${prefix === "" ? "" : " "}${letter.toUpperCase()}`,
  );
}

/** A short label suitable for a table cell or pill. */
export function variantShort(variant: VariantName | null | undefined): string {
  if (variant === "standard") return "SCCP";
  if (variant === "chess960") return "C960";
  return variantLabel(variant);
}
