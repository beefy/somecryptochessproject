import { describe, expect, it } from "vitest";

import { fromHex } from "./canonical";
import {
  addressFor,
  ed25519HalfHex,
  generateKeypair,
  hybridEd25519Keypair,
  keypairFromSeed,
  publicKeyHex,
  signDomain,
  verifyDomain,
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

