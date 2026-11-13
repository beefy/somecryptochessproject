/**
 * The testnet table: a board where both seats are played from this tab.
 *
 * Nothing here is simulated. The two seats are real Ed25519 keypairs made in the
 * browser, their bech32 addresses are derived under the node's network id, and
 * the colour draw is the protocol's own `whiteSeat` over a room beacon. Each move
 * is signed with `movePayload`, and a finished game produces the canonical game
 * hash and both result signatures -- the same documents a win proof carries.
 *
 * The one live request is the faucet: a game played here has no book opening and
 * no opponent, so it cannot be a proof. The page says so and only the funding
 * call reaches the node.
 */

import { useMemo, useState, type ReactNode } from "react";
import { Chess, type Move, type PieceSymbol, type Square } from "chess.js";
import { Pressable, StyleSheet, useWindowDimensions, View } from "react-native";
import { Button, Divider, HelperText, Text, TextInput } from "react-native-paper";

import { requestFaucet } from "../api";
import { CodeBlock, FailureNotice, KeyValue, Loading, Mono, Section, StatusPill } from "../components/Chrome";
import { DOMAINS, toHex } from "../lib/canonical";
import { formatInteger, shortAddress } from "../lib/format";
import {
  addressFor,
  generateKeypair,
  keypairFromSeedHex,
  publicKeyHex,
  seedHex,
  signDomain,
  verifyDomain,
  type Keypair,
} from "../lib/keys";
import {
  STARTING_FEN,
  beaconForRoom,
  canonicalGameHash,
  matchId,
  movePayload,
  resultFor,
  resultPayload,
  whiteSeat,
} from "../lib/protocol";
import { useNode } from "../node";
import type { FaucetView } from "../types";

/** One played move, with the signature the seat published for it. */
interface PlayedMove {
  ply: number;
  san: string;
  seat: string;
  publicKey: string;
  signature: string;
  /** The position after the move, so the board can be rebuilt from any prefix. */
  fen: string;
}

const GLYPHS: Record<PieceSymbol, string> = {
  k: "♚",
  q: "♛",
  r: "♜",
  b: "♝",
  n: "♞",
  p: "♟",
};

function freshSeeds(): [string, string] {
  return [seedHex(generateKeypair()), seedHex(generateKeypair())];
}

function wholeNumber(text: string, fallback: number): number {
  const value = Number(text.trim());
  return Number.isSafeInteger(value) && value >= 0 ? value : fallback;
}

