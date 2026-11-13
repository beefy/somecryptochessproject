/**
 * One game: the settlement state, the certificate that opened it and the PGN.
 *
 * The game hash is the identity here, not the match id: `/v1/games/{hash}` is the
 * canonical lookup, and the transcript (seats, opening, both start signatures,
 * the co-signed result) is fetched separately because a node only serves it for
 * matches it has actually seen.
 */

import type { ReactNode } from "react";
import { StyleSheet, View } from "react-native";
import { Text } from "react-native-paper";

import { fetchGame, fetchTranscript } from "../api";
import {
  CodeBlock,
  EmptyState,
  FailureNotice,
  KeyValue,
  Link,
  Loading,
  Mono,
  Section,
  StatusPill,
} from "../components/Chrome";
import { formatInteger, shortAddress } from "../lib/format";
import { useResource } from "../useResource";
import type { GameView } from "../types";

export function GameDetail({ gameHash }: { gameHash: string }): ReactNode {
  const game = useResource((signal) => fetchGame(gameHash, signal), [gameHash], { pollMs: 10000 });
  const matchId = game.data?.match_id ?? undefined;
  const transcript = useResource(
    async (signal) => {
      if (matchId === undefined) return undefined;
      try {
        return await fetchTranscript(matchId, signal);
      } catch {
        // A node only caches transcripts for matches it has seen; that is not a
        // failure of this page, so the rest of the game still renders.
        return undefined;
      }
    },
    [matchId],
  );

  if (game.error !== undefined) return <FailureNotice error={game.error} />;
  if (game.data === undefined) return <Loading />;

  const view = game.data;
  const seen = transcript.data;
  const certificate = seen?.certificate;
  const result = seen?.result;

  return (
    <>
      <View style={styles.titleRow}>
        <View style={styles.titleBlock}>
          <Text variant="headlineSmall">Game</Text>
          <Mono copy>{view.game_hash}</Mono>
        </View>
        <View style={styles.titleActions}>
          <StatusPill label={phaseLabel(view)} tone={phaseTone(view)} />
          <Mono copy>{view.winner === null ? "winner unknown" : shortAddress(view.winner)}</Mono>
        </View>
      </View>

      <Section title="Settlement" subtitle="where the proof stands on this node">
        <KeyValue label="Winner">
          {view.winner === null ? (
            "unknown"
          ) : (
            <Link href={`#/accounts/${view.winner}`} mono>
              {view.winner}
            </Link>
          )}
        </KeyValue>
        <KeyValue label="Block">
          {view.height === null ? (
            "not mined yet"
          ) : (
            <Link href={`#/blocks/${view.height}`} mono>
              #{formatInteger(view.height)}
            </Link>
          )}
        </KeyValue>
        <KeyValue label="Proof id">{view.proof_id === null ? "-" : <Mono copy>{view.proof_id}</Mono>}</KeyValue>
        <KeyValue label="Match">{view.match_id === null ? "-" : <Mono copy>{view.match_id}</Mono>}</KeyValue>
        <KeyValue label="Challenge" mono>
          {view.challenge_status}
        </KeyValue>
        <KeyValue label="Quality">{view.quality_verdict === null ? "not sampled" : view.quality_verdict}</KeyValue>
        {view.quality_trace_hash !== null && (
          <KeyValue label="Quality trace">
            <Mono copy>{view.quality_trace_hash}</Mono>
          </KeyValue>
        )}
      </Section>

      {certificate !== undefined && (
        <Section title="Certificate" subtitle="the signed start-of-match commitment">
          <KeyValue label="Epoch / round" mono>
            {`${certificate.epoch} / ${certificate.round}`}
          </KeyValue>
          <KeyValue label="White">
            <Link href={`#/accounts/${certificate.white}`} mono>
              {certificate.white}
            </Link>
          </KeyValue>
          <KeyValue label="Black">
            <Link href={`#/accounts/${certificate.black}`} mono>
              {certificate.black}
            </Link>
          </KeyValue>
          <KeyValue label="Opening">
            {`${certificate.opening.book_version} #${certificate.opening.line_id}: ${certificate.opening.moves.join(" ")}`}
          </KeyValue>
          <KeyValue label="Engines">
            {`${certificate.engine_white} vs ${certificate.engine_black}`}
          </KeyValue>
          <KeyValue label="Time control">
            {`${formatInteger(certificate.time_control.initial_ms / 1000)}s + ${formatInteger(
              certificate.time_control.increment_ms / 1000,
            )}s`}
          </KeyValue>
          <KeyValue label="Start signatures">
            <CodeBlock>{JSON.stringify(certificate.start_signatures, null, 2)}</CodeBlock>
          </KeyValue>
        </Section>
      )}

      {result !== undefined && (
        <Section title="Result" subtitle="both seats sign over the game hash, never the moves">
          <KeyValue label="Outcome" mono>
            {`${result.result} by ${result.termination}, ${result.plies} plies`}
          </KeyValue>
          <KeyValue label="Result signatures">
            <CodeBlock>{JSON.stringify(result.result_signatures, null, 2)}</CodeBlock>
          </KeyValue>
        </Section>
      )}

      <Section
        title="PGN"
        subtitle={
          seen === undefined ? "this node has no transcript for the match" : "as replayed during verification"
        }
      >
        {seen === undefined ? <EmptyState>No transcript available for this game</EmptyState> : <CodeBlock>{seen.pgn}</CodeBlock>}
      </Section>
    </>
  );
}

/** Settlement state as a pill label. */
function phaseLabel(view: GameView): string {
  if (view.in_mempool) return "pending";
  if (view.challenge_status === "open") return "challenged";
  if (view.challenge_status === "upheld") return "challenge upheld";
  if (view.challenge_status === "rejected") return "challenge rejected";
  if (view.quality_verdict === "fail") return "quality fail";
  return "settled";
}

/** And as a pill colour. */
function phaseTone(view: GameView): "good" | "warn" | "bad" | "info" | "neutral" {
  if (view.in_mempool) return "warn";
  if (view.challenge_status === "open" || view.challenge_status === "upheld") return "bad";
  if (view.quality_verdict === "fail") return "bad";
  if (view.challenge_status === "rejected") return "info";
  return "good";
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
});
