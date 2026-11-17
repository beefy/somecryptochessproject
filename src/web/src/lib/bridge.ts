/**
 * ETH<->SCCP bridge helpers: hashlocks, signed orders and the tiny slice of
 * Ethereum ABI the `EthHtlc` contract needs.
 *
 * ETH amounts are `bigint`/decimal strings throughout. One wei is 10^-18 ETH,
 * so a useful amount crosses JavaScript's safe-integer boundary immediately;
 * parsing it into a `number` would sign a different amount than the user saw.
 * SCCP amounts stay integers in base units because 21M SCCP is comfortably
 * inside the safe range.
 */

import { keccak_256 } from "@noble/hashes/sha3.js";
import { sha256 } from "@noble/hashes/sha2.js";

import type { OrderView, TakeView } from "../types";
import { DOMAINS, domainHex, fromHex, toHex } from "./canonical";
import { hybridPublicKeyHex, hybridSign, type HybridKeypair } from "./keys";

export const ETH_DECIMALS = 18;
export const WEI_PER_ETH = 10n ** 18n;
const ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const DIGEST = /^[0-9a-f]{64}$/;
const TX_HASH = /^0x[0-9a-fA-F]{64}$/;

export class BridgeError extends Error {
  override name = "BridgeError";
}

/** A fresh 32-byte secret, as lowercase hex. */
export function randomSecret(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return toHex(bytes);
}

/** `sha256(preimage)`: the hashlock both chains verify. */
export function hashlockOf(preimage: string): string {
  if (!DIGEST.test(preimage)) throw new BridgeError("a preimage must be 32-byte lowercase hex");
  return toHex(sha256(fromHex(preimage)));
}

/** Parse a decimal ETH amount to wei exactly; no floats, exponents or rounding. */
export function parseEthToWei(text: string): bigint {
  const trimmed = text.trim().replace(/,/g, "");
  const match = /^([0-9]+)(?:[.]([0-9]{0,18}))?$/.exec(trimmed);
  if (match === null) throw new BridgeError("enter an ETH amount with at most 18 decimals");
  const whole = BigInt(match[1] ?? "0");
  const fraction = (match[2] ?? "").padEnd(ETH_DECIMALS, "0");
  return whole * WEI_PER_ETH + BigInt(fraction === "" ? "0" : fraction);
}

/** Format wei as ETH, trimming trailing zeroes but keeping at least one decimal. */
export function formatWei(value: bigint | string, digits = 8): string {
  const wei = typeof value === "string" ? BigInt(value) : value;
  const whole = wei / WEI_PER_ETH;
  const fraction = (wei % WEI_PER_ETH).toString().padStart(ETH_DECIMALS, "0");
  const shown = fraction.slice(0, Math.max(1, Math.min(ETH_DECIMALS, digits))).replace(/0+$/, "");
  return `${whole.toString()}.${shown === "" ? "0" : shown}`;
}

/** Wei as a 0x-prefixed 32-byte hexadecimal word (the JSON-RPC `value`). */
export function weiToHex(value: bigint): string {
  if (value < 0n) throw new BridgeError("wei cannot be negative");
  return `0x${value.toString(16)}`;
}

/** ETH per SCCP, from an order, as a display string with eight decimals. */
export function orderPrice(order: Pick<OrderView, "give_amount" | "want_amount">): string {
  if (order.give_amount <= 0) return "-";
  const scaled = (BigInt(order.want_amount) * 100_000_000n) / BigInt(order.give_amount);
  return formatWei(scaled, 8);
}

export function isEthAddress(value: string): boolean {
  return ADDRESS.test(value) && BigInt(value) !== 0n;
}

export function isTxHash(value: string): boolean {
  return TX_HASH.test(value);
}

/**
 * The SCCP height a taker should name when the ETH leg unlocks in `ethSeconds`.
 *
 * The second leg (SCCP) must expire *before* the first leg (ETH), leaving the
 * maker `margin` blocks to claim ETH after a last-moment SCCP claim. If it
 * expired after the ETH leg, a taker could wait until the ETH timelock passed,
 * claim SCCP at its last valid height, and then refund the ETH: the maker would
 * lose the SCCP. The same rule in reverse protects the taker if the maker stalls.
 */
export function sccpTimelockHeight(
  currentHeight: number,
  ethSeconds: number,
  targetBlockSeconds: number,
  margin: number,
  minimum: number,
): number {
  const blocksUntilEth = Math.floor(
    Math.max(0, ethSeconds) / Math.max(1, targetBlockSeconds),
  );
  const safeBlocks = blocksUntilEth - Math.max(1, margin);
  if (safeBlocks < 1) {
    throw new BridgeError(
      `the ETH timelock is too short for ${margin} SCCP blocks of margin; choose a longer lock`,
    );
  }
  const safeHeight = currentHeight + safeBlocks;
  if (safeHeight < minimum) {
    throw new BridgeError(
      `the order asks for SCCP height ${minimum}, but this ETH timelock is only safe up to ${safeHeight}`,
    );
  }
  return safeHeight;
}

