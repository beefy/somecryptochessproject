/**
 * Ledger transactions, ported from `sccp/chain/tx.py` and
 * `sccp/wallet/service.py`.
 *
 * The envelope is one canonical body — `{network, type, from, nonce, fee,
 * payload}` — and the signature is the hybrid Ed25519 + ML-DSA-65 blob over
 * `H("sccp/v1/tx", body)`. The payload's shape depends on `type` and is
 * validated by the chain, so these builders only fill in the fields a seat needs
 * to join a roster: `register`, `availability` and `reveal`.
 *
 * Nothing here is specific to a browser: the same bytes `sccp miner register`
 * produces come out of {@link buildRegister}, and {@link signTransaction} signs
 * them with the same wire format `sccp wallet` does, which is what lets a seat
 * created in this tab be paired by the relay like any other.
 *
 * Every transaction carries a hybrid Ed25519 + ML-DSA-65 signature: there is one
 * scheme and no classical fallback on the ledger (`PROTOCOL.md` section 2).
 */

import { DOMAINS, domainHex, fromHex } from "./canonical";
import {
  HYBRID_SIGNATURE_HEX_LENGTH,
  HYBRID_SIGNATURE_SIZE,
  type HybridKeypair,
  hybridSign,
} from "./keys";

/** Wire spellings from `sccp.protocol.models.TxType`. */
export type TxType =
  | "transfer"
  | "register"
  | "register_witness"
  | "availability"
  | "reveal"
  | "create_pool"
  | "add_liquidity"
  | "remove_liquidity"
  | "swap"
  | "htlc_lock"
  | "htlc_claim"
  | "htlc_refund";

/** A signed ledger transaction, in wire order (`from` is the sender field). */
export interface Transaction {
  network: string;
  type: TxType;
  from: string;
  nonce: number;
  fee: number;
  payload: Record<string, unknown>;
  /** 6748 hex characters (3374 bytes: scheme || ed25519 || mldsa65), or absent. */
  signature?: string;
}

export class TxError extends Error {
  override name = "TxError";
}

/** Hex length of a hybrid Ed25519 + ML-DSA-65 signature (3374 bytes). */
export const LEDGER_SIGNATURE_HEX_LENGTH = HYBRID_SIGNATURE_HEX_LENGTH;

/** Bytes in the same signature: `scheme || ed25519(64) || mldsa65(3309)`. */
export const LEDGER_SIGNATURE_SIZE = HYBRID_SIGNATURE_SIZE;

/**
 * Whether `signature` is a well-formed ledger signature.
 *
 * The scheme byte is `0x01`, so a hybrid signature starts with `"01"`. There is
 * no second shape to tell it apart from: an Ed25519-only 128-hex string is not a
 * ledger signature at all any more (`sccp.protocol.models.LedgerSignatureHex`).
 */
export function isLedgerSignatureHex(signature: string | null | undefined): boolean {
  return (
    typeof signature === "string" &&
    signature.length === LEDGER_SIGNATURE_HEX_LENGTH &&
    /^01[0-9a-f]*$/.test(signature)
  );
}

/** The canonical signed body: everything but the signature. */
export function txBody(tx: Transaction): Record<string, unknown> {
  return {
    network: tx.network,
    type: tx.type,
    from: tx.from,
    nonce: tx.nonce,
    fee: tx.fee,
    payload: tx.payload,
  };
}

/** `H("sccp/v1/tx", body_without_signature)` as lowercase hex. */
export function txId(tx: Transaction): string {
  return domainHex(DOMAINS.TX, txBody(tx));
}

/**
 * Sign a transaction with the account's hybrid key.
 *
 * `sccp.chain.tx.sign_transaction` signs the hex *txid*, not the JSON and not a
 * domain hash — the domain was already folded in when the body was hashed — so
 * this signs the same 32 bytes and returns the 6748-hex hybrid blob.
 */
export function signTransaction(keypair: HybridKeypair, tx: Transaction): Transaction {
  return { ...tx, signature: hybridSign(keypair, fromHex(txId(tx))) };
}

