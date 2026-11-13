import { describe, expect, it } from "vitest";

import { fromHex } from "./canonical";
import { addressFor, generateKeypair, keypairFromSeed, publicKeyHex, signDomain, verifyDomain } from "./keys";
import { movePayload } from "./protocol";
import vectors from "./vectors.json";

const keys = vectors.keys;

describe("keys", () => {
  it("derives the documented public key from a seed", () => {
    const keypair = keypairFromSeed(fromHex(keys.seed_hex));
    expect(publicKeyHex(keypair)).toBe(keys.public_key_hex);
  });

  it("derives the same addresses as sccp/crypto/address.py", () => {
    const keypair = keypairFromSeed(fromHex(keys.seed_hex));
    expect(addressFor(keypair.publicKey, "testnet")).toBe(keys.address_testnet);
    expect(addressFor(keypair.publicKey, "mainnet")).toBe(keys.address_mainnet);
    // The network is inside the address hash, so devnet and testnet share an HRP
    // but must not share an address with mainnet.
    expect(addressFor(keypair.publicKey, "devnet")).not.toBe(keys.address_mainnet);
  });

  it("verifies the move signature Python produced", () => {
    expect(verifyDomain(keys.public_key_hex, "sccp/v1/move", vectors.move.payload, vectors.move.signature_hex)).toBe(
      true,
    );
    expect(verifyDomain(keys.public_key_hex, "sccp/v1/result", vectors.move.payload, vectors.move.signature_hex)).toBe(
      false,
    );
  });

  it("verifies the result signature Python produced", () => {
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
    const keypair = keypairFromSeed(fromHex(keys.seed_hex));
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
