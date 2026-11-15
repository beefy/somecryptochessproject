/**
 * The in-progress games: the relay's open sessions, not the chain's proofs.
 *
 * A settled game is a block explorer object; a game being played is a relay
 * object, and the two live in different services. This page asks the relay only
 * for `pending` and `active` sessions, polls while it is open, and counts down
 * the side to move locally between answers. Finished sessions are not shown
 * here: their proofs appear on the Games page once a block settles one.
 */

import type { ReactNode } from "react";
import { StyleSheet, useWindowDimensions, View } from "react-native";
import { Button, HelperText, Text } from "react-native-paper";

import { EmptyState, Link, Loading, Section, StatusPill } from "../components/Chrome";
import { formatClock, formatInteger, shortAddress, shortHash } from "../lib/format";
import { relayMatches } from "../lib/relay";
import type { RelayMatchView } from "../types";
import { useNow, useResource } from "../useResource";

/** How often the relay's open sessions are refreshed. */
const POLL_MS = 2000;

export function LiveGames(): ReactNode {
  const open = useResource(
    (signal) => relayMatches({ statuses: ["pending", "active"], signal }),
    [],
    { pollMs: POLL_MS },
  );
  const now = useNow(500);
  const { width } = useWindowDimensions();
  const wide = width >= 720;

  // An older relay may ignore `status`, so the filter is repeated here: the page
  // must not show a finished session as live just because the parameter was
  // unknown to the service it asked.
  const matches = (open.data ?? []).filter(
    (match) => match.status === "pending" || match.status === "active",
  );
  const active = matches.filter((match) => match.status === "active").length;
  const elapsed = open.updatedAt === undefined ? 0 : Math.max(0, now - open.updatedAt);

  return (
    <>
      <View style={styles.hero}>
        <Text variant="headlineMedium" style={styles.heroTitle}>
          Live games
        </Text>
        <Text variant="bodyLarge" style={styles.heroBody}>
          A match is played at the relay, then co-signed and settled on chain. This page shows
          the sessions the relay still has open, with the clocks it is enforcing; a finished
          game moves to the Games page when a block carries its proof.
        </Text>
      </View>

      <Section
        title="In progress"
        subtitle={
          open.data === undefined
            ? "waiting for the relay"
            : `${formatInteger(matches.length)} open · ${formatInteger(active)} active`
        }
        action={
          <Button mode="outlined" compact onPress={open.reload} disabled={open.loading}>
            Refresh
          </Button>
        }
      >
        {open.error !== undefined ? (
          <View style={styles.failure}>
            <Text variant="bodyMedium">{open.error.message}</Text>
            <Text variant="bodySmall" style={styles.muted}>
              The relay is a separate service from the node. Start one with{" "}
              <Text style={styles.mono}>sccp matchmaker run</Text>, or point{" "}
              <Text style={styles.mono}>VITE_SCCP_RELAY</Text> at a relay URL.
            </Text>
            <Button mode="outlined" onPress={open.reload}>
              Retry
            </Button>
          </View>
        ) : open.data === undefined ? (
          <Loading />
        ) : matches.length === 0 ? (
          <EmptyState>No game is in progress at this relay</EmptyState>
        ) : (
          <View>
            <View style={styles.header}>
              <Text variant="labelSmall" style={[styles.colStatus, styles.muted]}>
                STATE
              </Text>
              <Text variant="labelSmall" style={[styles.colSeats, styles.muted]}>
                SEATS
              </Text>
              <Text variant="labelSmall" style={[styles.colClock, styles.muted]}>
                CLOCKS
              </Text>
              {wide && (
                <Text variant="labelSmall" style={[styles.colRound, styles.muted]}>
                  ROUND
                </Text>
              )}
            </View>
            {matches.map((match) => (
              <LiveRow key={match.matchup.match_id} match={match} elapsed={elapsed} wide={wide} />
            ))}
          </View>
        )}
        {open.loading && open.data !== undefined && (
          <Text variant="bodySmall" style={styles.muted}>
            refreshing…
          </Text>
        )}
      </Section>

      <HelperText type="info" visible>
        Clocks are the relay&apos;s numbers at its last answer, counted down locally while the
        side to move is known. A seat that falls to zero is flagged by the relay and the
        opponent settles that with a timeout proof.
      </HelperText>
    </>
  );
}

function LiveRow({
  match,
  elapsed,
  wide,
}: {
  match: RelayMatchView;
  elapsed: number;
  wide: boolean;
}): ReactNode {
  const live = match.status === "active";
  const whiteMs = Math.max(0, match.white_ms - (live && match.turn === "white" ? elapsed : 0));
  const blackMs = Math.max(0, match.black_ms - (live && match.turn === "black" ? elapsed : 0));
  const timeControl = `${Math.round(match.time_control.initial_ms / 60000)}+${Math.round(
    match.time_control.increment_ms / 1000,
  )}`;

  return (
    <View style={styles.row}>
      <View style={styles.colStatus}>
        <StatusPill
          label={match.status === "pending" ? "waiting" : "active"}
          tone={match.status === "pending" ? "warn" : "good"}
        />
      </View>
      <View style={styles.colSeats}>
        <View style={styles.seat}>
          <Text variant="bodySmall" style={styles.muted}>
            white
          </Text>
          <Link href={`#/miners/${match.matchup.white}`} mono>
            {shortAddress(match.matchup.white)}
          </Link>
        </View>
        <View style={styles.seat}>
          <Text variant="bodySmall" style={styles.muted}>
            black
          </Text>
          <Link href={`#/miners/${match.matchup.black}`} mono>
            {shortAddress(match.matchup.black)}
          </Link>
        </View>
        <Text variant="bodySmall" style={styles.muted}>
          {`${shortHash(match.matchup.match_id, 12)} · ${formatInteger(match.last_ply)} plies · ${timeControl}`}
        </Text>
      </View>
      <View style={styles.colClock}>
        <Text
          variant="bodySmall"
          style={[styles.numeric, match.turn === "white" && live && styles.turn]}
        >
          {`W ${formatClock(whiteMs)}`}
        </Text>
        <Text
          variant="bodySmall"
          style={[styles.numeric, match.turn === "black" && live && styles.turn]}
        >
          {`B ${formatClock(blackMs)}`}
        </Text>
      </View>
      {wide && (
        <Text variant="bodySmall" style={[styles.colRound, styles.muted, styles.numeric]}>
          {`e${match.matchup.epoch} r${match.matchup.round}`}
        </Text>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  hero: { gap: 8, paddingTop: 8 },
  heroTitle: { fontWeight: "600" },
  heroBody: { opacity: 0.78, maxWidth: 760 },
  failure: { gap: 8, alignItems: "flex-start" },
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
  colStatus: { width: 88 },
  colSeats: { flexGrow: 1, flexShrink: 1, minWidth: 180, gap: 2 },
  colClock: { width: 84, alignItems: "flex-end", gap: 2 },
  colRound: { width: 72 },
  seat: { flexDirection: "row", alignItems: "baseline", gap: 6 },
  muted: { opacity: 0.66 },
  numeric: { fontVariant: ["tabular-nums"] },
  turn: { fontWeight: "700" },
  mono: { fontFamily: "monospace" },
});