/** The exact JSON body a node's `POST /v1/tx` expects. */
export interface WireTransaction extends Omit<Transaction, "signature"> {
  signature: string | null;
}

/** Convert a signed transaction into its wire form (`signature: null` when unsigned). */
export function toWireTransaction(tx: Transaction): WireTransaction {
  return { ...tx, signature: tx.signature ?? null };
}

/** `H("sccp/v1/beacon-commit", {address, epoch, nonce})` as hex. */
export function availabilityCommitment(address: string, epoch: number, nonce: string): string {
  if (!Number.isSafeInteger(epoch) || epoch < 0) throw new TxError(`bad epoch ${epoch}`);
  if (!/^[0-9a-f]{64}$/.test(nonce)) throw new TxError("the nonce must be 32 bytes of lowercase hex");
  return domainHex(DOMAINS.BEACON_COMMIT, { address, epoch, nonce });
}

export interface RegisterFields {
  network: string;
  sender: string;
  nonce: number;
  fee: number;
  bond: number;
  endpoint: string;
  engine: string;
  publicKeyHex: string;
  /** `standard` is the protocol-v1 default and is omitted from the payload. */
  variant?: string;
}

/** The `register` transaction: a bond, an endpoint and an engine fingerprint. */
export function buildRegister(fields: RegisterFields): Transaction {
  const payload: Record<string, unknown> = {
    public_key: fields.publicKeyHex.toLowerCase(),
    endpoint: fields.endpoint,
    engine: fields.engine,
    bond: fields.bond,
  };
  if (fields.variant !== undefined && fields.variant !== "standard") {
    payload.variant = fields.variant;
  }
  return {
    network: fields.network,
    type: "register",
    from: fields.sender,
    nonce: fields.nonce,
    fee: fields.fee,
    payload,
  };
}

export interface AvailabilityFields {
  network: string;
  sender: string;
  nonce: number;
  fee: number;
  /** The epoch being committed for: `current + availability_lead`. */
  epoch: number;
  maxGames: number;
  commitment: string;
  publicKeyHex: string;
}

/** The `availability` transaction: a commitment for the epoch two ahead. */
export function buildAvailability(fields: AvailabilityFields): Transaction {
  return {
    network: fields.network,
    type: "availability",
    from: fields.sender,
    nonce: fields.nonce,
    fee: fields.fee,
    payload: {
      public_key: fields.publicKeyHex.toLowerCase(),
      epoch: fields.epoch,
      max_games: fields.maxGames,
      commitment: fields.commitment,
    },
  };
}

export interface RevealFields {
  network: string;
  sender: string;
  nonce: number;
  fee: number;
  /** The epoch the commitment was made for; a reveal is only valid at `epoch - 1`. */
  epoch: number;
  nonceSecret: string;
  publicKeyHex: string;
}

/** The `transfer` transaction that moves SCCP between accounts. */
export interface TransferFields {
  network: string;
  sender: string;
  nonce: number;
  fee: number;
  /** Recipient, a bech32 address on the same network. */
  to: string;
  /** Base units (one token unit = 10^-decimals), always positive. */
  amount: number;
  publicKeyHex: string;
  /** A non-native asset id; omit for SCCP so the v1 payload stays byte-identical. */
  asset?: string | null;
}

/**
 * The `transfer` transaction: `{to, amount, asset?, public_key}`, exactly the
 * payload `sccp.wallet.service.WalletService.build_transfer` signs.
 *
 * The chain's `_check_unknown_keys` makes this set strict, so nothing else may
 * be added here without a protocol change (`docs/BIG_PICTURE.md` section 5).
 * A token transfer still pays its fee in native SCCP.
 */
export function buildTransfer(fields: TransferFields): Transaction {
  if (!Number.isSafeInteger(fields.amount) || fields.amount <= 0) {
    throw new TxError(`a transfer amount must be a positive integer, got ${fields.amount}`);
  }
  const payload: Record<string, unknown> = {
    to: fields.to,
    amount: fields.amount,
    public_key: fields.publicKeyHex.toLowerCase(),
  };
  if (fields.asset !== undefined && fields.asset !== null && fields.asset !== "") {
    payload.asset = fields.asset;
  }
  return {
    network: fields.network,
    type: "transfer",
    from: fields.sender,
    nonce: fields.nonce,
    fee: fields.fee,
    payload,
  };
}

