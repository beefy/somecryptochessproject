/**
 * A small Chess960 adapter for `chess.js`.
 *
 * `chess.js` understands arbitrary FENs but only generates castling moves the
 * standard way (king starts on e1/e8, rook on a/h). Chess960 needs both pieces to
 * end on c/d and g/f from wherever they started, so its `O-O`/`O-O-O` moves fail
 * to replay. This wrapper keeps the castling rights itself, applies those two
 * moves directly and leaves every other move to `chess.js`.
 *
 * The canonical start FEN stays a Shredder-FEN (rook files, not KQkq): it is part
 * of the game hash. `chess.js` sees the same position with castling masked out,
 * because its built-in castle logic only understands the standard back rank.
 */

import { Chess, type Color, type PieceSymbol, type Square } from "chess.js";

/** Whether `variant` needs the adapter rather than ordinary `chess.js`. */
export function isChess960(variant: string): boolean {
  return variant === "chess960";
}

interface Rights {
  white: { kingside: Square | null; queenside: Square | null };
  black: { kingside: Square | null; queenside: Square | null };
}

const FILES = "abcdefgh";

function backRank(color: Color): number {
  return color === "w" ? 1 : 8;
}

function square(file: number, rank: number): Square {
  return `${FILES[file]}${rank}` as Square;
}

function kingSquare(chess: Chess, color: Color): Square | null {
  return chess.findPiece({ type: "k", color })[0] ?? null;
}

/** Translate a Shredder-FEN castling field into the rook squares it names. */
function rightsFromFen(chess: Chess, fen: string): Rights {
  const field = fen.trim().split(/\s+/)[2] ?? "-";
  const rights: Rights = {
    white: { kingside: null, queenside: null },
    black: { kingside: null, queenside: null },
  };
  for (const colour of ["w", "b"] as const) {
    const king = kingSquare(chess, colour);
    if (king === null) continue;
    const kingFile = FILES.indexOf(king[0] ?? "e");
    const rank = backRank(colour);
    for (const char of field) {
      const file = FILES.indexOf(char.toLowerCase());
      if (file < 0) continue;
      if ((colour === "w") !== (char === char.toUpperCase())) continue;
      const candidate = square(file, rank);
      const piece = chess.get(candidate);
      if (piece?.type !== "r" || piece.color !== colour) continue;
      const side = colour === "w" ? rights.white : rights.black;
      if (file < kingFile && side.queenside === null) side.queenside = candidate;
      if (file > kingFile && side.kingside === null) side.kingside = candidate;
    }
  }
  return rights;
}

/**
 * The canonical FEN a game hashes, but with castling hidden from `chess.js`.
 *
 * Chess960 rights are tracked by {@link VariantGame}: `chess.js` only implements
 * the standard e1/rook-h1 castle, so handing it KQkq for a shuffled back rank
 * makes it invent illegal king moves. A `-` castling field leaves ordinary move
 * generation correct, while the adapter adds the real castle itself.
 */
export function chessJsStartFen(fen: string, variant: string): string {
  if (!isChess960(variant)) return fen;
  const fields = fen.trim().split(/\s+/);
  fields[2] = "-";
  return fields.slice(0, 6).join(" ");
}

function boardField(chess: Chess): string {
  const rows: string[] = [];
  for (const row of chess.board()) {
    let text = "";
    let empty = 0;
    for (const cell of row) {
      if (cell === null) {
        empty += 1;
        continue;
      }
      if (empty > 0) {
        text += String(empty);
        empty = 0;
      }
      text += cell.color === "w" ? cell.type.toUpperCase() : cell.type;
    }
    if (empty > 0) text += String(empty);
    rows.push(text);
  }
  return rows.join("/");
}

function fenWith(
  chess: Chess,
  turn: Color,
  halfMoves: number,
  moveNumber: number,
): string {
  return `${boardField(chess)} ${turn} - - ${halfMoves} ${moveNumber}`;
}

