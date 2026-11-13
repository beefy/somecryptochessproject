/**
 * Ed25519 keypairs in the browser.
 *
 * WebCrypto still refuses Ed25519 in some browsers and generating a non-extractable
 * key cannot be serialized, so keys come from `@noble/curves` — the same curve, the
 * same 32-byte seed / 32-byte public key / 64-byte signature shapes that
 * `sccp/crypto/keys.py` uses. Signatures made here verify in Python and vice versa.
 */

import { ed25519 } from "@noble/curves/ed25519.js";
import { hmac } from "@noble/hashes/hmac.js";
import { sha256 } from "@noble/hashes/sha2.js";

import { ADDRESS_VERSION, PROGRAM_SIZE, addressHrp, encodeProgram } from "./bech32";
import { type Domain, fromHex, domainHash, toHex } from "./canonical";

export const SEED_SIZE = 32;
export const PUBLIC_KEY_SIZE = 32;
export const SIGNATURE_SIZE = 64;

/**
 * The algorithm name committed to by every address
 * (`sccp.crypto.pq.HYBRID_ALGORITHM`).
 */
export const HYBRID_ALGORITHM = "ed25519-mldsa65";

/** `scheme || ed25519(32) || mldsa65(1952)`: the only account public key. */
export const HYBRID_PUBLIC_KEY_SIZE = 1985;

/** Hex characters in one hybrid public key blob (3970). */
export const HYBRID_PUBLIC_KEY_HEX_LENGTH = HYBRID_PUBLIC_KEY_SIZE * 2;

export interface Keypair {
  /** 32-byte Ed25519 seed: this is the secret. */
  readonly seed: Uint8Array;
  /** 32-byte raw public key. */
  readonly publicKey: Uint8Array;
}

export class KeyError extends Error {
  override name = "KeyError";
}

function checkSeed(seed: Uint8Array): Uint8Array {
  if (seed.length !== SEED_SIZE) {
    throw new KeyError(`Ed25519 seed must be ${SEED_SIZE} bytes, got ${seed.length}`);
  }
  return seed;
}

/** Deterministically derive a keypair from a 32-byte seed. */
export function keypairFromSeed(seed: Uint8Array): Keypair {
  const checked = checkSeed(seed);
  return { seed: checked, publicKey: ed25519.getPublicKey(checked) };
}

/** Generate a keypair from the platform CSPRNG. */
export function generateKeypair(): Keypair {
  const seed = new Uint8Array(SEED_SIZE);
  crypto.getRandomValues(seed);
  return keypairFromSeed(seed);
}

/**
 * The HMAC label `sccp.crypto.pq` derives the Ed25519 sub-seed under.
 *
 * A hybrid account's two halves are not "the same seed used twice": each is
 * `HMAC-SHA-256(master_seed, label)`, so a key recovered from one scheme can
 * never be replayed under the other.
 */
export const ED25519_SUBSEED_LABEL = new TextEncoder().encode("sccp/v1/pq/ed25519");

/** The Ed25519 half's seed: `HMAC-SHA-256(master_seed, "sccp/v1/pq/ed25519")`. */
export function hybridEd25519Seed(masterSeed: Uint8Array): Uint8Array {
  return hmac(sha256, checkSeed(masterSeed), ED25519_SUBSEED_LABEL);
}

/**
 * The Ed25519 half of a hybrid account, from its 32-byte master seed.
 *
 * This is the key that signs moves, clock receipts and gossip. Deriving it here
 * means a browser holding a hybrid master seed can play a game (and verify every
 * signature it makes in Python) even without an ML-DSA implementation; the
 * hybrid half is only needed to sign transactions and results.
 */
export function hybridEd25519Keypair(masterSeed: Uint8Array): Keypair {
  return keypairFromSeed(hybridEd25519Seed(masterSeed));
}

/** Rebuild a keypair from a hex seed, as shown by the "export key" button. */
export function keypairFromSeedHex(text: string): Keypair {
  return keypairFromSeed(fromHex(text.trim().toLowerCase()));
}

export function publicKeyHex(keypair: Keypair): string {
  return toHex(keypair.publicKey);
}

