/**
 * The handful of protocol rules the explorer recomputes in the browser.
 *
 * Every function here has a Python twin that produced its test vectors:
 * `match_id`/`white_seat` live in `sccp/protocol/pairing.py`, the move and result
 * payloads in `sccp/protocol/clocks.py` and `sccp/protocol/transcript.py`, and the
 * game hash in `sccp/chess/pgn.py`.
 */

import { sha256 } from "@noble/hashes/sha2.js";

import { DOMAINS, compareCodePoints, domainHex, toHex } from "./canonical";

export type Color = "white" | "black";
export type ResultValue = "1-0" | "0-1" | "1/2-1/2";
export type TerminationValue = "checkmate" | "draw" | "resignation" | "time_forfeit";

/** `chess.STARTING_FEN`, the start position every game hash commits to. */
export const STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

/** The colour tag separating the colour draw from any other beacon use. */
export const COLOUR_TAG = new TextEncoder().encode("color");

export class ProtocolError extends Error {
  override name = "ProtocolError";
}

/** Sort a match's two seats the way Python's `sorted()` does. */
export function sortedSeats(seats: readonly [string, string]): [string, string] {
  const [first, second] = seats;
  if (first === second) throw new ProtocolError("the two seats of a match must be distinct");
  return compareCodePoints(first, second) <= 0 ? [first, second] : [second, first];
}

/** `H("sccp/v1/match", {network, epoch, round, seats})` as hex. */
export function matchId(
  network: string,
  epoch: number,
  round: number,
  seats: readonly [string, string],
): string {
  return domainHex(DOMAINS.MATCH, {
    network,
    epoch,
    round,
    seats: sortedSeats(seats),
  });
}

/**
 * `sha256(beacon || "color" || match_id)[:8]` decides colours: even means the
 * lexicographically first seat plays White.
 */
export function whiteSeat(beacon: Uint8Array, match: string, seats: readonly [string, string]): string {
  if (beacon.length !== 32) throw new ProtocolError(`beacon must be 32 bytes, got ${beacon.length}`);
  const [seatA, seatB] = sortedSeats(seats);
  const message = new Uint8Array(beacon.length + COLOUR_TAG.length + match.length);
  message.set(beacon, 0);
  message.set(COLOUR_TAG, beacon.length);
  message.set(new TextEncoder().encode(match), beacon.length + COLOUR_TAG.length);
  const digest = sha256(message);
  let low = 0n;
  for (const byte of digest.slice(0, 8)) low = (low << 8n) | BigInt(byte);
  return (low & 1n) === 0n ? seatA : seatB;
}

export interface MoveFields {
  network: string;
  match_id: string;
  /** The **address** of the seat that played the move, not its colour. */
  seat: string;
  ply: number;
  san: string;
}

/** The exact payload a seat signs for one move (PROTOCOL.md section 8.4). */
export function movePayload(move: MoveFields): Record<string, string | number> {
  return {
    network: move.network,
    match_id: move.match_id,
    seat: move.seat,
    ply: move.ply,
    san: move.san,
  };
}

export interface ResultFields {
  network: string;
  match_id: string;
  game_hash: string;
  result: ResultValue;
  termination: TerminationValue;
  plies: number;
}

/** The exact payload both seats sign when the game ends (section 8.5). */
export function resultPayload(result: ResultFields): Record<string, string | number> {
  return {
    network: result.network,
    match_id: result.match_id,
    game_hash: result.game_hash,
    result: result.result,
    termination: result.termination,
    plies: result.plies,
  };
}

/**
 * `H("sccp/v1/game", {network, start_fen, san_list, result})` as hex.
 *
 * Headers, clocks and PGN formatting are excluded on purpose: two transcripts of
 * the same moves and result hash identically.
 */
export function canonicalGameHash(
  network: string,
  sanMoves: readonly string[],
  result: ResultValue,
  startFen: string = STARTING_FEN,
): string {
  return domainHex(DOMAINS.GAME, {
    network,
    start_fen: startFen,
    san_list: [...sanMoves],
    result,
  });
}

/** `sha256` of an arbitrary string, used for the demo table's beacon. */
export function sha256Hex(text: string): string {
  return toHex(sha256(new TextEncoder().encode(text)));
}

/** Turn a room code into the 32-byte beacon that decides colours, deterministic per room. */
export function beaconForRoom(room: string): Uint8Array {
  return sha256(new TextEncoder().encode(`sccp-web/beacon\u0000${room.trim().toLowerCase()}`));
}

/** The result implied by a finished chess.js game, or `null` while it is live. */
export function resultFor(outcome: {
  winner?: Color | undefined;
  draw?: boolean;
}): { result: ResultValue; termination: TerminationValue } | null {
  if (outcome.winner === "white") return { result: "1-0", termination: "checkmate" };
  if (outcome.winner === "black") return { result: "0-1", termination: "checkmate" };
  if (outcome.draw === true) return { result: "1/2-1/2", termination: "draw" };
  return null;
}
