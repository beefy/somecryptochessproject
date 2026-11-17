/**
 * AMM arithmetic for the DEX page, ported from `sccp/chain/dex.py`.
 *
 * Reserves and amounts are integer base units. The product of two reserves can
 * exceed JavaScript's safe-integer range even when each reserve is valid, so the
 * intermediate arithmetic is `bigint`; only a final output that is already
 * bounded by a reserve is converted back to `number` for the wire. A preview is
 * never trusted: the node recomputes the same curve, and the signed transaction
 * carries a minimum output the chain must honour.
 */

import { DOMAINS, domainHex } from "./canonical";

export const NATIVE_ASSET = "";
export const BPS_DENOMINATOR = 10_000n;

export class DexMathError extends Error {
  override name = "DexMathError";
}

function ensureSafe(value: bigint, what: string): number {
  if (value < 0n || value > BigInt(Number.MAX_SAFE_INTEGER)) {
    throw new DexMathError(`${what} is outside the safe integer range`);
  }
  return Number(value);
}

function positive(value: number, what: string): bigint {
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new DexMathError(`${what} must be a positive integer, got ${value}`);
  }
  return BigInt(value);
}

/** Integer square root, the same floor Python's `math.isqrt` returns. */
function isqrt(value: bigint): bigint {
  if (value < 0n) throw new DexMathError("isqrt needs a non-negative integer");
  if (value < 2n) return value;
  let current = value;
  let next = (current + value / current) / 2n;
  while (next < current) {
    current = next;
    next = (current + value / current) / 2n;
  }
  return current;
}

/** Order a pair the way consensus stores it, carrying each amount with its side. */
export function normalizePair(
  assetA: string | null,
  amountA: number,
  assetB: string | null,
  amountB: number,
): { assetA: string; amountA: number; assetB: string; amountB: number } {
  const first = { asset: assetA ?? NATIVE_ASSET, amount: amountA };
  const second = { asset: assetB ?? NATIVE_ASSET, amount: amountB };
  if (first.asset === second.asset) {
    throw new DexMathError("a pool needs two different assets");
  }
  const [a, b] = first.asset < second.asset ? [first, second] : [second, first];
  return { assetA: a.asset, amountA: a.amount, assetB: b.asset, amountB: b.amount };
}

/** `H("sccp/v1/pool", {asset_a, asset_b})`, independent of side order. */
export function poolId(assetA: string | null, assetB: string | null): string {
  const pair = normalizePair(assetA, 1, assetB, 1);
  return domainHex(DOMAINS.POOL, { asset_a: pair.assetA, asset_b: pair.assetB });
}

/** `isqrt(amountA * amountB)` for a pool's first deposit. */
export function initialShares(amountA: number, amountB: number): number {
  return ensureSafe(isqrt(positive(amountA, "amount_a") * positive(amountB, "amount_b")), "shares");
}

/** Shares for a later deposit, bounded by the scarcer side of the pair. */
export function addedShares(
  reserveA: number,
  reserveB: number,
  totalShares: number,
  amountA: number,
  amountB: number,
): number {
  if (totalShares <= 0) return 0;
  const byA = (positive(amountA, "amount_a") * BigInt(totalShares)) / positive(reserveA, "reserve_a");
  const byB = (positive(amountB, "amount_b") * BigInt(totalShares)) / positive(reserveB, "reserve_b");
  return ensureSafe(byA < byB ? byA : byB, "shares");
}

/** The two reserves a share redemption pays, rounded down like consensus. */
export function removedAmounts(
  reserveA: number,
  reserveB: number,
  totalShares: number,
  shares: number,
): { amountA: number; amountB: number } {
  const total = positive(totalShares, "total_shares");
  const redeemed = positive(shares, "shares");
  if (redeemed > total) throw new DexMathError("cannot remove more shares than the pool has");
  return {
    amountA: ensureSafe((BigInt(reserveA) * redeemed) / total, "amount_a"),
    amountB: ensureSafe((BigInt(reserveB) * redeemed) / total, "amount_b"),
  };
}

/** The output of a fee-paying constant-product swap, rounded down. */
export function swapOutput(
  reserveIn: number,
  reserveOut: number,
  amountIn: number,
  feeBps: number,
): number {
  if (!Number.isSafeInteger(feeBps) || feeBps < 0 || feeBps >= Number(BPS_DENOMINATOR)) {
    throw new DexMathError(`fee ${feeBps} is outside [0, ${BPS_DENOMINATOR})`);
  }
  const afterFee = positive(amountIn, "amount_in") * (BPS_DENOMINATOR - BigInt(feeBps));
  const numerator = afterFee * positive(reserveOut, "reserve_out");
  const denominator = positive(reserveIn, "reserve_in") * BPS_DENOMINATOR + afterFee;
  return ensureSafe(numerator / denominator, "amount_out");
}

/** The B side that keeps `amountA` at the pool's current ratio, rounded up. */
export function proportionalAmount(
  amountA: number,
  reserveA: number,
  reserveB: number,
): number {
  if (amountA <= 0 || reserveA <= 0) return 0;
  const numerator = positive(amountA, "amount_a") * positive(reserveB, "reserve_b");
  return ensureSafe((numerator + BigInt(reserveA) - 1n) / BigInt(reserveA), "amount_b");
}

/** A position's percentage of a pool, e.g. `12.5`. */
export function sharePercent(shares: number, totalShares: number): number {
  if (totalShares <= 0 || shares <= 0) return 0;
  return (shares / totalShares) * 100;
}
