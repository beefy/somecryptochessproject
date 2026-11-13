/**
 * An in-browser verifier for win proofs.
 *
 * This is the read-only half of `sccp/protocol/verification.py`: every rule that
 * needs nothing but the proof, the network constants and the epoch beacon. It
 * deliberately does **not** claim to be the whole check — a block also needs the
 * winner on the epoch roster, inside the claim window, and as the block's own
 * miner (`sccp/chain/block.py::check_proof`). What it does prove is that the
 * document is self-consistent: the seats signed this certificate, both seats
 * signed this result, the book line is the one hashed in the certificate, and
 * the moves replay to the claimed game in the claimed number of plies.
 *
 * Each rule is reported separately so the page can show a table instead of a
 * single opaque "invalid".
 */

import { Chess } from "chess.js";

import type { WinProof } from "../types";
import { DOMAINS, fromHex } from "./canonical";
import { certificatePayload, openingHash } from "./certificate";
import { verifyDomain } from "./keys";
import { canonicalGameHash, matchId, resultPayload, whiteSeat } from "./protocol";
import { proofId } from "./proof";

export interface VerifyParams {
  network_id: string;
  rounds_per_epoch: number;
  min_plies: number;
  quality_mode: "off" | "sampled";
}

export interface VerifyContext {
  params: VerifyParams;
  /** The epoch's beacon as the *chain* derived it, never the proof's claim. */
  beacon: { network: string; epoch: number; hash: string };
  /** Registered public keys of the two seats (from the epoch roster). */
  publicKeys: Record<string, string>;
}

export interface CheckResult {
  rule: string;
  ok: boolean;
  detail: string;
}

function require(condition: unknown, message: string): void {
  if (!condition) throw new Error(message);
}

/** The fixed v1 control: 30 minutes, no increment. */
const FIXED_INITIAL_MS = 1_800_000;

/**
 * Run every locally-checkable proof rule.
 *
 * The returned `ok` is the conjunction of the checks; the caller may still
 * display the table while some fail, which is the point.
 */
