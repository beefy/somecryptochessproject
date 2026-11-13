/**
 * Win proofs: the document a winner announces to the node.
 *
 * Ported from `sccp/protocol/transcript.py::build_win_proof`. A browser seat has
 * no privileged position here: the proof it builds is the same shape a
 * Stockfish miner builds from the same co-signed transcript, so the node's
 * `POST /v1/proofs` admission runs the identical `check_proof` on it.
 *
 * Two rules are worth restating, because they bite a hand-played game:
 *
 * - `low_ply` is derived, not chosen: a game shorter than `params.min_plies`
 *   is a low-ply win, which a deployment may treat as a weaker claim.
 * - a sampled network requires a `quality_summary` field on every proof. A
 *   browser has no analysis engine, so an honest summary can only say
 *   `verdict: "unknown"`; a validator that samples it may disagree and open a
 *   bonded challenge. That is the protocol working as designed, not a bug here.
 */

import type {
  ClockEvidence,
  GameResult,
  MatchCertificate,
  QualitySummary,
  ResultValue,
  MoveEvidence,
  WinProof,
} from "../types";
import { DOMAINS, domainHex } from "./canonical";
import { certificateMoveRoot } from "./commitment";
import { canonicalGameHash } from "./protocol";

/** The network constants a proof is checked against. */
export interface ProofParams {
  network_id: string;
  min_plies: number;
  quality_mode: "off" | "sampled";
}

export interface BuildProofFields {
  /** The relay's co-signed transcript pieces. */
  certificate: MatchCertificate;
  pgn: string;
  sanMoves: readonly string[];
  result: GameResult;
  /** `result.result_signatures`: one per seat, hex. */
  resultSignatures: Record<string, string>;
  params: ProofParams;
  qualitySummary?: QualitySummary | null;
  clockEvidence?: ClockEvidence | null;
  moveEvidence?: MoveEvidence | null;
  timeoutClaimId?: string | null;
}

export class ProofBuildError extends Error {
  override name = "ProofBuildError";
}

/** The winning colour implied by a decisive result, or `null` for a draw. */
export function winnerSeat(result: ResultValue): "white" | "black" | null {
  if (result === "1-0") return "white";
  if (result === "0-1") return "black";
  return null;
}

/** `H("sccp/v1/proof", {game_hash, match_id, winner})` as hex. */
export function proofId(gameHash: string, matchId: string, winner: string): string {
  return domainHex(DOMAINS.PROOF, { game_hash: gameHash, match_id: matchId, winner });
}

/**
 * Wrap a co-signed result into the proof the node expects.
 *
 * Every inconsistency the chain would reject is rejected here first, so a
 * failure shows up next to the board instead of as a mempool error.
 */
export function buildWinProof(fields: BuildProofFields): WinProof {
  const { certificate, result, params } = fields;
  if (certificate.network !== params.network_id) {
    throw new ProofBuildError(
      `certificate is for ${certificate.network}, this node is ${params.network_id}`,
    );
  }
  for (const colour of ["white", "black"] as const) {
    if (certificate.start_signatures[colour] === undefined) {
      throw new ProofBuildError(`the certificate is missing the ${colour} start signature`);
    }
  }
  const seat = winnerSeat(result.result);
  if (seat === null) {
    throw new ProofBuildError(`a win proof needs a decisive result, not ${result.result}`);
  }
  const winner = seat === "white" ? certificate.white : certificate.black;
  const loser = seat === "white" ? certificate.black : certificate.white;
  for (const colour of ["white", "black"] as const) {
    if (fields.resultSignatures[colour] === undefined) {
      throw new ProofBuildError(`the proof is missing the ${colour} result signature`);
    }
  }
  if (result.plies !== fields.sanMoves.length) {
    throw new ProofBuildError(
      `the result claims ${result.plies} plies, the move list has ${fields.sanMoves.length}`,
    );
  }
  const gameHash = canonicalGameHash(certificate.network, fields.sanMoves, result.result);
  if (gameHash !== result.game_hash) {
    throw new ProofBuildError(
      `the moves hash to ${gameHash}, the co-signed result says ${result.game_hash}`,
    );
  }
  if (result.match_id !== certificate.match_id) {
    throw new ProofBuildError("the result belongs to another match");
  }
  if (fields.clockEvidence != null && result.termination !== "time_forfeit") {
    throw new ProofBuildError("clock_evidence is only valid for a time forfeit");
  }
  if (fields.moveEvidence != null && result.termination !== "checkmate") {
    throw new ProofBuildError("move_evidence is only valid for a checkmate");
  }
  if (fields.timeoutClaimId != null && result.termination !== "time_forfeit") {
    throw new ProofBuildError("timeout_claim_id is only valid for a time forfeit");
  }
  const quality = fields.qualitySummary ?? null;
  if (params.quality_mode === "sampled" && quality === null) {
    throw new ProofBuildError(
      `network ${params.network_id} requires a quality_summary on every proof`,
    );
  }

  const moveCommitment = certificateMoveRoot(
    certificate.network,
    certificate.match_id,
    { white: certificate.white, black: certificate.black },
    fields.sanMoves,
    certificate.opening.moves.length,
  );
  if (moveCommitment !== result.move_commitment) {
    throw new ProofBuildError(
      "the moves do not fold to the move_commitment the result was signed over",
    );
  }

  return {
    network: certificate.network,
    proof_id: proofId(gameHash, certificate.match_id, winner),
    game_hash: gameHash,
    match_id: certificate.match_id,
    epoch: certificate.epoch,
    round: certificate.round,
    winner,
    loser,
    winner_seat: seat,
    result: result.result,
    termination: result.termination,
    plies: result.plies,
    move_commitment: moveCommitment,
    low_ply: result.plies < params.min_plies,
    opening: certificate.opening,
    pgn: fields.pgn,
    certificate,
    result_signatures: { ...fields.resultSignatures },
    clock_evidence: fields.clockEvidence ?? null,
    move_evidence: fields.moveEvidence ?? null,
    quality_summary: quality,
    timeout_claim_id: fields.timeoutClaimId ?? null,
  };
}

/**
 * The honest summary a browser can attach when a network demands one.
 *
 * `verdict: "unknown"` is the only truthful value without an analysis engine:
 * claiming `pass` would be a false claim about a game nobody analysed, and the
 * field exists so a validator can sample and challenge it.
 */
export function webQualitySummary(engineFingerprint: string | null): QualitySummary {
  return {
    policy: "sccp-web/hand-play-1",
    verdict: "unknown",
    trace_hash: null,
    engine_fingerprint: engineFingerprint,
    white_blunders: 0,
    black_blunders: 0,
    worst_cpl: 0,
  };
}
