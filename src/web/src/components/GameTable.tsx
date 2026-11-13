/**
 * The games list: one row per proof, whether it settled or is still waiting.
 *
 * The result and the two seats come first because that is what a chess reader
 * looks for; the settlement column says where the proof is (a block, or the
 * mempool) and how the chain currently judges it.
 */

import type { ReactNode } from "react";
import { useWindowDimensions, View } from "react-native";
import { Text } from "react-native-paper";

import { formatInteger, shortAddress } from "../lib/format";
import type { GameSummary } from "../types";
import { EmptyState, HashLink, Link, StatusPill } from "./Chrome";
import { tableStyles as styles } from "./tableStyles";

/** How a game's settlement state reads as one pill. */
export function gameStatus(game: GameSummary): {
  label: string;
  tone: "good" | "warn" | "bad" | "info" | "neutral";
} {
  if (game.in_mempool) return { label: "pending", tone: "warn" };
  if (game.challenge_status === "open") return { label: "challenged", tone: "bad" };
  if (game.challenge_status === "upheld") return { label: "challenge upheld", tone: "bad" };
  if (game.challenge_status === "rejected") return { label: "challenge rejected", tone: "info" };
  if (game.quality_verdict === "fail") return { label: "quality fail", tone: "bad" };
  return { label: "settled", tone: "good" };
}

/** One game in list form. */
export function GameRow({ game }: { game: GameSummary }): ReactNode {
  const { width } = useWindowDimensions();
  const wide = width >= 720;
  const status = gameStatus(game);
  return (
    <View style={styles.row}>
      <View style={styles.colResult}>
        <Text variant="bodyMedium" style={styles.numeric}>
          {game.result ?? "—"}
        </Text>
      </View>
      <View style={styles.colSeats}>
        <Link href={`#/accounts/${game.winner}`} mono>
          {shortAddress(game.winner)}
        </Link>
        <Text variant="bodySmall" style={styles.muted}>
          {game.loser === null ? "opponent unknown" : "beat "}
          {game.loser !== null && (
            <Link href={`#/accounts/${game.loser}`} mono>
              {shortAddress(game.loser)}
            </Link>
          )}
        </Text>
      </View>
      {wide && (
        <Text variant="bodySmall" style={[styles.colPlies, styles.muted]}>
          {game.plies === null ? "-" : `${game.plies} plies`}
        </Text>
      )}
      {wide && (
        <Text variant="bodySmall" style={[styles.colEpoch, styles.muted]}>
          {game.epoch === null ? "-" : `e${game.epoch} r${game.round ?? 0}`}
        </Text>
      )}
      <View style={styles.colWhere}>
        {game.height === null ? (
          <Text variant="bodySmall" style={styles.muted}>
            no block
          </Text>
        ) : (
          <Link href={`#/blocks/${game.height}`} mono>
            #{formatInteger(game.height)}
          </Link>
        )}
        <StatusPill label={status.label} tone={status.tone} />
      </View>
      <View style={styles.colGameHash}>
        <HashLink value={game.game_hash} href={`#/games/${game.game_hash}`} />
      </View>
    </View>
  );
}

/** A game list with a header. */
export function GameTable({ games }: { games: GameSummary[] }): ReactNode {
  const { width } = useWindowDimensions();
  const wide = width >= 720;
  if (games.length === 0) return <EmptyState>No games yet</EmptyState>;
  return (
    <View>
      <View style={styles.header}>
        <Text variant="labelSmall" style={[styles.colResult, styles.muted]}>
          RESULT
        </Text>
        <Text variant="labelSmall" style={[styles.colSeats, styles.muted]}>
          WINNER
        </Text>
        {wide && (
          <Text variant="labelSmall" style={[styles.colPlies, styles.muted]}>
            LENGTH
          </Text>
        )}
        {wide && (
          <Text variant="labelSmall" style={[styles.colEpoch, styles.muted]}>
            EPOCH
          </Text>
        )}
        <Text variant="labelSmall" style={[styles.colWhere, styles.muted]}>
          SETTLED / STATE
        </Text>
        <Text variant="labelSmall" style={[styles.colGameHash, styles.muted]}>
          GAME HASH
        </Text>
      </View>
      {games.map((game) => (
        <GameRow key={game.game_hash} game={game} />
      ))}
    </View>
  );
}
