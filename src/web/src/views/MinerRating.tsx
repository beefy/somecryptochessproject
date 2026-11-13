/**
 * One miner's Elo page: current rating, the whole rating history as a line
 * chart, and the games this node can still list for the seat.
 *
 * The page is a read-only projection of `/v1/ratings/{address}`. It never
 * accepts a number from the reader: the only way the chart moves is a new proof
 * settling in a block.
 */

import type { ReactNode } from "react";
import { StyleSheet, useWindowDimensions, View } from "react-native";
import { Text, useTheme } from "react-native-paper";

import { fetchGames, fetchMinerRating } from "../api";
import { StatCard } from "../components/BlockTable";
import {
  FailureNotice,
  KeyValue,
  Link,
  Loading,
  Mono,
  Section,
  StatusPill,
} from "../components/Chrome";
import { GameTable } from "../components/GameTable";
import { LineChart } from "../components/LineChart";
import { formatInteger, formatRating, shortAddress } from "../lib/format";
import type { RatingPoint } from "../types";
import { useResource } from "../useResource";

/** The SVG tooltip for one history point. */
function pointLabel(point: RatingPoint): string {
  const games = `${point.games} ${point.games === 1 ? "game" : "games"}`;
  return `epoch ${point.epoch} · ${formatRating(point.rating)} (RD ${formatRating(
    point.deviation,
  )}) · ${games}`;
}

export function MinerRating({ address }: { address: string }): ReactNode {
  const detail = useResource((signal) => fetchMinerRating(address, signal), [address], {
    pollMs: 15000,
  });
  const games = useResource((signal) => fetchGames(signal), [], { pollMs: 20000 });
  const paper = useTheme();
  const { width } = useWindowDimensions();
  const wide = width >= 720;

  if (detail.error !== undefined) return <FailureNotice error={detail.error} />;
  if (detail.data === undefined) return <Loading />;

  const view = detail.data;
  const points = view.history.map((point) => ({
    x: point.epoch,
    y: point.rating,
    label: pointLabel(point),
  }));
  const mine = (games.data?.games ?? []).filter(
    (game) => game.winner === address || game.loser === address,
  );
  const firstEpoch = view.history[0]?.epoch;

  return (
    <>
      <View style={styles.titleRow}>
        <View style={styles.titleBlock}>
          <Text variant="headlineSmall">Miner</Text>
          <Mono copy>{view.address}</Mono>
        </View>
        <View style={styles.titleActions}>
          {view.rank !== null && (
            <StatusPill label={`rank #${formatInteger(view.rank)}`} tone="info" />
          )}
          <StatusPill
            label={view.provisional ? "provisional" : "established"}
            tone={view.provisional ? "warn" : "good"}
          />
        </View>
      </View>

      <View style={styles.stats}>
        <StatCard
          label="Rating"
          value={formatRating(view.rating)}
          hint={`deviation ${formatRating(view.deviation)}`}
        />
        <StatCard
          label="Record"
          value={`${view.wins} / ${view.draws} / ${view.losses}`}
          hint={`${formatInteger(view.games)} settled games`}
        />
        {wide && (
          <StatCard label="Volatility" value={formatRating(view.volatility, 4)} hint="Glicko-2 sigma" />
        )}
        {wide && (
          <StatCard
            label="Periods"
            value={formatInteger(view.history.length)}
            hint={firstEpoch === undefined ? "none yet" : `since epoch ${firstEpoch}`}
          />
        )}
      </View>

      <Section
        title="Elo over time"
        subtitle={
          view.history.length === 0
            ? "no settled game in this node's chain"
            : "Glicko-2, one point per epoch the seat played in"
        }
      >
        <LineChart
          points={points}
          colour={paper.colors.primary}
          textColour={paper.colors.onSurface}
          label={`Glicko-2 rating history for ${shortAddress(view.address)}`}
          emptyLabel="This seat has no settled game yet, so it still sits at the 1500 prior."
        />
      </Section>

      <Section title="Details" subtitle="what the rating was computed from">
        <KeyValue label="Account">
          <Link href={`#/accounts/${view.address}`}>balances and registration</Link>
        </KeyValue>
        <KeyValue label="Volatility" mono>
          {formatRating(view.volatility, 5)}
        </KeyValue>
        <KeyValue label="Last settled">
          {view.last_epoch === null || view.last_height === null ? (
            "never"
          ) : (
            <Text variant="bodyMedium">
              epoch {formatInteger(view.last_epoch)} at{" "}
              <Link href={`#/blocks/${view.last_height}`} mono>
                #{formatInteger(view.last_height)}
              </Link>
            </Text>
          )}
        </KeyValue>
        <KeyValue label="History points" mono>
          {formatInteger(view.history.length)}
        </KeyValue>
      </Section>

      <Section
        title="Games"
        subtitle={
          mine.length === 0
            ? "no game with this seat in the current list"
            : `${mine.length} game(s) with this seat`
        }
      >
        {mine.length === 0 ? (
          <Text variant="bodyMedium" style={styles.muted}>
            {shortAddress(address)} has no proof in the games this node knows about.
          </Text>
        ) : (
          <GameTable games={mine} />
        )}
      </Section>

      <Text variant="bodySmall" style={styles.muted}>
        The list is the node&apos;s recent proofs, not a full history; the rating above is replayed
        from every proof on the adopted branch.
      </Text>
    </>
  );
}

const styles = StyleSheet.create({
  titleRow: {
    flexDirection: "row",
    alignItems: "flex-start",
    justifyContent: "space-between",
    gap: 12,
    flexWrap: "wrap",
  },
  titleBlock: { gap: 4, flexShrink: 1 },
  titleActions: { flexDirection: "row", alignItems: "center", gap: 10, flexWrap: "wrap" },
  stats: { flexDirection: "row", flexWrap: "wrap", gap: 12 },
  muted: { opacity: 0.66 },
});
