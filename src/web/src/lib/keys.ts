/**
 * Hybrid Ed25519 + ML-DSA-65 keys in the browser.
 *
 * WebCrypto still refuses Ed25519 in some browsers and generating a non-extractable
 * key cannot be serialized, so keys come from `@noble/curves` and
 * `@noble/post-quantum` — the same primitives `sccp/crypto/pq.py` uses: one
 * 32-byte master seed, two HMAC-separated sub-seeds, a 1985-byte
 * `scheme || ed25519 || mldsa65` public key and a 3374-byte signature blob.
 * Keys and signatures made here verify in Python and vice versa, which is what
 * makes a browser tab a real seat rather than a read-only viewer.
 *
 * Two key types live here, and the difference matters:
 *
 * - {@link Keypair} is the Ed25519 *half*. Moves, clock receipts and gossip are
 *   Ed25519 (`docs/PROTOCOL.md` section 8.5), so those are signed with it.
 * - {@link HybridKeypair} is the account: it holds both halves. Anything the
 *   ledger or the relay checks as a hybrid signature — transactions,
 *   certificates, results — is signed with it.
 */

import { ed25519 } from "@noble/curves/ed25519.js";
import { hmac } from "@noble/hashes/hmac.js";
import { sha256 } from "@noble/hashes/sha2.js";
import { ml_dsa65 } from "@noble/post-quantum/ml-dsa.js";

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

/** Leading byte of a hybrid public key or signature (`HYBRID_SCHEME_ID`). */
export const HYBRID_SCHEME_ID = 1;

/** `scheme || ed25519(32) || mldsa65(1952)`: the only account public key. */
export const HYBRID_PUBLIC_KEY_SIZE = 1985;

/** Hex characters in one hybrid public key blob (3970). */
export const HYBRID_PUBLIC_KEY_HEX_LENGTH = HYBRID_PUBLIC_KEY_SIZE * 2;

/** Raw ML-DSA-65 public key length in bytes (FIPS 204, category 3). */
export const MLDSA65_PUBLIC_KEY_SIZE = 1952;

/** Raw ML-DSA-65 signature length in bytes. */
export const MLDSA65_SIGNATURE_SIZE = 3309;

/** `scheme || ed25519(64) || mldsa65(3309)` signature blob: 3374 bytes. */
export const HYBRID_SIGNATURE_SIZE = 1 + SIGNATURE_SIZE + MLDSA65_SIGNATURE_SIZE;

/** Hex characters in one hybrid signature (6748). */
export const HYBRID_SIGNATURE_HEX_LENGTH = HYBRID_SIGNATURE_SIZE * 2;

export interface Keypair {
  /** 32-byte Ed25519 seed: this is the secret. */
  readonly seed: Uint8Array;
  /** 32-byte raw public key. */
  readonly publicKey: Uint8Array;
}

