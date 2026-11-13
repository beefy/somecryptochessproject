/**
 * Miner Elo: the derived Glicko-2 leaderboard and its distribution.
 *
 * The node computes these numbers by replaying settled proofs; the explorer only
 * shapes them. The top ten is a slice of `miners`, while the histogram covers
 * every rated miner, so the page shows both the leaders and the field.
 */

import type { ReactNode } from "react";
import { StyleSheet, useWindowDimensions, View } from "react-native";
import { Divider, Text, useTheme } from "react-native-paper";

import { fetchRatings } from "../api";
import { StatCard } from "../components/BlockTable";
import { EmptyState, FailureNotice, Link, Loading, Section } from "../components/Chrome";
import { formatInteger, formatRating, shortAddress } from "../lib/format";
import type { MinerRating } from "../types";
import { useResource } from "../useResource";

export function Ratings(): ReactNode {
  const ratings = useResource((signal) => fetchRatings(signal), [], { pollMs: 15000 });
  const { width } = useWindowDimensions();
  const paper = useTheme();
  const wide = width >= 720;
  const board = ratings.data;
  const top = board?.miners.slice(0, 10) ?? [];
  const buckets = board?.distribution.buckets ?? [];
  const peak = Math.max(1, ...buckets.map((bucket) => bucket.count));

  return (
    <>
      <View style={styles.hero}>
        <Text variant="headlineMedium" style={styles.heroTitle}>
          Miner Elo
        </Text>
        <Text variant="bodyLarge" style={styles.heroBody}>
          Every settled proof is a game with two seats and a result, so the node can replay the
          whole chain as a Glicko-2 tournament. A miner cannot submit a rating: the only input is a
          proof the chain already verified.
        </Text>
      </View>

      {ratings.error !== undefined && <FailureNotice error={ratings.error} />}

      <View style={styles.stats}>
        <StatCard
          label="Rated miners"
          value={board === undefined ? "-" : formatInteger(board.distribution.rated)}
          hint={
            board === undefined ? undefined : `${formatInteger(board.rated_epochs)} rated epochs`
          }
        />
        <StatCard
          label="Settled games"
          value={board === undefined ? "-" : formatInteger(board.games)}
          hint="proofs on the adopted branch"
        />
        <StatCard
          label="Mean rating"
          value={board === undefined ? "-" : formatRating(board.distribution.mean)}
          hint={
            board === undefined ? undefined : `median ${formatRating(board.distribution.median)}`
          }
        />
        {wide && (
          <StatCard
            label="Algorithm"
            value={board === undefined ? "Glicko-2" : board.algorithm}
            hint="one rating period per epoch"
          />
        )}
      </View>

      <Divider />

      <Section title="Top 10" subtitle="strongest first; click a miner for its rating history">
        {ratings.error !== undefined ? (
          <FailureNotice error={ratings.error} />
        ) : board === undefined ? (
          <Loading />
        ) : top.length === 0 ? (
          <EmptyState>No miner has settled a game yet</EmptyState>
        ) : (
          <View>
            <View style={styles.header}>
              <Text variant="labelSmall" style={[styles.colRank, styles.muted]}>
                RANK
              </Text>
              <Text variant="labelSmall" style={[styles.colMiner, styles.muted]}>
                MINER
              </Text>
              <Text variant="labelSmall" style={[styles.colRating, styles.muted]}>
                RATING
              </Text>
              {wide && (
                <Text variant="labelSmall" style={[styles.colDeviation, styles.muted]}>
                  RD
                </Text>
              )}
              <Text variant="labelSmall" style={[styles.colRecord, styles.muted]}>
                W / D / L
              </Text>
              {wide && (
                <Text variant="labelSmall" style={[styles.colGames, styles.muted]}>
                  GAMES
                </Text>
              )}
            </View>
            {top.map((miner) => (
              <RatingRow key={miner.address} miner={miner} wide={wide} />
            ))}
            <Text variant="bodySmall" style={[styles.muted, styles.note]}>
              RD is the rating deviation. A miner is provisional until ten settled games, and a
              quiet miner&apos;s deviation widens while its rating stays put.
            </Text>
          </View>
        )}
      </Section>

      <Section
        title="Distribution"
        subtitle={
          board === undefined
            ? "fixed 100-point bands, so two polls draw the same axis"
            : `${formatInteger(board.distribution.rated)} rated miners in fixed 100-point bands`
        }
      >
        {ratings.error !== undefined ? (
          <FailureNotice error={ratings.error} />
        ) : board === undefined ? (
          <Loading />
        ) : (
          <View style={styles.histogram}>
            {buckets.map((bucket) => (
              <View key={bucket.label} style={styles.bucketRow}>
                <Text variant="bodySmall" style={[styles.bucketLabel, styles.numeric]}>
                  {bucket.label}
                </Text>
                <View style={styles.bucketTrack}>
                  <View
                    style={[
                      styles.bucketFill,
                      { flex: bucket.count, backgroundColor: paper.colors.primary },
                    ]}
                  />
                  <View style={{ flex: peak - bucket.count }} />
                </View>
                <Text variant="bodySmall" style={[styles.bucketCount, styles.numeric]}>
                  {formatInteger(bucket.count)}
                </Text>
              </View>
            ))}
          </View>
        )}
      </Section>
    </>
  );
}

