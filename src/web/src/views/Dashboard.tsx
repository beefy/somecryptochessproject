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

import { fetchBlocks, fetchGames } from "../api";
import { BlockTable, StatCard } from "../components/BlockTable";
import { FailureNotice, KeyValue, Loading, Section } from "../components/Chrome";
import { GameTable } from "../components/GameTable";
import { tokensFor, variantLabel } from "../lib/assets";
import { formatAge, formatAmount, formatInteger, formatTokenAmount } from "../lib/format";
import { useNode } from "../node";
import { useNow, useResource } from "../useResource";

export function Dashboard(): ReactNode {
  const { status, params } = useNode();
  const blocks = useResource((signal) => fetchBlocks({ limit: 6, signal }), [], { pollMs: 5000 });
  const games = useResource((signal) => fetchGames({ signal }), [], { pollMs: 8000 });
  const now = useNow(1000);
  const { width } = useWindowDimensions();
  const wide = width >= 720;

  const tip = blocks.data?.blocks[0];
  const tokens = tokensFor(params.data);
  const variantTokens = tokens.filter((token) => !token.native);

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
        <StatCard
          label="Burned"
          value={status.data === undefined ? "-" : formatAmount(status.data.burned)}
          hint="removed by slashing"
        />
      </View>

      {variantTokens.length > 0 && status.data !== undefined && (
        <Section
          title="Variant tokens"
          subtitle="every variant mints its own token; native SCCP remains the fee currency"
        >
          {/* `minted` starts at the genesis premine and grows by each block's
              subsidy plus its transaction fees. */}
          <KeyValue label="SCCP minted + fees" mono>
            {formatAmount(status.data.minted)}
          </KeyValue>
          {variantTokens.map((token) => (
            <KeyValue key={token.asset ?? token.symbol} label={`${variantLabel(token.variant)} · ${token.symbol}`} mono>
              {formatTokenAmount(
                status.data?.minted_assets?.[token.asset ?? ""] ?? 0,
                token.symbol,
                token.decimals,
              )}{" "}
              · {formatTokenAmount(token.subsidy, token.symbol, token.decimals)} per settled proof
            </KeyValue>
          ))}
        </Section>
      )}

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
    </>
  );
}

const styles = StyleSheet.create({
  hero: { gap: 8, paddingTop: 8, maxWidth: 760 },
  heroTitle: { fontWeight: "600" },
  heroBody: { opacity: 0.8 },
  stats: { flexDirection: "row", flexWrap: "wrap", gap: 12 },
  muted: { opacity: 0.66 },
});
