/**
 * The games list: every proof this node knows about, pending ones first.
 *
 * `/v1/games` already orders pending proofs ahead of settled ones, so this view
 * is a straight render plus a couple of counters -- a filter that disagrees with
 * the node's ordering would just be a second, worse opinion.
 *
 * Paging is a stack of cursors, like the block list. A game cursor carries the
 * settle height *and* the game hash: a block can settle more than one proof, and
 * a height-only cursor would skip the rest of that block's games.
 */

import { useState, type ReactNode } from "react";
import { StyleSheet, View } from "react-native";
import { Button, Text } from "react-native-paper";

import { fetchGames } from "../api";
import { EmptyState, FailureNotice, Loading, Section } from "../components/Chrome";
import { GameTable } from "../components/GameTable";
import { formatInteger } from "../lib/format";
import { useResource } from "../useResource";

const PAGE_SIZE = 25;

interface GameCursor {
  height: number;
  gameHash: string;
}

export function Games(): ReactNode {
  const [cursors, setCursors] = useState<Array<GameCursor | undefined>>([undefined]);
  const before = cursors[cursors.length - 1];
  const games = useResource(
    (signal) =>
      fetchGames({
        limit: PAGE_SIZE,
        beforeHeight: before?.height,
        beforeGameHash: before?.gameHash,
        signal,
      }),
    [before?.height, before?.gameHash],
    before === undefined ? { pollMs: 8000 } : {},
  );
  const all = games.data?.games ?? [];
  const pending = all.filter((game) => game.in_mempool).length;
  const oldest = all[all.length - 1];

  return (
    <Section
      title="Games"
      subtitle={
        games.data === undefined
          ? "settled proofs and the ones waiting for a block"
          : `${formatInteger(all.length)} games · ${pending} pending`
      }
      action={
        <View style={styles.controls}>
          <Button
            mode="outlined"
            compact
            disabled={cursors.length === 1}
            onPress={() => setCursors((stack) => stack.slice(0, -1))}
          >
            Newer
          </Button>
          <Button
            mode="outlined"
            compact
            disabled={
              oldest === undefined || oldest.height === null || all.length < PAGE_SIZE
            }
            onPress={() => {
              if (oldest === undefined || oldest.height === null) return;
              const cursor: GameCursor = { height: oldest.height, gameHash: oldest.game_hash };
              setCursors((stack) => [...stack, cursor]);
            }}
          >
            Older
          </Button>
        </View>
      }
    >
      {games.error !== undefined ? (
        <FailureNotice error={games.error} />
      ) : games.data === undefined ? (
        <Loading />
      ) : all.length === 0 ? (
        <EmptyState>No game at this cursor</EmptyState>
      ) : (
        <GameTable games={all} />
      )}
      {games.loading && games.data !== undefined && (
        <View style={styles.footer}>
          <Text variant="bodySmall" style={styles.muted}>
            refreshing…
          </Text>
        </View>
      )}
    </Section>
  );
}

const styles = StyleSheet.create({
  controls: { flexDirection: "row", gap: 8 },
  footer: { paddingTop: 8 },
  muted: { opacity: 0.66 },
});
