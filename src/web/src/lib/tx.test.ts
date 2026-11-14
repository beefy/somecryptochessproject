import { describe, expect, it } from "vitest";

import { certificateMoveRoot, evidenceRoot, foldCommitment, moveLeaf, ZERO_ROOT } from "./commitment";
import { fromHex } from "./canonical";
import {
  HYBRID_SIGNATURE_HEX_LENGTH,
  hybridKeypairFromSeedHex,
  hybridVerify,
} from "./keys";
import {
  LEDGER_SIGNATURE_HEX_LENGTH,
  availabilityCommitment,
  buildAvailability,
  buildRegister,
  buildReveal,
  isLedgerSignatureHex,
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

  it("signs a register transaction the chain's hybrid verifier accepts", () => {
    const keypair = hybridKeypairFromSeedHex(ledger.seed_hex);
    const unsigned = unsignedRegister();
    const signed = signTransaction(keypair, unsigned);
    expect(signed.signature).toHaveLength(LEDGER_SIGNATURE_HEX_LENGTH);
    expect(LEDGER_SIGNATURE_HEX_LENGTH).toBe(HYBRID_SIGNATURE_HEX_LENGTH);
    expect(isLedgerSignatureHex(signed.signature)).toBe(true);
    // Signing covers the txid, not the envelope, so the id is unchanged by it.
    expect(txId(signed)).toBe(ledger.register.txid);
    expect(hybridVerify(ledger.public_key_hex, fromHex(txId(signed)), signed.signature as string)).toBe(true);
  });

  it("verifies the hybrid signature the CLI produced for the same txid", () => {
    expect(hybridVerify(ledger.public_key_hex, fromHex(ledger.register.txid), wire.signature)).toBe(true);
    // A signature over another transaction's digest is not this transaction's.
    const other = signTransaction(hybridKeypairFromSeedHex(ledger.seed_hex), {
      ...unsignedRegister(),
      nonce: wire.nonce + 1,
    });
    expect(hybridVerify(ledger.public_key_hex, fromHex(ledger.register.txid), other.signature as string)).toBe(
      false,
    );
  });

  it("recognises the hybrid signature the CLI produced", () => {
    expect(isLedgerSignatureHex(wire.signature)).toBe(true);
    expect(wire.signature).toHaveLength(LEDGER_SIGNATURE_HEX_LENGTH);
    // A 64-byte Ed25519 signature is not a ledger signature any more.
    expect(isLedgerSignatureHex("00".repeat(64))).toBe(false);
    expect(isLedgerSignatureHex(null)).toBe(false);
    expect(isLedgerSignatureHex(undefined)).toBe(false);
    expect(isLedgerSignatureHex(`02${"00".repeat(3373)}`)).toBe(false);
  });

  it("builds the register payload a seat needs", () => {
    const built = buildRegister({
      network: wire.network,
      sender: wire.from,
      nonce: wire.nonce,
      fee: wire.fee,
      bond: 0,
      endpoint: String(wire.payload.endpoint),
      engine: String(wire.payload.engine),
      publicKeyHex: ledger.public_key_hex,
    });
    expect(built).toStrictEqual(unsignedRegister());
    expect(txId(built)).toBe(ledger.register.txid);
  });

  it("reproduces the availability commitment", () => {
    const entry = ledger.availability_commitment;
    expect(availabilityCommitment(entry.address, entry.epoch, entry.nonce)).toBe(entry.commitment);
  });

  it("refuses an availability nonce that is not 32 bytes of hex", () => {
    expect(() => availabilityCommitment(ledger.availability_commitment.address, 3, "11")).toThrow();
  });

  it("builds availability and reveal payloads the chain's validators accept", () => {
    const publicKeyHex = ledger.public_key_hex;
    const entry = ledger.availability_commitment;
    const availability = buildAvailability({
      network: wire.network,
      sender: wire.from,
      nonce: 1,
      fee: wire.fee,
      epoch: 5,
      maxGames: 3,
      commitment: entry.commitment,
      publicKeyHex,
    });
    expect(availability.type).toBe("availability");
    expect(availability.payload).toStrictEqual({
      public_key: publicKeyHex,
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
      publicKeyHex,
    });
    expect(reveal.type).toBe("reveal");
    expect(reveal.payload).toStrictEqual({
      public_key: publicKeyHex,
      epoch: 6,
      nonce: entry.nonce,
    });
  });
});

describe("commitments", () => {
  it("starts at the zero root", () => {
    expect(foldCommitment([])).toBe(ZERO_ROOT);
    expect(ZERO_ROOT).toBe("00".repeat(32));
  });

  it("reproduces the move commitment a result signs", () => {
    expect(foldCommitment(vectors.commitment.leaves)).toBe(vectors.commitment.move_commitment);
  });

  it("folds the same leaves a certificate commits to", () => {
    const commitment = vectors.commitment;
    const leaves = commitment.leaves as { seat: string; ply: number; san: string }[];
    const last = leaves[leaves.length - 1] as { ply: number };

    expect(
      certificateMoveRoot(
        vectors.network,
        vectors.pairing.match_id,
        { white: vectors.keys.address_testnet, black: vectors.keys.peer_address_testnet },
        vectors.game.san_moves,
        0,
      ),
    ).toBe(commitment.move_commitment);

    expect(
      evidenceRoot(
        vectors.network,
        vectors.pairing.match_id,
        leaves.map((leaf) => ({ ply: leaf.ply, seat: leaf.seat, san: leaf.san })),
      ),
    ).toBe(commitment.move_commitment);
    expect(last.ply).toBe(leaves.length - 1);
  });

  it("changes when a ply changes", () => {
    const leaves = [...(vectors.commitment.leaves as Record<string, unknown>[])];
    const first = leaves[0] as { ply: number; seat: string; san: string };
    leaves[0] = moveLeaf({
      network: vectors.network,
      matchId: vectors.pairing.match_id,
      seat: first.seat,
      ply: first.ply,
      san: "e4",
    });
    expect(foldCommitment(leaves)).not.toBe(vectors.commitment.move_commitment);
  });
});

