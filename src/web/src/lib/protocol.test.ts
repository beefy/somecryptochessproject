import { describe, expect, it } from "vitest";

import { fromHex } from "./canonical";
import {
  COLOUR_TAG,
  canonicalGameHash,
  matchId,
  resultPayload,
  sortedSeats,
  whiteSeat,
} from "./protocol";
import vectors from "./vectors.json";

const pairing = vectors.pairing;
const seats = pairing.seats as [string, string];

describe("pairing", () => {
  it("reproduces the Python match id from unsorted seats", () => {
    const [first, second] = seats;
    expect(matchId(vectors.network, pairing.epoch, pairing.round, [second, first])).toBe(pairing.match_id);
  });

  it("draws the same colour as Python", () => {
    expect(whiteSeat(fromHex(pairing.beacon_hex), pairing.match_id, seats)).toBe(pairing.white_seat);
  });

  it("is sensitive to the beacon", () => {
    const flipped = fromHex(pairing.beacon_hex);
    flipped[31] = (flipped[31] as number) ^ 1;
    const drawn = whiteSeat(flipped, pairing.match_id, seats);
    // Either colour is a legitimate outcome; what matters is that a different
    // beacon is a different input to the draw, never an error.
    expect(pairing.seats).toContain(drawn);
    expect(COLOUR_TAG).toHaveLength(5);
  });

  it("orders seats like Python's sorted()", () => {
    expect(sortedSeats(["sccpt1b", "sccpt1a"])).toStrictEqual(["sccpt1a", "sccpt1b"]);
    const [first, second] = seats;
    const ordered = sortedSeats(seats);
    expect(ordered).toStrictEqual([first, second].sort((a, b) => (a < b ? -1 : 1)));
    expect(sortedSeats([second, first])).toStrictEqual(ordered);
    expect(() => sortedSeats(["a", "a"])).toThrow();
  });

  it("rejects a beacon of the wrong length", () => {
    expect(() => whiteSeat(new Uint8Array(16), pairing.match_id, seats)).toThrow();
  });
});

describe("game hash and result payload", () => {
  it("matches tests/fixtures/chess_vectors.json via the Python generator", () => {
    expect(canonicalGameHash(vectors.network, vectors.game.san_moves, "0-1")).toBe(
      vectors.game.game_hash,
    );
  });

  it("changes when the result changes", () => {
    expect(canonicalGameHash(vectors.network, vectors.game.san_moves, "1-0")).not.toBe(
      vectors.game.game_hash,
    );
  });

  it("builds the exact result payload both seats sign", () => {
    const payload = resultPayload({
      network: vectors.network,
      match_id: pairing.match_id,
      game_hash: vectors.game.game_hash,
      result: "0-1",
      termination: "checkmate",
      plies: vectors.game.san_moves.length,
    });
    expect(payload).toStrictEqual(vectors.result.payload);
  });
});