export function Play(): ReactNode {
  const { status } = useNode();
  const network = status.data?.network ?? null;
  const [room, setRoom] = useState("club");
  const [epochText, setEpochText] = useState("0");
  const [roundText, setRoundText] = useState("0");
  const [seeds, setSeeds] = useState<[string, string]>(freshSeeds);
  const [moves, setMoves] = useState<PlayedMove[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [grant, setGrant] = useState<FaucetView | null>(null);
  const [grantError, setGrantError] = useState<string | null>(null);
  const [granting, setGranting] = useState(false);

  const epoch = wholeNumber(epochText, 0);
  const round = wholeNumber(roundText, 0);
  const { width } = useWindowDimensions();
  const boardSize = width < 720 ? Math.min(width - 56, 416) : 448;

  const seats = useMemo<readonly [Keypair, Keypair] | null>(() => {
    if (network === null) return null;
    try {
      return [keypairFromSeedHex(seeds[0]), keypairFromSeedHex(seeds[1])];
    } catch {
      return null;
    }
  }, [network, seeds]);

  const match = useMemo(() => {
    if (network === null || seats === null) return null;
    const addresses = [addressFor(seats[0].publicKey, network), addressFor(seats[1].publicKey, network)] as const;
    const beacon = beaconForRoom(room);
    const id = matchId(network, epoch, round, addresses);
    const white = whiteSeat(beacon, id, addresses);
    return {
      addresses,
      beaconHex: toHex(beacon),
      id,
      white,
      black: white === addresses[0] ? addresses[1] : addresses[0],
    };
  }, [network, seats, room, epoch, round]);

  const fen = moves.length === 0 ? STARTING_FEN : (moves[moves.length - 1] as PlayedMove).fen;
  const outcome = useMemo(() => {
    const finished = new Chess(fen);
    if (!finished.isGameOver()) return null;
    const checkmate = finished.isCheckmate();
    return resultFor({
      winner: checkmate ? (finished.turn() === "w" ? "black" : "white") : undefined,
      draw: !checkmate,
    });
  }, [fen]);

  const closing = useMemo(() => {
    if (outcome === null || network === null || match === null || seats === null) return null;
    const keyFor = (address: string): Keypair => (address === match.addresses[0] ? seats[0] : seats[1]);
    const gameHash = canonicalGameHash(
      network,
      moves.map((move) => move.san),
      outcome.result,
    );
    const payload = resultPayload({
      network,
      match_id: match.id,
      game_hash: gameHash,
      result: outcome.result,
      termination: outcome.termination,
      plies: moves.length,
    });
    const signatures: [string, string][] = [
      [match.white, signDomain(keyFor(match.white), DOMAINS.RESULT, payload)],
      [match.black, signDomain(keyFor(match.black), DOMAINS.RESULT, payload)],
    ];
    return { gameHash, payload, signatures };
  }, [outcome, moves, network, match, seats]);

  if (network === null || seats === null || match === null) {
    if (status.error !== undefined) return <FailureNotice error={status.error} />;
    return <Loading />;
  }

  const board = new Chess(fen);
  const turnAddress = board.turn() === "w" ? match.white : match.black;
  const legal =
    selected === null
      ? []
      : board.moves({ square: selected as Square, verbose: true }).map((move) => move.to);
  const live = moves.length === 0;
  const keyFor = (address: string): Keypair => (address === match.addresses[0] ? seats[0] : seats[1]);

  const commit = (from: string, to: string): void => {
    const next = new Chess(fen);
    let move: Move;
    try {
      move = next.move({ from, to, promotion: "q" });
    } catch {
      return;
    }
    const keypair = keyFor(turnAddress);
    const ply = moves.length;
    const payload = movePayload({ network, match_id: match.id, seat: turnAddress, ply, san: move.san });
    setMoves([
      ...moves,
      {
        ply,
        san: move.san,
        seat: turnAddress,
        publicKey: publicKeyHex(keypair),
        signature: signDomain(keypair, DOMAINS.MOVE, payload),
        fen: next.fen(),
      },
    ]);
    setSelected(null);
  };

  const press = (square: Square): void => {
    if (selected !== null && legal.includes(square)) {
      commit(selected, square);
      return;
    }
    const piece = board.get(square);
    setSelected(piece !== undefined && piece.color === board.turn() ? square : null);
  };

  const reset = (): void => {
    setMoves([]);
    setSelected(null);
    setSeeds(freshSeeds());
    setGrant(null);
    setGrantError(null);
  };

  const askFaucet = async (): Promise<void> => {
    setGranting(true);
    setGrantError(null);
    try {
      setGrant(await requestFaucet(match.white));
    } catch (error) {
      setGrantError(error instanceof Error ? error.message : String(error));
    } finally {
      setGranting(false);
    }
  };

  return (
    <>
      <View style={styles.hero}>
        <Text variant="headlineMedium" style={styles.heroTitle}>
          Play a table
        </Text>
        <Text variant="bodyLarge" style={styles.heroBody}>
          Two browser-generated seats, one board. Every move is signed here with the protocol&apos;s own move
          payload, so each signature below verifies against the public key of the seat that played it.
        </Text>
      </View>

      <Section
        title="The match"
        subtitle="a room code picks the beacon; the beacon and match id pick the colours"
        action={
          <Button mode="contained-tonal" compact onPress={reset}>
            New game
          </Button>
        }
      >
        <View style={styles.fields}>
          <TextInput
            mode="outlined"
            dense
            label="Room"
            value={room}
            onChangeText={setRoom}
            disabled={!live}
            autoCapitalize="none"
            style={styles.field}
          />
          <TextInput
            mode="outlined"
            dense
            label="Epoch"
            value={epochText}
            onChangeText={setEpochText}
            disabled={!live}
            keyboardType="number-pad"
            style={styles.fieldNarrow}
          />
          <TextInput
            mode="outlined"
            dense
            label="Round"
            value={roundText}
            onChangeText={setRoundText}
            disabled={!live}
            keyboardType="number-pad"
            style={styles.fieldNarrow}
          />
        </View>
        {!live && (
          <HelperText type="info" visible>
            Room, epoch and round are fixed once the first move is signed. Start a new game to change them.
          </HelperText>
        )}
        <KeyValue label="Beacon">
          <Mono copy>{match.beaconHex}</Mono>
        </KeyValue>
        <KeyValue label="Match id">
          <Mono copy>{match.id}</Mono>
        </KeyValue>
        <KeyValue label="White seat">
          <Mono copy>{match.white}</Mono>
        </KeyValue>
        <KeyValue label="Black seat">
          <Mono copy>{match.black}</Mono>
        </KeyValue>
        <KeyValue label="Seeds">
          <CodeBlock>{`white ${seeds[0]}\nblack ${seeds[1]}`}</CodeBlock>
        </KeyValue>
        <HelperText type="info" visible>
          Those seeds are the private keys of a throwaway table, printed so the page can show it signs with
          nothing up its sleeve. Never reuse them for anything of value.
        </HelperText>
      </Section>

      <Divider />

      <Section
        title="Board"
        subtitle={
          outcome === null
            ? `${board.turn() === "w" ? "White" : "Black"} to move${board.isCheck() ? ", in check" : ""}`
            : `game over: ${outcome.result} by ${outcome.termination}`
        }
        action={
          moves.length === 0 ? undefined : (
            <Button
              mode="outlined"
              compact
              onPress={() => {
                setMoves(moves.slice(0, -1));
                setSelected(null);
              }}
            >
              Take back
            </Button>
          )
        }
      >
        <View style={styles.table}>
          <View style={[styles.board, { width: boardSize, height: boardSize }]}>
            {board.board().map((row, rowIndex) => (
              <View key={rowIndex} style={styles.boardRow}>
                {row.map((cell, columnIndex) => {
                  const square = cell?.square ?? squareName(columnIndex, rowIndex);
                  const light = (rowIndex + columnIndex) % 2 === 0;
                  const isSelected = selected === square;
                  const isTarget = legal.includes(square);
                  return (
                    <Pressable
                      key={square}
                      onPress={() => press(square)}
                      style={[
                        styles.square,
                        light ? styles.squareLight : styles.squareDark,
                        isSelected && styles.squareSelected,
                        isTarget && styles.squareTarget,
                      ]}
                      accessibilityLabel={`${square}${cell === null ? "" : ` ${cell.color} ${cell.type}`}`}
                    >
                      {cell !== null && (
                        <Text
                          style={[
                            styles.piece,
                            cell.color === "w" ? styles.pieceWhite : styles.pieceBlack,
                            { fontSize: boardSize / 11 },
                          ]}
                        >
                          {GLYPHS[cell.type]}
                        </Text>
                      )}
                    </Pressable>
                  );
                })}
              </View>
            ))}
          </View>

          <View style={styles.side}>
            <View style={styles.turnRow}>
              <StatusPill
                label={board.turn() === "w" ? "white to move" : "black to move"}
                tone={board.turn() === "w" ? "neutral" : "info"}
              />
              <Text variant="bodySmall" style={styles.muted}>
                {shortAddress(turnAddress)}
              </Text>
            </View>

            {moves.length === 0 ? (
              <Text variant="bodyMedium" style={styles.muted}>
                Tap a piece, then a highlighted square. Both colours are yours: this is a table, not an
                opponent, and a pawn always promotes to a queen.
              </Text>
            ) : (
              <View style={styles.log}>
                {moves
                  .slice()
                  .reverse()
                  .map((move) => {
                    const payload = movePayload({
                      network,
                      match_id: match.id,
                      seat: move.seat,
                      ply: move.ply,
                      san: move.san,
                    });
                    const valid = verifyDomain(move.publicKey, DOMAINS.MOVE, payload, move.signature);
                    return (
                      <View key={move.ply} style={styles.logRow}>
                        <Text variant="bodySmall" style={styles.logPly}>
                          {formatInteger(move.ply + 1)}.
                        </Text>
                        <Text variant="bodyMedium" style={styles.logSan}>
                          {move.san}
                        </Text>
                        <Text variant="bodySmall" style={styles.muted}>
                          {move.seat === match.white ? "white" : "black"}
                        </Text>
                        <StatusPill label={valid ? "verified" : "bad signature"} tone={valid ? "good" : "bad"} />
                      </View>
                    );
                  })}
              </View>
            )}
            {moves.length > 0 && (
              <KeyValue label="Last signature">
                <Mono copy>{(moves[moves.length - 1] as PlayedMove).signature}</Mono>
              </KeyValue>
            )}
          </View>
        </View>
      </Section>

      {outcome !== null && closing !== null && (
        <>
          <Divider />
          <Section title="Result" subtitle="both seats sign the game hash, never the move list">
            <KeyValue label="Outcome" mono>
              {`${outcome.result} by ${outcome.termination} in ${moves.length} plies`}
            </KeyValue>
            <KeyValue label="Game hash">
              <Mono copy>{closing.gameHash}</Mono>
            </KeyValue>
            {closing.signatures.map(([address, signature]) => {
              const valid = verifyDomain(
                publicKeyHex(keyFor(address)),
                DOMAINS.RESULT,
                closing.payload,
                signature,
              );
              return (
                <View key={address} style={styles.sigRow}>
                  <Text variant="bodySmall" style={styles.muted}>
                    {address === match.white ? "white" : "black"} {shortAddress(address)}
                  </Text>
                  <Mono copy>{signature}</Mono>
                  <StatusPill label={valid ? "verified" : "bad signature"} tone={valid ? "good" : "bad"} />
                </View>
              );
            })}
          </Section>
        </>
      )}

      <Divider />

      <Section title="Testnet faucet" subtitle="fund the white seat from this node's faucet">
        <Text variant="bodyMedium" style={styles.muted}>
          A game played here is not a proof: it has no book opening and no opponent, so it never leaves the
          browser. The faucet call is the one thing this page asks of the node.
        </Text>
        <View style={styles.actions}>
          <Button mode="contained" onPress={() => void askFaucet()} loading={granting} disabled={granting}>
            Fund the white seat
          </Button>
          <Button mode="text" onPress={() => setGrant(null)} disabled={grant === null}>
            Clear
          </Button>
        </View>
        {grantError !== null && <FailureNotice error={new Error(grantError)} />}
        {grant !== null && (
          <>
            <KeyValue label="Grant" mono>
              {`${formatInteger(grant.amount)} coins`}
            </KeyValue>
            <KeyValue label="To">
              <Mono copy>{grant.address}</Mono>
            </KeyValue>
            <KeyValue label="Txid">
              <Mono copy>{grant.txid}</Mono>
            </KeyValue>
            <Text variant="bodySmall" style={styles.muted}>
              The transfer is in the node&apos;s mempool; it appears on the blocks page once a miner seals it.
            </Text>
          </>
        )}
      </Section>
    </>
  );
}

/** `a1`-style name for a square at (column, row) in `board()` order. */
function squareName(column: number, row: number): Square {
  const files = "abcdefgh";
  return `${files[column]}${8 - row}` as Square;
}

const styles = StyleSheet.create({
  hero: { gap: 8, paddingTop: 8, maxWidth: 760 },
  heroTitle: { fontWeight: "600" },
  heroBody: { opacity: 0.8 },
  actions: { flexDirection: "row", gap: 8, flexWrap: "wrap" },
  fields: { flexDirection: "row", gap: 8, flexWrap: "wrap", paddingTop: 4 },
  field: { flexGrow: 1, minWidth: 160 },
  fieldNarrow: { width: 110 },
  table: { flexDirection: "row", gap: 20, flexWrap: "wrap", alignItems: "flex-start", paddingTop: 8 },
  board: {
    borderRadius: 8,
    overflow: "hidden",
    borderWidth: 1,
    borderColor: "rgba(0,0,0,0.24)",
  },
  boardRow: { flex: 1, flexDirection: "row" },
  square: { flex: 1, alignItems: "center", justifyContent: "center" },
  squareLight: { backgroundColor: "#e9e2d0" },
  squareDark: { backgroundColor: "#7d6a54" },
  squareSelected: { backgroundColor: "#c9a227" },
  squareTarget: { backgroundColor: "#94b58a" },
  piece: { textAlign: "center" },
  pieceWhite: { color: "#ffffff", textShadowColor: "#161616", textShadowRadius: 1 },
  pieceBlack: { color: "#161616", textShadowColor: "#ffffff", textShadowRadius: 1 },
  side: { flexGrow: 1, flexShrink: 1, minWidth: 240, gap: 10 },
  turnRow: { flexDirection: "row", alignItems: "center", gap: 8, flexWrap: "wrap" },
  log: { gap: 2 },
  logRow: { flexDirection: "row", alignItems: "center", gap: 8, flexWrap: "wrap" },
  logPly: { width: 42, fontVariant: ["tabular-nums"], opacity: 0.66 },
  logSan: { width: 64, fontWeight: "600" },
  sigRow: { gap: 4, paddingVertical: 6, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: "rgba(128,128,128,0.32)" },
  muted: { opacity: 0.66 },
});
