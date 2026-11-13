/**
 * Ed25519 keypairs in the browser.
 *
 * WebCrypto still refuses Ed25519 in some browsers and generating a non-extractable
 * key cannot be serialized, so keys come from `@noble/curves` — the same curve, the
 * same 32-byte seed / 32-byte public key / 64-byte signature shapes that
 * `sccp/crypto/keys.py` uses. Signatures made here verify in Python and vice versa.
 */

import { ed25519 } from "@noble/curves/ed25519.js";

import { ADDRESS_VERSION, HASH_SIZE, addressHrp, encodeProgram } from "./bech32";
import { type Domain, fromHex, domainHash, toHex } from "./canonical";

export const SEED_SIZE = 32;
export const PUBLIC_KEY_SIZE = 32;
export const SIGNATURE_SIZE = 64;

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
 * The 20-byte address hash: `H("sccp/v1/address", {network, public_key})[:20]`.
 *
 * Note that this is *not* `sha256(public_key)[:20]`; the network is inside the
 * hash so the same key has an unrelated address on each chain.
 */
export function addressHash(network: string, publicKeyHexText: string): Uint8Array {
  const digest = domainHash("sccp/v1/address", {
    network,
    public_key: publicKeyHexText.toLowerCase(),
  });
  return digest.slice(0, HASH_SIZE);
}

/** The 21-byte `version || hash` program the bech32 string commits to. */
export function addressProgram(publicKey: Uint8Array, network: string): Uint8Array {
  const program = new Uint8Array(1 + HASH_SIZE);
  program[0] = ADDRESS_VERSION;
  program.set(addressHash(network, toHex(publicKey)), 1);
  return program;
}

/** Derive the bech32 address for a raw public key on `network`. */
export function addressFor(publicKey: Uint8Array, network: string): string {
  return encodeProgram(addressHrp(network), addressProgram(publicKey, network));
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
