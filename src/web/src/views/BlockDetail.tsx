/**
 * One block, in full: header, transactions and the proofs it mined.
 *
 * A block is the unit the explorer exists for, so this view prints every header
 * field verbatim rather than summarising. The proofs are the interesting part:
 * each one is a finished chess game, with both seats' signatures and the PGN the
 * node replayed to accept it.
 */

import type { ReactNode } from "react";
import { StyleSheet, View } from "react-native";
import { Button, Divider, Text } from "react-native-paper";

import { fetchBlock } from "../api";
import { CodeBlock, FailureNotice, KeyValue, Link, Loading, Mono, Section, StatusPill } from "../components/Chrome";
import { formatAge, formatInteger, formatTimestamp, prettyJson, shortAddress } from "../lib/format";
import { useNode } from "../node";
import { navigate } from "../router";
import { useNow, useResource } from "../useResource";
import type { WinProof } from "../types";

export function BlockDetail({ reference }: { reference: string }): ReactNode {
  const view = useResource((signal) => fetchBlock(reference, signal), [reference]);
  const { status } = useNode();
  const now = useNow(1000);

  if (view.error !== undefined) return <FailureNotice error={view.error} />;
  if (view.data === undefined) return <Loading />;

  const { block, hash, height, parent_hash, chain_work, main_chain } = view.data;
  const header = block.header;
  const tip = status.data?.height;

  return (
    <>
      <View style={styles.titleRow}>
        <View style={styles.titleBlock}>
          <Text variant="headlineSmall">Block #{formatInteger(height)}</Text>
          <Mono copy>{hash}</Mono>
        </View>
        <View style={styles.titleActions}>
          <StatusPill label={main_chain ? "main chain" : "side chain"} tone={main_chain ? "good" : "warn"} />
          <Button mode="outlined" compact disabled={height <= 0} onPress={() => navigate(`#/blocks/${height - 1}`)}>
            Previous
          </Button>
          <Button
            mode="outlined"
            compact
            disabled={tip === undefined || height >= tip}
            onPress={() => navigate(`#/blocks/${height + 1}`)}
          >
            Next
          </Button>
        </View>
      </View>

      <Section title="Header" subtitle={`sealed ${formatAge(header.timestamp, now)}`}>
        <KeyValue label="Timestamp">
          {`${formatTimestamp(header.timestamp)} (${formatAge(header.timestamp, now)})`}
        </KeyValue>
        <KeyValue label="Parent">
          <Link href={`#/blocks/${parent_hash}`} mono>
            {parent_hash}
          </Link>
        </KeyValue>
        <KeyValue label="Epoch" mono>
          {formatInteger(header.epoch)}
        </KeyValue>
        <KeyValue label="Beacon">
          <Mono copy>{header.beacon_hash}</Mono>
        </KeyValue>
        <KeyValue label="Miner">
          <Link href={`#/accounts/${header.miner}`} mono>
            {header.miner}
          </Link>
        </KeyValue>
        <KeyValue label="Difficulty">
          {`${formatInteger(header.wins_required)} wins required, ${header.seal_bits} seal bits`}
        </KeyValue>
        <KeyValue label="Seal nonce" mono>
          {formatInteger(header.seal_nonce)}
        </KeyValue>
        <KeyValue label="Chain work" mono>
          {formatInteger(chain_work)}
        </KeyValue>
        <KeyValue label="Merkle root">
          <Mono copy>{header.merkle_root}</Mono>
        </KeyValue>
        <KeyValue label="State root">
          <Mono copy>{header.state_root}</Mono>
        </KeyValue>
        <KeyValue label="Roster root">
          <Mono copy>{header.roster_root}</Mono>
        </KeyValue>
        <KeyValue label="Protocol version" mono>
          {header.version}
        </KeyValue>
      </Section>

      <Divider />

      <Section
        title="Transactions"
        subtitle={
          block.coinbase === null
            ? `${block.transactions.length} in this block`
            : `${block.transactions.length + 1} including the coinbase`
        }
      >
        {block.coinbase !== null && (
          <View style={styles.txRow}>
            <StatusPill label="coinbase" tone="info" />
            <Text variant="bodyMedium" style={styles.txFrom}>
              {shortAddress(block.coinbase.from)}
            </Text>
            <Text variant="bodySmall" style={styles.muted}>
              pays the miner
            </Text>
          </View>
        )}
        {block.transactions.map((tx, index) => (
          <View key={`${tx.from}-${tx.nonce}-${index}`} style={styles.txBlock}>
            <View style={styles.txRow}>
              <StatusPill label={tx.type} tone="neutral" />
              <Text variant="bodyMedium" style={styles.txFrom}>
                {shortAddress(tx.from)}
              </Text>
              <Text variant="bodySmall" style={styles.muted}>
                nonce {formatInteger(tx.nonce)} · fee {formatInteger(tx.fee)}
              </Text>
            </View>
            {Object.keys(tx.payload).length > 0 && <CodeBlock>{prettyJson(tx.payload)}</CodeBlock>}
          </View>
        ))}
        {block.coinbase === null && block.transactions.length === 0 && (
          <Text variant="bodyMedium" style={styles.muted}>
            This block carries no transactions.
          </Text>
        )}
      </Section>

      <Divider />

      <Section title="Proofs" subtitle="one finished, co-signed game per proof">
        {block.proofs.length === 0 ? (
          <Text variant="bodyMedium" style={styles.muted}>
            No proof in this block.
          </Text>
        ) : (
          block.proofs.map((proof) => <ProofCard key={proof.proof_id} proof={proof} />)
        )}
      </Section>
    </>
  );
}

function ProofCard({ proof }: { proof: WinProof }): ReactNode {
  return (
    <View style={styles.proof}>
      <View style={styles.txRow}>
        <StatusPill label={`${proof.result} · ${proof.winner_seat}`} tone="good" />
        <Text variant="bodyMedium">
          {shortAddress(proof.winner)} beat {shortAddress(proof.loser)}
        </Text>
        <Text variant="bodySmall" style={styles.muted}>
          {proof.plies} plies · epoch {proof.epoch} round {proof.round}
        </Text>
      </View>
      <KeyValue label="Game">
        <Link href={`#/games/${proof.game_hash}`} mono>
          {proof.game_hash}
        </Link>
      </KeyValue>
      <KeyValue label="Proof id">
        <Mono copy>{proof.proof_id}</Mono>
      </KeyValue>
      <KeyValue label="Opening">
        {`${proof.opening.book_version} #${proof.opening.line_id}: ${proof.opening.moves.join(" ")}`}
      </KeyValue>
      <KeyValue label="PGN">
        <CodeBlock>{proof.pgn}</CodeBlock>
      </KeyValue>
    </View>
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
  titleActions: { flexDirection: "row", alignItems: "center", gap: 8, flexWrap: "wrap" },
  muted: { opacity: 0.66 },
  txRow: { flexDirection: "row", alignItems: "center", gap: 10, flexWrap: "wrap", paddingVertical: 4 },
  txBlock: { gap: 6, paddingVertical: 4 },
  txFrom: { fontVariant: ["tabular-nums"] },
  proof: { gap: 4, paddingVertical: 10 },
});
