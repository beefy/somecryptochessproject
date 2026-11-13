/**
 * Overview: the chain at a glance.
 *
 * Everything here comes from documents the node already serves, with no derived
 * numbers: the epoch shown is the tip block's own `epoch`, not
 * `height / epoch_length`, so a parameter change cannot make the explorer
 * disagree with the chain it is reading.
 */

import type { ReactNode } from "react";
import { StyleSheet, useWindowDimensions, View } from "react-native";
import { Divider, Text } from "react-native-paper";

import { fetchBlocks, fetchChallenges, fetchGames } from "../api";
import { BlockTable, StatCard } from "../components/BlockTable";
import { EmptyState, FailureNotice, Loading, Section, StatusPill } from "../components/Chrome";
import { GameTable } from "../components/GameTable";
import { formatAge, formatAmount, formatInteger } from "../lib/format";
import { useNode } from "../node";
import { useNow, useResource } from "../useResource";

export function Dashboard(): ReactNode {
  const { status, params } = useNode();
  const blocks = useResource((signal) => fetchBlocks({ limit: 6, signal }), [], { pollMs: 5000 });
  const games = useResource((signal) => fetchGames(signal), [], { pollMs: 8000 });
  const challenges = useResource((signal) => fetchChallenges(signal), [], { pollMs: 15000 });
  const now = useNow(1000);
  const { width } = useWindowDimensions();
  const wide = width >= 720;

  const tip = blocks.data?.blocks[0];
  const openChallenges = (challenges.data?.challenges ?? []).filter((item) => item.status === "open");

  return (
    <>
      <View style={styles.hero}>
        <Text variant="headlineMedium" style={styles.heroTitle}>
          A chess chain
        </Text>
        <Text variant="bodyLarge" style={styles.heroBody}>
          In SCCP the work that seals a block is a chess game. Two seated miners play an opening from the
          epoch&apos;s book, both sign every move, and the winner signs the result. The proof they produce is
          what a block commits to.
        </Text>
      </View>

      {status.error !== undefined && <FailureNotice error={status.error} />}

      <View style={styles.stats}>
        <StatCard
          label="Height"
          value={status.data === undefined ? "-" : formatInteger(status.data.height)}
          hint={tip === undefined ? undefined : `sealed ${formatAge(tip.timestamp, now)}`}
        />
        <StatCard
          label="Epoch"
          value={tip === undefined ? "-" : formatInteger(tip.epoch)}
          hint={params.data === undefined ? undefined : `${params.data.epoch_length} blocks long`}
        />
        <StatCard
          label="Peers"
          value={status.data === undefined ? "-" : formatInteger(status.data.peers)}
          hint={status.data?.syncing === true ? "syncing" : "in sync"}
        />
        <StatCard
          label="Mempool"
          value={
            status.data === undefined
              ? "-"
              : `${status.data.mempool.transactions} tx / ${status.data.mempool.proofs} proofs`
          }
          hint="waiting for the next block"
        />
        {wide && (
          <StatCard
            label="Difficulty"
            value={tip === undefined ? "-" : `${tip.wins_required} wins / ${tip.seal_bits} bits`}
            hint="at the tip"
          />
        )}
        {wide && (
          <StatCard
            label="Block reward"
            value={params.data === undefined ? "-" : formatAmount(params.data.subsidy)}
            hint={params.data === undefined ? undefined : `${params.data.coinbase_maturity}-block maturity`}
          />
        )}
      </View>

      <Divider />

      <Section title="Latest blocks" subtitle="newest first, one row per sealed block">
        {blocks.error !== undefined ? (
          <FailureNotice error={blocks.error} />
        ) : blocks.data === undefined ? (
          <Loading />
        ) : (
          <BlockTable blocks={blocks.data.blocks} />
        )}
      </Section>

      <Section title="Recent games" subtitle="settled proofs and the ones still waiting in the mempool">
        {games.error !== undefined ? (
          <FailureNotice error={games.error} />
        ) : games.data === undefined ? (
          <Loading />
        ) : (
          <GameTable games={games.data.games.slice(0, 6)} />
        )}
      </Section>

      <Section title="Open challenges" subtitle="fraud claims inside their resolution window">
        {challenges.error !== undefined ? (
          <FailureNotice error={challenges.error} />
        ) : challenges.data === undefined ? (
          <Loading />
        ) : openChallenges.length === 0 ? (
          <EmptyState>No challenge is open</EmptyState>
        ) : (
          openChallenges.map((item) => (
            <View key={item.challenge_id} style={styles.challengeRow}>
              <StatusPill label={item.status} tone="bad" />
              <Text variant="bodyMedium" style={styles.challengeReason}>
                {item.reason}
              </Text>
              <Text variant="bodySmall" style={styles.muted}>
                #{formatInteger(item.opened_height)} → #{formatInteger(item.deadline_height)}
              </Text>
            </View>
          ))
        )}
      </Section>
    </>
  );
}

const styles = StyleSheet.create({
  hero: { gap: 8, paddingTop: 8, maxWidth: 760 },
  heroTitle: { fontWeight: "600" },
  heroBody: { opacity: 0.8 },
  stats: { flexDirection: "row", flexWrap: "wrap", gap: 12 },
  challengeRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    flexWrap: "wrap",
    paddingVertical: 6,
  },
  challengeReason: { flexShrink: 1 },
  muted: { opacity: 0.66 },
});