export interface RevealFields {
  network: string;
  sender: string;
  nonce: number;
  fee: number;
  /** The epoch the commitment was made for; a reveal is only valid at `epoch - 1`. */
  epoch: number;
  nonceSecret: string;
  publicKeyHex: string;
}

/** The `reveal` transaction that opens a committed epoch's roster slot. */
export function buildReveal(fields: RevealFields): Transaction {
  return {
    network: fields.network,
    type: "reveal",
    from: fields.sender,
    nonce: fields.nonce,
    fee: fields.fee,
    payload: {
      public_key: fields.publicKeyHex.toLowerCase(),
      epoch: fields.epoch,
      nonce: fields.nonceSecret,
    },
  };
}

/** The `create_pool` transaction: an initial two-sided deposit. */
export interface CreatePoolFields {
  network: string;
  sender: string;
  nonce: number;
  fee: number;
  /** `null` means native SCCP. The builder orders the pair itself. */
  assetA: string | null;
  amountA: number;
  assetB: string | null;
  amountB: number;
  publicKeyHex: string;
}

function requirePositive(field: string, value: number): void {
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new TxError(`${field} must be a positive integer, got ${value}`);
  }
}

/** The `create_pool` transaction: `{asset_a?, amount_a, asset_b?, amount_b}`. */
export function buildCreatePool(fields: CreatePoolFields): Transaction {
  requirePositive("amount_a", fields.amountA);
  requirePositive("amount_b", fields.amountB);
  const first = { asset: fields.assetA ?? "", amount: fields.amountA };
  const second = { asset: fields.assetB ?? "", amount: fields.amountB };
  if (first.asset === second.asset) {
    throw new TxError("a pool needs two different assets");
  }
  const [a, b] = first.asset < second.asset ? [first, second] : [second, first];
  const payload: Record<string, unknown> = {
    amount_a: a.amount,
    amount_b: b.amount,
    public_key: fields.publicKeyHex.toLowerCase(),
  };
  if (a.asset !== "") payload.asset_a = a.asset;
  if (b.asset !== "") payload.asset_b = b.asset;
  return {
    network: fields.network,
    type: "create_pool",
    from: fields.sender,
    nonce: fields.nonce,
    fee: fields.fee,
    payload,
  };
}

export interface AddLiquidityFields {
  network: string;
  sender: string;
  nonce: number;
  fee: number;
  poolId: string;
  amountA: number;
  amountB: number;
  publicKeyHex: string;
}

/** The `add_liquidity` transaction: a proportional two-sided deposit. */
export function buildAddLiquidity(fields: AddLiquidityFields): Transaction {
  requirePositive("amount_a", fields.amountA);
  requirePositive("amount_b", fields.amountB);
  return {
    network: fields.network,
    type: "add_liquidity",
    from: fields.sender,
    nonce: fields.nonce,
    fee: fields.fee,
    payload: {
      pool_id: fields.poolId,
      amount_a: fields.amountA,
      amount_b: fields.amountB,
      public_key: fields.publicKeyHex.toLowerCase(),
    },
  };
}

export interface RemoveLiquidityFields {
  network: string;
  sender: string;
  nonce: number;
  fee: number;
  poolId: string;
  shares: number;
  publicKeyHex: string;
}

/** The `remove_liquidity` transaction: redeem pool shares for both reserves. */
export function buildRemoveLiquidity(fields: RemoveLiquidityFields): Transaction {
  requirePositive("shares", fields.shares);
  return {
    network: fields.network,
    type: "remove_liquidity",
    from: fields.sender,
    nonce: fields.nonce,
    fee: fields.fee,
    payload: {
      pool_id: fields.poolId,
      shares: fields.shares,
      public_key: fields.publicKeyHex.toLowerCase(),
    },
  };
}