export function seedHex(keypair: Keypair): string {
  return toHex(keypair.seed);
}

/**
 * The 32-byte address hash: `H("sccp/v1/address", {algorithm, network,
 * public_key})`.
 *
 * The complete SHA-256 digest, not a prefix: an address is what stands between a
 * quantum adversary and someone else's balance, so it keeps 128-bit preimage
 * security. The algorithm name and the network are inside the hash, so the same
 * key has an unrelated address on each chain and a future scheme cannot collide
 * with this one.
 */
export function addressHash(network: string, publicKeyHexText: string): Uint8Array {
  return domainHash("sccp/v1/address", {
    algorithm: HYBRID_ALGORITHM,
    network,
    public_key: publicKeyHexText.toLowerCase(),
  });
}

/** The 33-byte `version || hash` program the bech32 string commits to. */
export function addressProgram(publicKey: Uint8Array, network: string): Uint8Array {
  if (publicKey.length !== HYBRID_PUBLIC_KEY_SIZE) {
    throw new KeyError(
      `an address needs the ${HYBRID_PUBLIC_KEY_SIZE}-byte hybrid public key, got ${publicKey.length}`,
    );
  }
  const program = new Uint8Array(PROGRAM_SIZE);
  program[0] = ADDRESS_VERSION;
  program.set(addressHash(network, toHex(publicKey)), 1);
  return program;
}

/** Derive the bech32 address for a hybrid public key blob on `network`. */
export function addressFor(publicKey: Uint8Array, network: string): string {
  return encodeProgram(addressHrp(network), addressProgram(publicKey, network));
}

/**
 * The Ed25519 half of a stored hybrid public key, as raw 32-byte hex.
 *
 * Moves, clock receipts and gossip sign with this half (`ed25519_public_key_hex`
 * in Python); the hybrid checkpoint anchors them. Total: anything that is not a
 * hybrid blob is returned unchanged so a verifier still fails closed.
 */
export function ed25519HalfHex(hybridPublicKeyHex: string): string {
  const text = hybridPublicKeyHex.toLowerCase();
  if (text.length === HYBRID_PUBLIC_KEY_HEX_LENGTH) return text.slice(2, 2 + PUBLIC_KEY_SIZE * 2);
  return text;
}

/** Sign `sha256(domain + 0x00 + canonical_json(payload))`; returns lowercase hex. */
export function signDomain(keypair: Keypair, domain: Domain | string, payload: unknown): string {
  const digest = domainHash(domain, payload);
  return toHex(ed25519.sign(digest, keypair.seed));
}

/**
 * Sign a raw 32-byte digest.
 *
 * Ledger transactions sign their *txid* directly (`sccp.chain.tx.sign_transaction`),
 * not a domain hash, so the payload has already been folded in by the caller.
 */
export function signDigest(keypair: Keypair, digest: Uint8Array): string {
  if (digest.length !== 32) {
    throw new KeyError(`a digest signature covers 32 bytes, got ${digest.length}`);
  }
  return toHex(ed25519.sign(digest, keypair.seed));
}

/** Verify a signature produced by {@link signDigest}. Never throws. */
export function verifyDigest(
  publicKeyHexText: string,
  digest: Uint8Array,
  signatureHex: string,
): boolean {
  try {
    if (digest.length !== 32) return false;
    const signature = fromHex(signatureHex);
    if (signature.length !== SIGNATURE_SIZE) return false;
    const publicKey = fromHex(publicKeyHexText);
    if (publicKey.length !== PUBLIC_KEY_SIZE) return false;
    return ed25519.verify(signature, digest, publicKey);
  } catch {
    return false;
  }
}

/** Verify a signature produced by {@link signDomain}. Never throws. */
export function verifyDomain(
  publicKeyHexText: string,
  domain: Domain | string,
  payload: unknown,
  signatureHex: string,
): boolean {
  try {
    const signature = fromHex(signatureHex);
    if (signature.length !== SIGNATURE_SIZE) return false;
    const publicKey = fromHex(publicKeyHexText);
    if (publicKey.length !== PUBLIC_KEY_SIZE) return false;
    return ed25519.verify(signature, domainHash(domain, payload), publicKey);
  } catch {
    return false;
  }
}
