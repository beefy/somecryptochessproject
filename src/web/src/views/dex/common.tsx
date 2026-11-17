/** Small shared pieces of the DEX panels. */

import type { ReactNode } from "react";
import { ScrollView, StyleSheet } from "react-native";
import { Button } from "react-native-paper";

import { tokenForAsset, type Token } from "../../lib/assets";
import { parseTokenAmount, shortHash } from "../../lib/format";
import type { PoolView } from "../../types";

/** `SCCP / C960`, using symbols once params have named the assets. */
export function pairLabel(pool: PoolView, tokens: readonly Token[]): string {
  const left = tokenForAsset(tokens, pool.asset_a)?.symbol ?? shortHash(pool.asset_a, 6);
  const right = tokenForAsset(tokens, pool.asset_b)?.symbol ?? shortHash(pool.asset_b, 6);
  return `${left} / ${right}`;
}

export function TokenPicker({
  tokens,
  value,
  onChange,
}: {
  tokens: readonly Token[];
  value: string | null;
  onChange: (asset: string | null) => void;
}): ReactNode {
  return (
    <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.pills}>
      {tokens.map((token) => (
        <Button
          key={token.asset ?? "native"}
          compact
          mode={token.asset === value ? "contained" : "outlined"}
          onPress={() => onChange(token.asset)}
        >
          {token.symbol}
        </Button>
      ))}
    </ScrollView>
  );
}

export function PoolPicker({
  pools,
  value,
  onChange,
  tokens,
}: {
  pools: readonly PoolView[];
  value: string;
  onChange: (poolId: string) => void;
  tokens: readonly Token[];
}): ReactNode {
  return (
    <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.pills}>
      {pools.map((pool) => (
        <Button
          key={pool.pool_id}
          compact
          mode={pool.pool_id === value ? "contained" : "outlined"}
          onPress={() => onChange(pool.pool_id)}
        >
          {pairLabel(pool, tokens)}
        </Button>
      ))}
    </ScrollView>
  );
}

/** Parse an optional field; an empty field is `null`, not zero. */
export function amountOrNull(text: string, token: Token): number | null {
  if (text.trim() === "") return null;
  return parseTokenAmount(text, token.symbol, token.decimals);
}

/** The minimum output for a slippage tolerance, using exact integer arithmetic. */
export function minAfterSlippage(amount: number, slippageBps: number): number {
  return Number((BigInt(amount) * BigInt(10_000 - slippageBps)) / 10_000n);
}

export const dexStyles = StyleSheet.create({
  root: { gap: 16 },
  header: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 12,
  },
  muted: { opacity: 0.66 },
  actions: { flexDirection: "row", alignItems: "center", gap: 8, flexWrap: "wrap" },
  pills: { gap: 8, paddingVertical: 4, alignItems: "center" },
  position: {
    flexDirection: "row",
    alignItems: "flex-start",
    justifyContent: "space-between",
    gap: 12,
    flexWrap: "wrap",
  },
  positionText: { gap: 2, flexShrink: 1 },
});

const styles = dexStyles;
