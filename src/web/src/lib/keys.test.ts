import { describe, expect, it } from "vitest";

import { DOMAINS, fromHex, toHex } from "./canonical";
import {
  addressFor,
  ed25519HalfHex,
  generateKeypair,
  hybridEd25519Keypair,
  hybridEd25519Seed,
  hybridKeypairFromSeed,
  hybridMldsaSeed,
  hybridSignDomain,
  hybridVerify,
  hybridVerifyDomain,
  keypairFromSeed,
  publicKeyHex,
  signDomain,
  verifyDomain,
  HYBRID_PUBLIC_KEY_SIZE,
  HYBRID_SIGNATURE_HEX_LENGTH,
  HYBRID_SIGNATURE_SIZE,
  MLDSA65_SIGNATURE_SIZE,
} from "./keys";
import { movePayload } from "./protocol";
import vectors from "./vectors.json";

const keys = vectors.keys;
const hybridPublicKey = fromHex(keys.hybrid_public_key_hex);

describe("keys", () => {
  it("derives the documented Ed25519 half from the master seed", () => {
    const half = hybridEd25519Keypair(fromHex(keys.seed_hex));
    expect(publicKeyHex(half)).toBe(keys.public_key_hex);
    expect(ed25519HalfHex(keys.hybrid_public_key_hex)).toBe(keys.public_key_hex);
    // The plain Ed25519 keypair of the same seed is a *different* key: the
    // hybrid half is HMAC-separated (sccp.crypto.pq derives both halves).
    expect(publicKeyHex(keypairFromSeed(fromHex(keys.seed_hex)))).not.toBe(keys.public_key_hex);
  });

  it("derives the same addresses as sccp/crypto/address.py", () => {
    expect(addressFor(hybridPublicKey, "testnet")).toBe(keys.address_testnet);
    expect(addressFor(hybridPublicKey, "mainnet")).toBe(keys.address_mainnet);
    expect(addressFor(hybridPublicKey, "devnet")).toBe(keys.address_devnet);
    // The network is inside the address hash, so devnet and testnet share an HRP
    // but must not share an address.
    expect(keys.address_devnet).not.toBe(keys.address_testnet);
  });

  it("refuses anything that is not a hybrid public key blob", () => {
    expect(() => addressFor(new Uint8Array(32), "testnet")).toThrow(/hybrid public key/);
  });

  it("derives the same hybrid public key as sccp/crypto/pq.py", () => {
    const keypair = hybridKeypairFromSeed(fromHex(keys.seed_hex));
    // The Ed25519 half is the HMAC-separated sub-seed, not the master seed.
    expect(publicKeyHex(keypair.ed25519)).toBe(keys.public_key_hex);
    // The blob is scheme || ed25519 || mldsa65, byte for byte what Python builds,
    // which is what makes a key generated here registrable on chain.
    expect(toHex(keypair.publicKey)).toBe(keys.hybrid_public_key_hex);
    expect(keypair.publicKey).toHaveLength(HYBRID_PUBLIC_KEY_SIZE);
    expect(keypair.mldsaSecretKey).toHaveLength(4032);
  });

  it("keeps the two sub-seeds under separate HMAC labels", () => {
    const master = fromHex(keys.seed_hex);
    expect(hybridMldsaSeed(master)).toHaveLength(32);
    expect(hybridMldsaSeed(master)).not.toStrictEqual(hybridEd25519Seed(master));
  });

  it("signs a payload with both halves and verifies it", () => {
    const keypair = hybridKeypairFromSeed(fromHex(keys.seed_hex));
    const signature = hybridSignDomain(keypair, DOMAINS.RESULT, vectors.result.payload);
    expect(signature).toHaveLength(HYBRID_SIGNATURE_HEX_LENGTH);
    expect(signature.startsWith("01")).toBe(true);
    expect(hybridVerifyDomain(keys.hybrid_public_key_hex, DOMAINS.RESULT, vectors.result.payload, signature)).toBe(
      true,
    );
    // The domain is signed: the same bytes under another domain do not verify,
    // and neither key can stand in for the other.
    expect(
      hybridVerifyDomain(keys.hybrid_public_key_hex, DOMAINS.CERTIFICATE, vectors.result.payload, signature),
    ).toBe(false);
    expect(
      hybridVerifyDomain(keys.peer_hybrid_public_key_hex, DOMAINS.RESULT, vectors.result.payload, signature),
    ).toBe(false);
  });

  it("verifies the hybrid result signature Python produced", () => {
    const signature = vectors.result.ledger_signature_hex;
    expect(fromHex(signature)).toHaveLength(HYBRID_SIGNATURE_SIZE);
    expect(
      hybridVerifyDomain(keys.peer_hybrid_public_key_hex, DOMAINS.RESULT, vectors.result.payload, signature),
    ).toBe(true);
    expect(
      hybridVerifyDomain(keys.peer_hybrid_public_key_hex, DOMAINS.CERTIFICATE, vectors.result.payload, signature),
    ).toBe(false);
    // The Ed25519 half signs the *tagged* digest, so it is deliberately not the
    // same bytes as the plain Ed25519 signature over the same payload.
    expect(signature.slice(2, 2 + 64)).not.toBe(vectors.result.signature_hex);
  });

  it("refuses malformed hybrid signatures instead of throwing", () => {
    const message = new Uint8Array(32);
    expect(hybridVerify(keys.hybrid_public_key_hex, message, "00")).toBe(false);
    expect(hybridVerify(keys.hybrid_public_key_hex, message, "zz")).toBe(false);
    // An Ed25519-half key is not a hybrid blob, and a hybrid signature under
    // another scheme byte is not this scheme's signature.
    expect(hybridVerify(keys.public_key_hex, message, vectors.result.ledger_signature_hex)).toBe(false);
    expect(
      hybridVerify(keys.hybrid_public_key_hex, message, `02${vectors.result.ledger_signature_hex.slice(2)}`),
    ).toBe(false);
    // A truncated blob is the most likely corruption on the wire.
    const truncated = vectors.result.ledger_signature_hex.slice(0, 2 + MLDSA65_SIGNATURE_SIZE * 2);
    expect(hybridVerify(keys.hybrid_public_key_hex, message, truncated)).toBe(false);
  });

  it("verifies the move signature Python produced", () => {
    expect(
      verifyDomain(keys.public_key_hex, "sccp/v1/move", vectors.move.payload, vectors.move.signature_hex),
    ).toBe(true);
    expect(
      verifyDomain(keys.public_key_hex, "sccp/v1/result", vectors.move.payload, vectors.move.signature_hex),
    ).toBe(false);
  });

  it("verifies the Ed25519 half of the result signature Python produced", () => {
    expect(
      verifyDomain(
        keys.peer_public_key_hex,
        "sccp/v1/result",
        vectors.result.payload,
        vectors.result.signature_hex,
      ),
    ).toBe(true);
  });

  it("reproduces Python's move signature exactly", () => {
    const keypair = hybridEd25519Keypair(fromHex(keys.seed_hex));
    const payload = movePayload({
      network: vectors.network,
      match_id: vectors.pairing.match_id,
      seat: keys.address_testnet,
      ply: 1,
      san: "f3",
    });
    expect(payload).toStrictEqual(vectors.move.payload);
    expect(signDomain(keypair, "sccp/v1/move", payload)).toBe(vectors.move.signature_hex);
  });

  it("generates distinct fresh keypairs", () => {
    const first = generateKeypair();
    const second = generateKeypair();
    expect(publicKeyHex(first)).not.toBe(publicKeyHex(second));
    expect(first.seed).toHaveLength(32);
    expect(() => keypairFromSeed(new Uint8Array(31))).toThrow();
  });

  it("refuses malformed signatures instead of throwing", () => {
    expect(verifyDomain(keys.public_key_hex, "sccp/v1/move", vectors.move.payload, "00")).toBe(false);
    expect(verifyDomain("zz", "sccp/v1/move", vectors.move.payload, vectors.move.signature_hex)).toBe(false);
  });
});