function RatingRow({ miner, wide }: { miner: MinerRating; wide: boolean }): ReactNode {
  return (
    <View style={styles.row}>
      <Text variant="bodyMedium" style={[styles.colRank, styles.numeric]}>
        {miner.rank}
      </Text>
      <View style={styles.colMiner}>
        <Link href={`#/miners/${miner.address}`} mono>
          {shortAddress(miner.address)}
        </Link>
      </View>
      <Text variant="bodyMedium" style={[styles.colRating, styles.numeric]}>
        {formatRating(miner.rating)}
      </Text>
      {wide && (
        <Text variant="bodySmall" style={[styles.colDeviation, styles.numeric, styles.muted]}>
          {formatRating(miner.deviation)}
        </Text>
      )}
      <Text variant="bodySmall" style={[styles.colRecord, styles.numeric]}>
        {miner.wins} / {miner.draws} / {miner.losses}
      </Text>
      {wide && (
        <Text variant="bodySmall" style={[styles.colGames, styles.numeric, styles.muted]}>
          {formatInteger(miner.games)}
        </Text>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  hero: { gap: 8, paddingTop: 8, maxWidth: 760 },
  heroTitle: { fontWeight: "600" },
  heroBody: { opacity: 0.8 },
  stats: { flexDirection: "row", flexWrap: "wrap", gap: 12 },
  header: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingVertical: 6,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: "rgba(128,128,128,0.4)",
  },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingVertical: 10,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: "rgba(128,128,128,0.24)",
  },
  colRank: { width: 44 },
  colMiner: { flexGrow: 1, flexShrink: 1, minWidth: 140 },
  colRating: { width: 82, textAlign: "right" },
  colDeviation: { width: 64, textAlign: "right" },
  colRecord: { width: 84 },
  colGames: { width: 60, textAlign: "right" },
  note: { paddingTop: 8 },
  histogram: { gap: 6 },
  bucketRow: { flexDirection: "row", alignItems: "center", gap: 10 },
  bucketLabel: { width: 84 },
  bucketTrack: {
    flexGrow: 1,
    flexShrink: 1,
    flexDirection: "row",
    height: 12,
    borderRadius: 6,
    overflow: "hidden",
    backgroundColor: "rgba(128,128,128,0.16)",
  },
  bucketFill: { height: "100%", borderRadius: 6 },
  bucketCount: { width: 48, textAlign: "right" },
  muted: { opacity: 0.66 },
  numeric: { fontVariant: ["tabular-nums"] },
});
