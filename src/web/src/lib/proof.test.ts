import { Chess } from "chess.js";
import { describe, expect, it } from "vitest";

import type { GameResult, MatchCertificate, WinProof } from "../types";
import { fromHex } from "./canonical";
import { certificatePayload, openingHash, signCertificate } from "./certificate";
import { keypairFromSeedHex } from "./keys";
import { ProofBuildError, buildWinProof, proofId, webQualitySummary } from "./proof";
import { whiteSeat } from "./protocol";
import { type VerifyContext, verifyProof } from "./verify";
import vectors from "./vectors.json";

const match = vectors.match;
const certificate = match.certificate as unknown as MatchCertificate;
const result = match.result as unknown as GameResult;
const proof = match.proof as unknown as WinProof;
const proofParams = match.params as unknown as VerifyContext["params"];
const context = {
  params: proofParams,
  beacon: match.beacon,
  publicKeys: match.public_keys,
} as unknown as VerifyContext;

describe("match certificates", () => {
  it("rebuilds the payload both seats signed", () => {
    expect(certificatePayload(certificate)).toStrictEqual(match.certificate_payload);
  });

  it("carries a well-formed hybrid start signature per seat", () => {
    // ML-DSA-65 is hedged and this bundle cannot produce it, so the bytes are
    // pinned by the Python generator rather than reproduced here; what a browser
    // can check is the shape (scheme byte 0x01, 3374 bytes).
    for (const seat of ["white", "black"] as const) {
      const signature = certificate.start_signatures[seat];
      expect(signature).toHaveLength(6748);
      expect(signature?.startsWith("01")).toBe(true);
    }
  });

  it("commits to the book line it names", () => {
    expect(openingHash(certificate.opening)).toBe(certificate.opening.hash);
  });
});

describe("win proofs", () => {
  it("reproduces the proof Python built from the same transcript", () => {
    const built = buildWinProof({
      certificate,
      pgn: match.pgn,
      sanMoves: match.san_moves,
      result,
      resultSignatures: result.result_signatures,
      params: proofParams,
    });
    expect(built).toStrictEqual(proof);
  });

  it("derives the proof id from the game hash, match and winner", () => {
    expect(proofId(proof.game_hash, proof.match_id, proof.winner)).toBe(proof.proof_id);
  });

  it("replays the PGN to the same SAN list the relay published", () => {
    const board = new Chess();
    board.loadPgn(match.pgn);
    expect(board.history()).toStrictEqual(match.san_moves);
    expect(board.isCheckmate()).toBe(true);
  });

  it("refuses a transcript whose moves do not hash to the co-signed result", () => {
    expect(() =>
      buildWinProof({
        certificate,
        pgn: match.pgn,
        sanMoves: match.san_moves.slice(0, -1),
        result,
        resultSignatures: result.result_signatures,
        params: proofParams,
      }),
    ).toThrow(ProofBuildError);
  });

  it("demands a quality summary only where the network samples", () => {
    const sampled = { ...proofParams, quality_mode: "sampled" as const };
    expect(() =>
      buildWinProof({
        certificate,
        pgn: match.pgn,
        sanMoves: match.san_moves,
        result,
        resultSignatures: result.result_signatures,
        params: sampled,
      }),
    ).toThrow(/quality_summary/);
    const summary = webQualitySummary(certificate.engine_white);
    expect(summary.verdict).toBe("unknown");
    const built = buildWinProof({
      certificate,
      pgn: match.pgn,
      sanMoves: match.san_moves,
      result,
      resultSignatures: result.result_signatures,
      params: sampled,
      qualitySummary: summary,
    });
    expect(built.quality_summary).toStrictEqual(summary);
  });
});

describe("the browser verifier", () => {
  it("passes every locally-checkable rule on the Python proof", () => {
    const verdict = verifyProof(proof, context);
    const failed = verdict.checks.filter((check) => !check.ok);
    expect(failed).toStrictEqual([]);
    expect(verdict.ok).toBe(true);
    expect(verdict.checks).toHaveLength(15);
  });

  it("catches a wrong low-ply flag", () => {
    const verdict = verifyProof({ ...proof, low_ply: !proof.low_ply }, context);
    expect(verdict.ok).toBe(false);
    expect(verdict.checks.find((check) => check.rule === "low ply flag")?.ok).toBe(false);
  });

  it("catches a rewritten game hash", () => {
    const verdict = verifyProof({ ...proof, game_hash: "00".repeat(32) }, context);
    const failed = verdict.checks.filter((check) => !check.ok).map((check) => check.rule);
    expect(failed).toContain("game hash");
    expect(failed).toContain("proof id");
  });

  it("reports the hybrid signature rules as skipped without a verifier", () => {
    const verdict = verifyProof(proof, context);
    for (const rule of ["start signatures", "result signatures"]) {
      const check = verdict.checks.find((item) => item.rule === rule);
      expect(check?.skipped).toBe(true);
      expect(check?.ok).toBe(true);
    }
    expect(verdict.ok).toBe(true);
  });

  it("catches a forged start signature once a verifier is supplied", () => {
    const tampered: WinProof = {
      ...proof,
      certificate: {
        ...certificate,
        start_signatures: {
          ...certificate.start_signatures,
          white: "00".repeat(6748),
        },
      },
    };
    const strict = verifyProof(tampered, { ...context, verifyLedgerSignature: () => false });
    expect(strict.checks.find((check) => check.rule === "start signatures")?.ok).toBe(false);
    expect(strict.ok).toBe(false);
  });

  it("catches a proof checked against the wrong beacon", () => {
    // A different beacon may draw the same colour, so search for one that does
    // not: the check must fail on the draw, not on the hash being unknown.
    const seats = [certificate.white, certificate.black] as const;
    let flipped: string | null = null;
    for (let index = 0; index < 64 && flipped === null; index += 1) {
      const candidate = index.toString(16).padStart(2, "0").repeat(32);
      if (whiteSeat(fromHex(candidate), proof.match_id, seats) !== certificate.white) {
        flipped = candidate;
      }
    }
    expect(flipped).not.toBeNull();
    const verdict = verifyProof(proof, {
      ...context,
      beacon: { ...context.beacon, hash: flipped as string },
    });
    expect(verdict.ok).toBe(false);
    expect(verdict.checks.find((check) => check.rule === "colour draw")?.ok).toBe(false);
  });

  it("catches a proof checked against another epoch's beacon", () => {
    const verdict = verifyProof(proof, {
      ...context,
      beacon: { ...context.beacon, epoch: context.beacon.epoch + 1 },
    });
    expect(verdict.checks.find((check) => check.rule === "epoch binding")?.ok).toBe(false);
  });
});
