import { describe, expect, it } from "vitest";

import { VariantGame, chessJsStartFen, pgnSanMoves } from "./variantGame";

// A legal Chess960 back rank whose king already starts on g1. Kingside
// castling therefore does not move the king at all, which `chess.js` cannot
// represent with its standard e1/rook-h1 logic.
const START = "4k3/4p3/8/8/8/8/8/R5KR w HA - 0 1";

describe("Chess960 adapter", () => {
  it("hides Shredder castling files from chess.js", () => {
    expect(chessJsStartFen(START, "chess960")).toBe(
      "4k3/4p3/8/8/8/8/8/R5KR w - - 0 1",
    );
    expect(chessJsStartFen(START, "standard")).toBe(START);
  });

  it("applies a castling move where the king stays on g1", () => {
    const board = new VariantGame("chess960", START);
    expect(board.targets("g1")).toContain("g1");
    expect(board.sanFor("g1", "g1")).toBe("O-O");
    expect(board.move("O-O")).toBe(true);
    expect(board.get("f1")?.type).toBe("r");
    expect(board.get("g1")?.type).toBe("k");
    expect(board.get("h1")).toBeUndefined();
    expect(board.turn).toBe("b");
  });

  it("keeps castling rights while unrelated pieces move", () => {
    const board = new VariantGame("chess960", START);
    expect(board.move("Ra2")).toBe(true);
    expect(board.move("e5")).toBe(true);
    expect(board.castleSans()).toStrictEqual(["O-O"]);
    expect(board.move("O-O")).toBe(true);
    expect(board.get("f1")?.type).toBe("r");
  });

  it("replays the same castle from a PGN", () => {
    const board = new VariantGame("chess960", START);
    expect(
      board.loadPgn('[Event "x"]\n[SetUp "1"]\n[FEN "x"]\n\n1. O-O e5 2. Kh2 *'),
    ).toBe(true);
    expect(board.history()).toStrictEqual(["O-O", "e5", "Kh2"]);
  });

  it("extracts SAN without comments, numbers or a result", () => {
    expect(pgnSanMoves('[Result "1-0"]\n\n1. e4 {best} e5 2. Nf3 (2. d4) Nc6 1-0')).toStrictEqual([
      "e4",
      "e5",
      "Nf3",
      "Nc6",
    ]);
  });
});
