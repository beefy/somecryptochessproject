import { describe, expect, it } from "vitest";

import { fromHex } from "./canonical";
import { keypairFromSeedHex, publicKeyHex, verifyDigest } from "./keys";
import {
  HYBRID_SIGNATURE_HEX_LENGTH,
  availabilityCommitment,
  buildAvailability,
  buildRegister,
  buildReveal,
  isHybridSignatureHex,
  signTransaction,
  txId,
  type Transaction,
} from "./tx";
import vectors from "./vectors.json";

const ledger = vectors.ledger;
const wire = ledger.register.transaction;

function unsignedRegister(): Transaction {
  return {
    network: wire.network,
    type: "register",
    from: wire.from,
    nonce: wire.nonce,
    fee: wire.fee,
    payload: wire.payload,
  };
}

describe("ledger transactions", () => {
  it("hashes a register transaction to the Python txid", () => {
    expect(txId(unsignedRegister())).toBe(ledger.register.txid);
  });

  it("signs a register transaction exactly like Python", () => {
    const keypair = keypairFromSeedHex(ledger.seed_hex);
    const signed = signTransaction(keypair, unsignedRegister());
    expect(signed.signature).toBe(wire.signature);
    expect(signed).toStrictEqual(wire);
  });

  it("signs the txid, not the wire JSON", () => {
    const keypair = keypairFromSeedHex(ledger.seed_hex);
    const signed = signTransaction(keypair, unsignedRegister());
    const digest = fromHex(txId(signed));
    expect(verifyDigest(publicKeyHex(keypair), digest, signed.signature as string)).toBe(true);
    expect(verifyDigest(publicKeyHex(keypair), digest, "00".repeat(64))).toBe(false);
  });

  it("builds the register payload a seat needs", () => {
    const keypair = keypairFromSeedHex(ledger.seed_hex);
    const built = buildRegister({
      network: wire.network,
      sender: wire.from,
      nonce: wire.nonce,
      fee: wire.fee,
      bond: 0,
      endpoint: String(wire.payload.endpoint),
      engine: String(wire.payload.engine),
      publicKeyHex: publicKeyHex(keypair),
    });
    expect(built).toStrictEqual(unsignedRegister());
    expect(txId(built)).toBe(ledger.register.txid);
  });

  it("reproduces the availability commitment", () => {
    const entry = ledger.availability_commitment;
    expect(availabilityCommitment(entry.address, entry.epoch, entry.nonce)).toBe(entry.commitment);
  });

  it("tells a hybrid signature from an Ed25519 one", () => {
    expect(isHybridSignatureHex(wire.signature)).toBe(false);
    expect(isHybridSignatureHex(null)).toBe(false);
    expect(isHybridSignatureHex(undefined)).toBe(false);
    expect(isHybridSignatureHex(`02${"00".repeat(3373)}`)).toBe(false);
    const hybrid = `01${"ab".repeat(3373)}`;
    expect(hybrid).toHaveLength(HYBRID_SIGNATURE_HEX_LENGTH);
    expect(isHybridSignatureHex(hybrid)).toBe(true);
  });

  it("refuses an availability nonce that is not 32 bytes of hex", () => {
    expect(() => availabilityCommitment(ledger.availability_commitment.address, 3, "11")).toThrow();
  });

  it("builds availability and reveal payloads the chain's validators accept", () => {
    const keypair = keypairFromSeedHex(ledger.seed_hex);
    const entry = ledger.availability_commitment;
    const availability = buildAvailability({
      network: wire.network,
      sender: wire.from,
      nonce: 1,
      fee: wire.fee,
      epoch: 5,
      maxGames: 3,
      commitment: entry.commitment,
      publicKeyHex: publicKeyHex(keypair),
    });
    expect(availability.type).toBe("availability");
    expect(availability.payload).toStrictEqual({
      public_key: publicKeyHex(keypair),
      epoch: 5,
      max_games: 3,
      commitment: entry.commitment,
    });
    const reveal = buildReveal({
      network: wire.network,
      sender: wire.from,
      nonce: 2,
      fee: wire.fee,
      epoch: 6,
      nonceSecret: entry.nonce,
      publicKeyHex: publicKeyHex(keypair),
    });
    expect(reveal.type).toBe("reveal");
    expect(reveal.payload).toStrictEqual({
      public_key: publicKeyHex(keypair),
      epoch: 6,
      nonce: entry.nonce,
    });
  });
});