/** An account key: the Ed25519 half plus the ML-DSA-65 half, from one seed. */
export interface HybridKeypair {
  /** The 32-byte master seed both halves are derived from: this is the secret. */
  readonly seed: Uint8Array;
  /** The Ed25519 half, which signs moves, clock receipts and gossip. */
  readonly ed25519: Keypair;
  /** The 1985-byte `scheme || ed25519 || mldsa65` public key blob. */
  readonly publicKey: Uint8Array;
  /** The raw 4032-byte ML-DSA-65 private key. */
  readonly mldsaSecretKey: Uint8Array;
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

/** The ML-DSA-65 sub-seed's HMAC label (`sccp.crypto.pq._MLDSA_LABEL`). */
export const MLDSA_SUBSEED_LABEL = new TextEncoder().encode("sccp/v1/pq/mldsa65");

/** The ML-DSA-65 half's seed: `HMAC-SHA-256(master_seed, "sccp/v1/pq/mldsa65")`. */
export function hybridMldsaSeed(masterSeed: Uint8Array): Uint8Array {
  return hmac(sha256, checkSeed(masterSeed), MLDSA_SUBSEED_LABEL);
}

/**
 * The FIPS 204 context string every ML-DSA signature is bound to
 * (`sccp.crypto.pq.SIGNATURE_CONTEXT`).
 *
 * A context is signed, not transported, so a signature made for another
 * protocol with the same key does not verify here — and vice versa.
 */
export const HYBRID_SIGNATURE_CONTEXT = new TextEncoder().encode(
  "sccp/v1/hybrid-ed25519-mldsa65",
);

/**
 * The prefix hashed into the message *both* halves sign
 * (`sccp.crypto.pq._MESSAGE_TAG`).
 *
 * Ed25519 and ML-DSA would otherwise be signing different byte strings under the
 * same intent; tagging keeps a signature from one scheme from ever being read as
 * a signature over a differently-framed message in the other.
 */
const HYBRID_MESSAGE_TAG = new TextEncoder().encode("sccp/v1/pq-hybrid\u0000");

/**
 * The Ed25519 half of a hybrid account, from its 32-byte master seed.
 *
 * This is the key that signs moves, clock receipts and gossip — the streams
 * `docs/PROTOCOL.md` section 8.5 keeps classical because they are anchored by a
 * hybrid signature elsewhere. Use {@link hybridKeypairFromSeed} when the full
 * account is needed; this helper exists so a caller that only publishes moves
 * does not pay for an ML-DSA keygen.
 */
export function hybridEd25519Keypair(masterSeed: Uint8Array): Keypair {
  return keypairFromSeed(hybridEd25519Seed(masterSeed));
}

/**
 * Derive the full hybrid account from its 32-byte master seed.
 *
 * The public key is built exactly as `HybridKeypair.public_key_bytes` does in
 * Python, so the bytes a browser registers are the bytes the node parses and the
 * address it derives is the address the roster holds. ML-DSA-65 key generation
 * from a seed is deterministic (FIPS 204 `KeyGen` expands the seed with
 * SHAKE-256), which is why this is reproducible from a phrase at all.
 */
export function hybridKeypairFromSeed(seed: Uint8Array): HybridKeypair {
  const master = checkSeed(seed);
  const ed25519Half = keypairFromSeed(hybridEd25519Seed(master));
  const { secretKey, publicKey } = ml_dsa65.keygen(hybridMldsaSeed(master));
  const blob = new Uint8Array(HYBRID_PUBLIC_KEY_SIZE);
  blob[0] = HYBRID_SCHEME_ID;
  blob.set(ed25519Half.publicKey, 1);
  blob.set(publicKey, 1 + PUBLIC_KEY_SIZE);
  return {
    seed: master,
    ed25519: ed25519Half,
    publicKey: blob,
    mldsaSecretKey: secretKey,
  };
}

/** Derive a hybrid account from a hex master seed, as the page asks for it. */
export function hybridKeypairFromSeedHex(text: string): HybridKeypair {
  return hybridKeypairFromSeed(fromHex(text.trim().toLowerCase()));
}

/** Generate a hybrid account from the platform CSPRNG. */
export function generateHybridKeypair(): HybridKeypair {
  const seed = new Uint8Array(SEED_SIZE);
  crypto.getRandomValues(seed);
  return hybridKeypairFromSeed(seed);
}

/** The 3970-hex hybrid public key blob: what a seat registers on chain. */
export function hybridPublicKeyHex(keypair: HybridKeypair): string {
  return toHex(keypair.publicKey);
}

function tagged(message: Uint8Array): Uint8Array {
  const out = new Uint8Array(HYBRID_MESSAGE_TAG.length + message.length);
  out.set(HYBRID_MESSAGE_TAG, 0);
  out.set(message, HYBRID_MESSAGE_TAG.length);
  return out;
}

/**
 * Sign `message` with both halves; returns the 6748-hex hybrid signature blob.
 *
 * This is `sccp.crypto.pq.hybrid_sign`: Ed25519 over the tagged message with the
 * sub-seed, and ML-DSA-65 over the same bytes with the protocol context. Both
 * halves must verify, so the signature is no stronger than Ed25519 against a
 * classical attacker and no weaker than ML-DSA-65 against a quantum one.
 *
 * FIPS 204 signing is hedged, so two calls over the same message produce
 * different — equally valid — signatures. Nothing may compare these bytes; code
 * that wants a check verifies them.
 */
export function hybridSign(keypair: HybridKeypair, message: Uint8Array): string {
  const signed = tagged(message);
  const ed25519Signature = ed25519.sign(signed, keypair.ed25519.seed);
  const mldsaSignature = ml_dsa65.sign(signed, keypair.mldsaSecretKey, {
    context: HYBRID_SIGNATURE_CONTEXT,
  });
  if (mldsaSignature.length !== MLDSA65_SIGNATURE_SIZE) {
    throw new KeyError(
      `ML-DSA-65 produced a ${mldsaSignature.length}-byte signature, expected ${MLDSA65_SIGNATURE_SIZE}`,
    );
  }
  const blob = new Uint8Array(HYBRID_SIGNATURE_SIZE);
  blob[0] = HYBRID_SCHEME_ID;
  blob.set(ed25519Signature, 1);
  blob.set(mldsaSignature, 1 + SIGNATURE_SIZE);
  return toHex(blob);
}

/**
 * Sign `sha256(domain + 0x00 + canonical_json(payload))` with both halves.
 *
 * Certificates and results are signed over a domain hash, never over raw JSON,
 * so the payload's exact spelling cannot be re-interpreted by a verifier.
 */
export function hybridSignDomain(
  keypair: HybridKeypair,
  domain: Domain | string,
  payload: unknown,
): string {
  return hybridSign(keypair, domainHash(domain, payload));
}

/**
 * Verify a hybrid signature over raw message bytes. Never throws.
 *
 * Malformed keys, signatures of the wrong scheme or length, and a disabled
 * ML-DSA implementation all return `false`: this consumes network data, and a
 * verifier that raises on bad input is a denial-of-service surface.
 */
export function hybridVerify(
  publicKeyHexText: string,
  message: Uint8Array,
  signatureHex: string,
): boolean {
  try {
    const signature = fromHex(signatureHex.trim().toLowerCase());
    if (signature.length !== HYBRID_SIGNATURE_SIZE) return false;
    if (signature[0] !== HYBRID_SCHEME_ID) return false;
    const publicKey = fromHex(publicKeyHexText.trim().toLowerCase());
    if (publicKey.length !== HYBRID_PUBLIC_KEY_SIZE) return false;
    if (publicKey[0] !== HYBRID_SCHEME_ID) return false;
    const signed = tagged(message);
    const ed25519Signature = signature.subarray(1, 1 + SIGNATURE_SIZE);
    if (!ed25519.verify(ed25519Signature, signed, publicKey.subarray(1, 1 + PUBLIC_KEY_SIZE))) {
      return false;
    }
    return ml_dsa65.verify(
      signature.subarray(1 + SIGNATURE_SIZE),
      signed,
      publicKey.subarray(1 + PUBLIC_KEY_SIZE),
      { context: HYBRID_SIGNATURE_CONTEXT },
    );
  } catch {
    return false;
  }
}

/** Verify a signature produced by {@link hybridSignDomain}. Never throws. */
export function hybridVerifyDomain(
  publicKeyHexText: string,
  domain: Domain | string,
  payload: unknown,
  signatureHex: string,
): boolean {
  try {
    return hybridVerify(publicKeyHexText, domainHash(domain, payload), signatureHex);
  } catch {
    return false;
  }
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
