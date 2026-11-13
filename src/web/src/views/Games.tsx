/**
 * The games list: every proof this node knows about, pending ones first.
 *
 * `/v1/games` already orders pending proofs ahead of settled ones, so this view
 * is a straight render plus a couple of counters -- a filter that disagrees with
 * the node's ordering would just be a second, worse opinion.
 */

import type { ReactNode } from "react";
import { StyleSheet, View } from "react-native";
import { Text } from "react-native-paper";

import { fetchGames } from "../api";
import { FailureNotice, Loading, Section } from "../components/Chrome";
import { GameTable } from "../components/GameTable";
import { formatInteger } from "../lib/format";
import { useResource } from "../useResource";

export function Games(): ReactNode {
  const games = useResource((signal) => fetchGames(signal), [], { pollMs: 8000 });
  const all = games.data?.games ?? [];
  const pending = all.filter((game) => game.in_mempool).length;
  const challenged = all.filter((game) => game.challenge_status === "open").length;

  return (
    <Section
      title="Games"
      subtitle={
        games.data === undefined
          ? "settled proofs and the ones waiting for a block"
          : `${formatInteger(all.length)} games · ${pending} pending · ${challenged} challenged`
      }
    >
      {games.error !== undefined ? (
        <FailureNotice error={games.error} />
      ) : games.data === undefined ? (
        <Loading />
      ) : (
        <GameTable games={games.data.games} />
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
  footer: { paddingTop: 8 },
  muted: { opacity: 0.66 },
});