/** The moves a SAN-string PGN carries, without comments or result markers. */
export function pgnSanMoves(pgn: string): string[] {
  const start = pgn.search(/\n\s*\n/);
  const movetext = start >= 0 ? pgn.slice(start) : pgn;
  const text = movetext
    .replace(/\{[^}]*\}/g, " ")
    .replace(/;[^\n]*/g, " ")
    .replace(/\([^)]*\)/g, " ")
    .replace(/\$\d+/g, " ");
  const moves: string[] = [];
  for (const token of text.split(/\s+/)) {
    if (token === "") continue;
    if (/^\d+\.(\.\.)?$/.test(token)) continue;
    if (token === "1-0" || token === "0-1" || token === "1/2-1/2" || token === "*") continue;
    moves.push(token.replace(/[!?]+$/, ""));
  }
  return moves;
}


/** A replayable board for either standard chess or Chess960. */
export class VariantGame {
  readonly variant: string;
  readonly startFen: string;
  readonly chess: Chess;
  readonly moves: string[] = [];
  private rights: Rights;

  constructor(variant: string, startFen: string) {
    this.variant = variant;
    this.startFen = startFen;
    this.chess = new Chess(chessJsStartFen(startFen, variant));
    this.rights = rightsFromFen(this.chess, startFen);
  }

  get turn(): Color {
    return this.chess.turn();
  }

  fen(): string {
    return this.chess.fen();
  }

  history(): string[] {
    return [...this.moves];
  }

  board(): ReturnType<Chess["board"]> {
    return this.chess.board();
  }

  get(square: Square): ReturnType<Chess["get"]> {
    return this.chess.get(square);
  }

  isCheck(): boolean {
    return this.chess.isCheck();
  }

  isCheckmate(): boolean {
    return this.chess.isCheckmate();
  }

  isDraw(): boolean {
    return this.chess.isDraw();
  }

  isStalemate(): boolean {
    return this.chess.isStalemate();
  }

  /** The target squares a piece can move to, including Chess960 castling. */
  targets(from: Square): Square[] {
    const targets = this.chess
      .moves({ square: from, verbose: true })
      .map((move) => move.to as Square);
    const king = kingSquare(this.chess, this.turn);
    if (isChess960(this.variant) && king === from) {
      if (this.canCastle("kingside")) targets.push(square(6, backRank(this.turn)));
      if (this.canCastle("queenside")) targets.push(square(2, backRank(this.turn)));
    }
    return targets;
  }

  /** The SAN a castling move would use, when the king can castle now. */
  castleSans(): string[] {
    if (!isChess960(this.variant)) return [];
    const moves: string[] = [];
    if (this.canCastle("kingside")) moves.push("O-O");
    if (this.canCastle("queenside")) moves.push("O-O-O");
    return moves;
  }

  /** The SAN the relay should receive for a UI move. */
  sanFor(from: Square, to: Square, promotion: PieceSymbol = "q"): string | null {
    const move = this.chess
      .moves({ square: from, verbose: true })
      .find((candidate) => candidate.to === to && candidate.promotion === promotion);
    if (move !== undefined) return move.san;
    if (isChess960(this.variant) && this.get(from)?.type === "k") {
      const rank = backRank(this.turn);
      if (to === square(6, rank) && this.canCastle("kingside")) return "O-O";
      if (to === square(2, rank) && this.canCastle("queenside")) return "O-O-O";
    }
    return null;
  }

  /** Apply one SAN move; returns false if it is not legal here. */
  move(san: string): boolean {
    if (!isChess960(this.variant)) {
      try {
        this.chess.move(san);
        this.moves.push(san);
        return true;
      } catch {
        return false;
      }
    }
    if (san === "O-O" || san === "O-O-O") {
      const side = san === "O-O" ? "kingside" : "queenside";
      if (!this.applyChess960Castle(side)) return false;
      this.moves.push(san);
      return true;
    }
    let moved;
    try {
      moved = this.chess.move(san);
    } catch {
      return false;
    }
    this.updateRights(moved);
    this.reloadPreservingRights();
    this.moves.push(san);
    return true;
  }

  /** Replay every move from a PGN. */
  loadPgn(pgn: string): boolean {
    for (const san of pgnSanMoves(pgn)) {
      if (!this.move(san)) return false;
    }
    return true;
  }


  private updateRights(move: {
    from: Square;
    to: Square;
    piece: PieceSymbol;
    color: Color;
    captured?: PieceSymbol;
  }): void {
    const own = move.color === "w" ? this.rights.white : this.rights.black;
    if (move.piece === "k") {
      own.kingside = null;
      own.queenside = null;
    }
    if (own.kingside === move.from) own.kingside = null;
    if (own.queenside === move.from) own.queenside = null;
    const other = move.color === "w" ? this.rights.black : this.rights.white;
    if (move.captured === "r") {
      if (other.kingside === move.to) other.kingside = null;
      if (other.queenside === move.to) other.queenside = null;
    }
  }