export function verifyProof(
  proof: WinProof,
  context: VerifyContext,
): { ok: boolean; checks: CheckResult[] } {
  const checks: CheckResult[] = [];
  const replay = new Chess();

  const run = (rule: string, body: () => string): void => {
    try {
      checks.push({ rule, ok: true, detail: body() });
    } catch (error) {
      checks.push({
        rule,
        ok: false,
        detail: error instanceof Error ? error.message : String(error),
      });
    }
  };

  run("network binding", () => {
    require(proof.network === context.params.network_id, `proof is for ${proof.network}`);
    require(proof.certificate.network === proof.network, "certificate names another network");
    require(context.beacon.network === proof.network, "beacon names another network");
    return proof.network;
  });

  run("epoch binding", () => {
    require(context.beacon.epoch === proof.epoch, `beacon is for epoch ${context.beacon.epoch}`);
    return `epoch ${proof.epoch}`;
  });

  run("certificate shape", () => {
    const cert = proof.certificate;
    require(cert.match_id === proof.match_id, "certificate match id differs from the proof");
    require(cert.epoch === proof.epoch, "certificate epoch differs from the proof");
    require(cert.round === proof.round, "certificate round differs from the proof");
    require(cert.white !== cert.black, "the two seats are the same address");
    require(proof.round < context.params.rounds_per_epoch, "round is outside the epoch");
    const seats = [cert.white, cert.black];
    require(seats.includes(proof.winner), "the winner is not a seat");
    require(seats.includes(proof.loser), "the loser is not a seat");
    require(proof.winner !== proof.loser, "the winner and loser are the same seat");
    return `${cert.white} vs ${cert.black}`;
  });

  run("match id", () => {
    const expected = matchId(proof.network, proof.epoch, proof.round, [
      proof.certificate.white,
      proof.certificate.black,
    ]);
    require(expected === proof.match_id, `seats hash to ${expected}`);
    return proof.match_id;
  });

  run("colour draw", () => {
    const expected = whiteSeat(fromHex(context.beacon.hash), proof.match_id, [
      proof.certificate.white,
      proof.certificate.black,
    ]);
    require(expected === proof.certificate.white, `the beacon gives White to ${expected}`);
    const winner = proof.result === "1-0" ? "white" : "black";
    require(proof.winner_seat === winner, "winner_seat disagrees with the result");
    return `white ${expected}`;
  });

  run("time control", () => {
    const control = proof.certificate.time_control;
    require(
      control.initial_ms === FIXED_INITIAL_MS && control.increment_ms === 0,
      `control is ${control.initial_ms}+${control.increment_ms}, v1 fixes 1800000+0`,
    );
    return "1800000+0";
  });

  run("opening commitment", () => {
    const opening = proof.opening;
    require(opening.moves.length >= 1, "the opening line is empty");
    require(
      openingHash(opening) === opening.hash,
      `book ${opening.book_version} line ${opening.line_id} hashes to ${openingHash(opening)}`,
    );
    const cert = proof.certificate.opening;
    require(
      cert.hash === opening.hash && cert.line_id === opening.line_id,
      "the proof and certificate name different openings",
    );
    require(
      cert.moves.length === opening.moves.length &&
        cert.moves.every((move, index) => move === opening.moves[index]),
      "opening moves differ between proof and certificate",
    );
    return `${opening.book_version} #${opening.line_id} (${opening.moves.length} plies)`;
  });

  run("pgn replay", () => {
    replay.loadPgn(proof.pgn);
    const history = replay.history();
    require(
      history.length === proof.plies,
      `the PGN has ${history.length} plies, the proof says ${proof.plies}`,
    );
    const prefix = proof.opening.moves;
    require(history.length >= prefix.length, "the game is shorter than its opening");
    require(
      prefix.every((move, index) => history[index] === move),
      "the PGN does not start with the certified opening line",
    );
    return `${history.length} plies`;
  });

  run("termination and result", () => {
    const history = replay.history();
    require(history.length >= 1, "a proof needs at least one move");
    if (proof.termination === "checkmate") {
      require(replay.isCheckmate(), "the replayed position is not checkmate");
      const expected = replay.turn() === "w" ? "0-1" : "1-0";
      require(proof.result === expected, `checkmate scores ${expected}, the proof says ${proof.result}`);
    } else if (proof.termination === "draw") {
      require(replay.isDraw() || replay.isStalemate(), "the replayed position is not a draw");
      require(proof.result === "1/2-1/2", "a draw must be scored 1/2-1/2");
    }
    return `${proof.result} by ${proof.termination}`;
  });

  run("game hash", () => {
    const expected = canonicalGameHash(proof.network, replay.history(), proof.result);
    require(expected === proof.game_hash, `the replayed game hashes to ${expected}`);
    return expected;
  });

  run("start signatures", () => {
    for (const seat of ["white", "black"] as const) {
      const address = seat === "white" ? proof.certificate.white : proof.certificate.black;
      const publicKey = context.publicKeys[address];
      if (publicKey === undefined) throw new Error(`no registered public key for ${address}`);
      const signature = proof.certificate.start_signatures[seat];
      if (signature === undefined) throw new Error(`the certificate has no ${seat} start signature`);
      require(
        verifyDomain(publicKey, DOMAINS.CERTIFICATE, certificatePayload(proof.certificate), signature),
        `the ${seat} start signature does not verify`,
      );
    }
    return "both seats signed the certificate";
  });

  run("result signatures", () => {
    const payload = resultPayload({
      network: proof.network,
      match_id: proof.match_id,
      game_hash: proof.game_hash,
      result: proof.result,
      termination: proof.termination,
      plies: proof.plies,
    });
    for (const seat of ["white", "black"] as const) {
      const address = seat === "white" ? proof.certificate.white : proof.certificate.black;
      const publicKey = context.publicKeys[address];
      if (publicKey === undefined) throw new Error(`no registered public key for ${address}`);
      const signature = proof.result_signatures[seat];
      if (signature === undefined) throw new Error(`no ${seat} result signature`);
      require(
        verifyDomain(publicKey, DOMAINS.RESULT, payload, signature),
        `the ${seat} result signature does not verify`,
      );
    }
    return "both seats signed the result";
  });

  run("low ply flag", () => {
    const expected = proof.plies < context.params.min_plies;
    require(
      proof.low_ply === expected,
      `low_ply is ${proof.low_ply} for ${proof.plies} plies (min_plies ${context.params.min_plies})`,
    );
    return expected ? `low ply (${proof.plies} < ${context.params.min_plies})` : "full length";
  });

  run("proof id", () => {
    const expected = proofId(proof.game_hash, proof.match_id, proof.winner);
    require(expected === proof.proof_id, `the fields recompute to ${expected}`);
    return expected;
  });

  run("quality summary", () => {
    if (context.params.quality_mode === "off") return "not required on this network";
    const summary = proof.quality_summary ?? null;
    if (summary === null) throw new Error("a sampled network requires a summary");
    return `${summary.policy}: ${summary.verdict}`;
  });

  return { ok: checks.every((check) => check.ok), checks };
}