export interface SwapFields {
  network: string;
  sender: string;
  nonce: number;
  fee: number;
  poolId: string;
  /** `null` means native SCCP. */
  assetIn: string | null;
  amountIn: number;
  minAmountOut: number;
  publicKeyHex: string;
}

/** The `swap` transaction: one side in, the other out, both in one transition. */
export function buildSwap(fields: SwapFields): Transaction {
  requirePositive("amount_in", fields.amountIn);
  if (!Number.isSafeInteger(fields.minAmountOut) || fields.minAmountOut < 0) {
    throw new TxError(`min_amount_out must be a non-negative integer`);
  }
  const payload: Record<string, unknown> = {
    pool_id: fields.poolId,
    amount_in: fields.amountIn,
    min_amount_out: fields.minAmountOut,
    public_key: fields.publicKeyHex.toLowerCase(),
  };
  if (fields.assetIn !== null && fields.assetIn !== "") payload.asset_in = fields.assetIn;
  return {
    network: fields.network,
    type: "swap",
    from: fields.sender,
    nonce: fields.nonce,
    fee: fields.fee,
    payload,
  };
}

/** The id an `htlc_lock` creates: `H("sccp/v1/htlc", {from, nonce})`. */
export function htlcId(sender: string, nonce: number): string {
  return domainHex(DOMAINS.HTLC, { from: sender, nonce });
}

function requireDigest(name: string, value: string): void {
  if (!/^[0-9a-f]{64}$/.test(value)) {
    throw new TxError(`${name} must be 32-byte lowercase hex`);
  }
}

export interface HtlcLockFields {
  network: string;
  sender: string;
  nonce: number;
  fee: number;
  hashlock: string;
  timelockHeight: number;
  beneficiary: string;
  amount: number;
  publicKeyHex: string;
  /** A non-native asset id; omit for native SCCP. */
  asset?: string | null;
}

/** `htlc_lock`: move tokens out of a balance into consensus escrow. */
export function buildHtlcLock(fields: HtlcLockFields): Transaction {
  requirePositive("amount", fields.amount);
  requireDigest("hashlock", fields.hashlock);
  if (!Number.isSafeInteger(fields.timelockHeight) || fields.timelockHeight < 0) {
    throw new TxError("timelock_height must be a non-negative integer");
  }
  const payload: Record<string, unknown> = {
    hashlock: fields.hashlock,
    timelock_height: fields.timelockHeight,
    beneficiary: fields.beneficiary,
    amount: fields.amount,
    public_key: fields.publicKeyHex.toLowerCase(),
  };
  if (fields.asset !== undefined && fields.asset !== null && fields.asset !== "") {
    payload.asset = fields.asset;
  }
  return {
    network: fields.network,
    type: "htlc_lock",
    from: fields.sender,
    nonce: fields.nonce,
    fee: fields.fee,
    payload,
  };
}

export interface HtlcCloseFields {
  network: string;
  sender: string;
  nonce: number;
  fee: number;
  htlcId: string;
  publicKeyHex: string;
}

/** `htlc_claim`: reveal the 32-byte preimage and receive the escrow. */
export function buildHtlcClaim(fields: HtlcCloseFields & { preimage: string }): Transaction {
  requireDigest("htlc_id", fields.htlcId);
  requireDigest("preimage", fields.preimage);
  return {
    network: fields.network,
    type: "htlc_claim",
    from: fields.sender,
    nonce: fields.nonce,
    fee: fields.fee,
    payload: {
      htlc_id: fields.htlcId,
      preimage: fields.preimage,
      public_key: fields.publicKeyHex.toLowerCase(),
    },
  };
}

/** `htlc_refund`: return an expired escrow to the account that locked it. */
export function buildHtlcRefund(fields: HtlcCloseFields): Transaction {
  requireDigest("htlc_id", fields.htlcId);
  return {
    network: fields.network,
    type: "htlc_refund",
    from: fields.sender,
    nonce: fields.nonce,
    fee: fields.fee,
    payload: {
      htlc_id: fields.htlcId,
      public_key: fields.publicKeyHex.toLowerCase(),
    },
  };
}