  private reloadPreservingRights(): void {
    const fields = this.chess.fen().split(/\s+/);
    this.chess.load(
      fenWith(
        this.chess,
        this.chess.turn(),
        Number(fields[4] ?? 0),
        Number(fields[5] ?? 1),
      ),
    );
  }

  private canCastle(side: "kingside" | "queenside"): boolean {
    const colour = this.turn;
    const rank = backRank(colour);
    const king = kingSquare(this.chess, colour);
    if (king === null || Number(king[1]) !== rank) return false;
    const rights = colour === "w" ? this.rights.white : this.rights.black;
    const rook = side === "kingside" ? rights.kingside : rights.queenside;
    if (rook === null) return false;
    const rookPiece = this.chess.get(rook);
    if (rookPiece?.type !== "r" || rookPiece.color !== colour) return false;

    const kingFile = FILES.indexOf(king[0] ?? "");
    const rookFile = FILES.indexOf(rook[0] ?? "");
    const kingTarget = side === "kingside" ? 6 : 2;
    const rookTarget = side === "kingside" ? 5 : 3;
    const lowKing = Math.min(kingFile, kingTarget);
    const highKing = Math.max(kingFile, kingTarget);
    const lowRook = Math.min(rookFile, rookTarget);
    const highRook = Math.max(rookFile, rookTarget);

    // Every square the king or rook crosses must be empty, apart from the two
    // pieces doing the castling. A destination may be the other piece's origin.
    const occupied = new Set([king, rook]);
    for (let file = lowKing; file <= highKing; file += 1) {
      const target = square(file, rank);
      if (!occupied.has(target) && this.chess.get(target) !== undefined) return false;
    }
    for (let file = lowRook; file <= highRook; file += 1) {
      const target = square(file, rank);
      if (!occupied.has(target) && this.chess.get(target) !== undefined) return false;
    }

    const enemy: Color = colour === "w" ? "b" : "w";
    const step = kingTarget >= kingFile ? 1 : -1;
    for (let file = kingFile; ; file += step) {
      if (this.chess.isAttacked(square(file, rank), enemy)) return false;
      if (file === kingTarget) break;
    }
    return true;
  }

  private applyChess960Castle(side: "kingside" | "queenside"): boolean {
    if (!this.canCastle(side)) return false;
    const colour = this.turn;
    const rank = backRank(colour);
    const king = kingSquare(this.chess, colour);
    const rights = colour === "w" ? this.rights.white : this.rights.black;
    const rook = side === "kingside" ? rights.kingside : rights.queenside;
    if (king === null || rook === null) return false;

    const kingTo = square(side === "kingside" ? 6 : 2, rank);
    const rookTo = square(side === "kingside" ? 5 : 3, rank);
    const cells = new Map<Square, { type: PieceSymbol; color: Color }>();
    for (const row of this.chess.board()) {
      for (const cell of row) {
        if (cell !== null) cells.set(cell.square, { type: cell.type, color: cell.color });
      }
    }
    cells.delete(king);
    cells.delete(rook);
    cells.set(kingTo, { type: "k", color: colour });
    cells.set(rookTo, { type: "r", color: colour });

    const rows: string[] = [];
    for (let row = 8; row >= 1; row -= 1) {
      let text = "";
      let empty = 0;
      for (let file = 0; file < 8; file += 1) {
        const cell = cells.get(square(file, row));
        if (cell === undefined) {
          empty += 1;
          continue;
        }
        if (empty > 0) {
          text += String(empty);
          empty = 0;
        }
        text += cell.color === "w" ? cell.type.toUpperCase() : cell.type;
      }
      if (empty > 0) text += String(empty);
      rows.push(text);
    }

    rights.kingside = null;
    rights.queenside = null;
    const fields = this.chess.fen().split(/\s+/);
    const nextTurn: Color = colour === "w" ? "b" : "w";
    const moveNumber = Number(fields[5] ?? 1) + (colour === "b" ? 1 : 0);
    this.chess.load(`${rows.join("/")} ${nextTurn} - - 0 ${moveNumber}`);
    return true;
  }
}

