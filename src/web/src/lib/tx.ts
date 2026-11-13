/**
 * Ledger transactions, ported from `sccp/chain/tx.py` and
 * `sccp/wallet/service.py`.
 *
 * The envelope is one canonical body — `{network, type, from, nonce, fee,
 * payload}` — and the signature is Ed25519 over `H("sccp/v1/tx", body)`. The
 * payload's shape depends on `type` and is validated by the chain, so these
 * builders only fill in the fields a seat needs to join a roster:
 * `register`, `availability` and `reveal`.
 *
 * Nothing here is specific to a browser: the same bytes `sccp miner register`
 * produces come out of {@link buildRegister}, which is what lets a seat created
 * in this tab be paired by the relay like any other.
 *
 * Signing is Ed25519-only on purpose. A CLI wallet may hold a hybrid
 * Ed25519 + ML-DSA-65 key, and the chain accepts its 6748-hex signature, but no
 * browser primitive signs ML-DSA-65 and a miner seat must be a version-0
 * address anyway (`PROTOCOL.md` section 10.5). {@link isHybridSignatureHex}
 * exists so a viewer can label such a transaction instead of mistaking it for a
 * malformed one.
 */

import { DOMAINS, domainHex, fromHex } from "./canonical";
import { type Keypair, signDigest } from "./keys";

/** Wire spellings from `sccp.protocol.models.TxType`. */
export type TxType = "transfer" | "register" | "register_witness" | "availability" | "reveal";

/** A signed ledger transaction, in wire order (`from` is the sender field). */
export interface Transaction {
  network: string;
  type: TxType;
  from: string;
  nonce: number;
  fee: number;
  payload: Record<string, unknown>;
  /** 128 hex characters for Ed25519, 6748 for a hybrid account (or absent). */
  signature?: string;
}

export class TxError extends Error {
  override name = "TxError";
}

/** Hex length of a hybrid Ed25519 + ML-DSA-65 signature (3374 bytes). */
export const HYBRID_SIGNATURE_HEX_LENGTH = 6748;

/**
 * Whether `signature` is a hybrid blob rather than an Ed25519 signature.
 *
 * The scheme byte is `0x01`, so a well-formed hybrid signature starts with
 * `"01"`; the length check alone is enough to tell the two apart on the wire,
 * which is what the chain does (`sccp.protocol.models.TxSignatureHex`).
 */
export function isHybridSignatureHex(signature: string | null | undefined): boolean {
  return (
    typeof signature === "string" &&
    signature.length === HYBRID_SIGNATURE_HEX_LENGTH &&
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

/** Sign `tx` and return the wire form (`signature` present, never `undefined`). */
export function signTransaction(keypair: Keypair, tx: Transaction): Transaction {
  const unsigned: Transaction = { ...tx };
  delete unsigned.signature;
  return { ...unsigned, signature: signDigest(keypair, fromHex(txId(unsigned))) };
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
}

/** The `register` transaction: a bond, an endpoint and an engine fingerprint. */
export function buildRegister(fields: RegisterFields): Transaction {
  return {
    network: fields.network,
    type: "register",
    from: fields.sender,
    nonce: fields.nonce,
    fee: fields.fee,
    payload: {
      public_key: fields.publicKeyHex.toLowerCase(),
      endpoint: fields.endpoint,
      engine: fields.engine,
      bond: fields.bond,
    },
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