interface OrderBody {
  network: string;
  maker_sccp: string;
  maker_eth: string;
  give_asset: "SCCP";
  give_amount: number;
  want_asset: "ETH";
  want_amount: string;
  min_sccp_timelock: number;
  expiry: number;
  nonce: number;
}

export function orderId(body: OrderBody | OrderView): string {
  return domainHex(DOMAINS.ORDER, {
    network: body.network,
    maker_sccp: body.maker_sccp,
    maker_eth: body.maker_eth,
    give_asset: body.give_asset,
    give_amount: body.give_amount,
    want_asset: body.want_asset,
    want_amount: body.want_amount,
    min_sccp_timelock: body.min_sccp_timelock,
    expiry: body.expiry,
    nonce: body.nonce,
  });
}

export interface OrderFields {
  network: string;
  makerSccp: string;
  makerEth: string;
  giveAmount: number;
  wantAmount: string;
  minSccpTimelock: number;
  expiry: number;
  nonce: number;
}

/** A sell-SCCP order signed over `H("sccp/v1/order", body)`. */
export function signBridgeOrder(
  keypair: HybridKeypair,
  fields: OrderFields,
): Omit<OrderView, "order_id"> {
  if (!isEthAddress(fields.makerEth)) throw new BridgeError("maker ETH address is malformed");
  if (!/^[1-9][0-9]*$/.test(fields.wantAmount)) {
    throw new BridgeError("the ETH price must be a positive integer amount of wei");
  }
  const body: OrderBody = {
    network: fields.network,
    maker_sccp: fields.makerSccp,
    maker_eth: fields.makerEth,
    give_asset: "SCCP",
    give_amount: fields.giveAmount,
    want_asset: "ETH",
    want_amount: fields.wantAmount,
    min_sccp_timelock: fields.minSccpTimelock,
    expiry: fields.expiry,
    nonce: fields.nonce,
  };
  const digest = orderId(body);
  return {
    ...body,
    public_key: hybridPublicKeyHex(keypair),
    signature: hybridSign(keypair, fromHex(digest)),
  };
}

interface TakeBody {
  network: string;
  order_id: string;
  taker_sccp: string;
  taker_eth: string;
  hashlock: string;
  eth_timelock: number;
  sccp_timelock_height: number;
  eth_tx_hash: string;
  nonce: number;
}

export function takeId(body: TakeBody | TakeView): string {
  return domainHex(DOMAINS.TAKE, {
    network: body.network,
    order_id: body.order_id,
    taker_sccp: body.taker_sccp,
    taker_eth: body.taker_eth,
    hashlock: body.hashlock,
    eth_timelock: body.eth_timelock,
    sccp_timelock_height: body.sccp_timelock_height,
    eth_tx_hash: body.eth_tx_hash,
    nonce: body.nonce,
  });
}

export interface TakeFields {
  network: string;
  orderId: string;
  takerSccp: string;
  takerEth: string;
  hashlock: string;
  ethTimelock: number;
  sccpTimelockHeight: number;
  ethTxHash: string;
  nonce: number;
}

/** A take signed over `H("sccp/v1/take", body)`. */
export function signBridgeTake(
  keypair: HybridKeypair,
  fields: TakeFields,
): Omit<TakeView, "take_id"> {
  if (!DIGEST.test(fields.orderId) || !DIGEST.test(fields.hashlock)) {
    throw new BridgeError("the order id and hashlock must be 32-byte lowercase hex");
  }
  if (!isTxHash(fields.ethTxHash)) throw new BridgeError("the ETH transaction hash is malformed");
  if (!isEthAddress(fields.takerEth)) throw new BridgeError("taker ETH address is malformed");
  const body: TakeBody = {
    network: fields.network,
    order_id: fields.orderId,
    taker_sccp: fields.takerSccp,
    taker_eth: fields.takerEth,
    hashlock: fields.hashlock,
    eth_timelock: fields.ethTimelock,
    sccp_timelock_height: fields.sccpTimelockHeight,
    eth_tx_hash: fields.ethTxHash,
    nonce: fields.nonce,
  };
  const digest = takeId(body);
  return {
    ...body,
    public_key: hybridPublicKeyHex(keypair),
    signature: hybridSign(keypair, fromHex(digest)),
  };
}

