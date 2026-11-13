/**
 * One account: balances, nonce and, for a miner, its registration.
 *
 * `total` is shown next to the breakdown because the node defines it as the sum
 * of spendable, immature and bonded coins -- printing only `balance` would make a
 * miner that has just won a block look empty until maturity.
 */

import type { ReactNode } from "react";
import { StyleSheet, View } from "react-native";
import { Text } from "react-native-paper";

import { fetchAccount, fetchGames } from "../api";
import { FailureNotice, KeyValue, Link, Loading, Mono, Section, StatusPill } from "../components/Chrome";
import { GameTable } from "../components/GameTable";
import { formatAmount, formatInteger, shortAddress } from "../lib/format";
import { useNode } from "../node";
import { useResource } from "../useResource";

export function AccountView({ address }: { address: string }): ReactNode {
  const account = useResource((signal) => fetchAccount(address, signal), [address], { pollMs: 10000 });
  const games = useResource((signal) => fetchGames(signal), [], { pollMs: 20000 });
  const { params } = useNode();

  if (account.error !== undefined) return <FailureNotice error={account.error} />;
  if (account.data === undefined) return <Loading />;

  const view = account.data;
  const mine = (games.data?.games ?? []).filter((game) => game.winner === address || game.loser === address);
  const registered = view.registered_until_epoch !== null;

  return (
    <>
      <View style={styles.titleRow}>
        <View style={styles.titleBlock}>
          <Text variant="headlineSmall">Account</Text>
          <Mono copy>{view.address}</Mono>
        </View>
        <View style={styles.titleActions}>
          <StatusPill label={registered ? "registered miner" : "plain account"} tone={registered ? "good" : "neutral"} />
          {view.banned_until > 0 && <StatusPill label={`banned to ${formatInteger(view.banned_until)}`} tone="bad" />}
        </View>
      </View>

      <Section title="Balance" subtitle="millionths of a coin, one coin per block subsidy unit">
        <KeyValue label="Total" mono>
          {formatAmount(view.total)}
        </KeyValue>
        <KeyValue label="Spendable" mono>
          {formatAmount(view.balance)}
        </KeyValue>
        <KeyValue label="Immature" mono>
          {formatAmount(view.immature)}
        </KeyValue>
        <KeyValue label="Bonded" mono>
          {formatAmount(view.bonded)}
        </KeyValue>
        <KeyValue label="Next nonce" mono>
          {formatInteger(view.nonce)}
        </KeyValue>
      </Section>

      <Section title="Registration" subtitle="what this account last announced">
        <KeyValue label="Registered until">
          {view.registered_until_epoch === null ? "not registered" : `epoch ${formatInteger(view.registered_until_epoch)}`}
        </KeyValue>
        <KeyValue label="Endpoint" mono>
          {view.endpoint ?? "-"}
        </KeyValue>
        <KeyValue label="Engine" mono>
          {view.engine ?? "-"}
        </KeyValue>
        <KeyValue label="Public key">
          {view.public_key === null ? "-" : <Mono copy>{view.public_key}</Mono>}
        </KeyValue>
        {registered && (
          <KeyValue label="Elo">
            <Link href={`#/miners/${address}`}>rating history</Link>
          </KeyValue>
        )}
        {params.data !== undefined && (
          <KeyValue label="Minimum bond" mono>
            {formatAmount(params.data.min_bond)}
          </KeyValue>
        )}
      </Section>

      <Section
        title="Games"
        subtitle={mine.length === 0 ? "no game with this seat in the current list" : `${mine.length} game(s) with this seat`}
      >
        {mine.length === 0 ? (
          <Text variant="bodyMedium" style={styles.muted}>
            {shortAddress(address)} has no proof in the games this node knows about.
          </Text>
        ) : (
          <GameTable games={mine} />
        )}
      </Section>

      {games.data !== undefined && mine.length === 0 && (
        <Text variant="bodySmall" style={styles.muted}>
          The list is the node&apos;s recent proofs, not a full history: an old game may not appear.
        </Text>
      )}
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
  muted: { opacity: 0.66 },
});
