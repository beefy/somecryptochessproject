/**
 * Folded stream commitments, ported from `sccp/protocol/commitments.py`
 * (`PROTOCOL.md` section 8.5).
 *
 * Live play signs every ply with Ed25519; the *claim* never rests on that alone.
 * Both sides fold the whole stream into a 32-byte root and hybrid-sign the root
 * once — inside a game result (`move_commitment`) or inside a witness
 * checkpoint. This module reproduces the fold exactly, so a browser can verify
 * a result's commitment and a refused-checkmate bundle without any ML-DSA code:
 *
 * ```text
 * root_0     = 32 zero bytes
 * root_{i+1} = sha256("sccp/v1/commitment" || 0x00 || root_i || canonical_json(leaf_i))
 * ```
 *
 * Each leaf is canonical JSON with sorted keys, the same serialization every
 * other hash uses, so the bytes here match the Python bytes exactly.
 */

import { sha256 } from "@noble/hashes/sha2.js";

import { canonicalJson, fromHex, toHex } from "./canonical";

/** The fold's starting value, as lowercase hex. */
export const ZERO_ROOT = "00".repeat(32);

const FOLD_PREFIX = new TextEncoder().encode("sccp/v1/commitment\u0000");

/** One committed ply: the leaf a seat, a relay and a verifier all agree on. */
export interface MoveLeaf {
  network: string;
  matchId: string;
  seat: string;
  ply: number;
  san: string;
}

/** The committed form of one ply, in the protocol's key spelling. */
export function moveLeaf(leaf: MoveLeaf): Record<string, unknown> {
  return {
    match_id: leaf.matchId,
    network: leaf.network,
    ply: leaf.ply,
    san: leaf.san,
    seat: leaf.seat,
  };
}

/** Fold any sequence of leaf objects into a 32-byte root, returned as hex. */
export function foldCommitment(leaves: readonly Record<string, unknown>[]): string {
  let root = fromHex(ZERO_ROOT);
  for (const leaf of leaves) {
    const body = new TextEncoder().encode(canonicalJson(leaf));
    const combined = new Uint8Array(FOLD_PREFIX.length + root.length + body.length);
    combined.set(FOLD_PREFIX, 0);
    combined.set(root, FOLD_PREFIX.length);
    combined.set(body, FOLD_PREFIX.length + root.length);
    root = sha256(combined);
  }
  return toHex(root);
}

/** The colour that moves at zero-based SAN index `ply`. */
export function seatForPly(ply: number): "white" | "black" {
  return ply % 2 === 0 ? "white" : "black";
}

/** The committed root of a game's post-opening plies (`certificate_move_root`). */
export function certificateMoveRoot(
  network: string,
  matchId: string,
  seats: { white: string; black: string },
  sanMoves: readonly string[],
  startPly: number,
): string {
  const leaves: Record<string, unknown>[] = [];
  for (let ply = startPly; ply < sanMoves.length; ply += 1) {
    leaves.push(
      moveLeaf({
        network,
        matchId,
        seat: seats[seatForPly(ply)],
        ply,
        san: sanMoves[ply] as string,
      }),
    );
  }
  return foldCommitment(leaves);
}

/** The root of an evidence bundle's `{ply, seat, san}` list. */
export function evidenceRoot(
  network: string,
  matchId: string,
  moves: readonly { ply: number; seat: string; san: string }[],
): string {
  return foldCommitment(moves.map((move) => moveLeaf({ network, matchId, ...move })));
}
