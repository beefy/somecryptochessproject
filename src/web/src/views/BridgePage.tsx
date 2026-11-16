/**
 * The bridge's public reserved space.
 *
 * It is deliberately a notice, not a mock form: a page that looks like it can
 * move assets and then does nothing is worse than saying so. The route is gated
 * like Play and DEX because whatever bridge eventually ships will sign from the
 * same wallet.
 */

import type { ReactNode } from "react";
import { StyleSheet, View } from "react-native";
import { HelperText, Text } from "react-native-paper";

import { Section, StatusPill } from "../components/Chrome";
import { WalletGate } from "../components/WalletGate";

export function BridgePage(): ReactNode {
  return (
    <View style={styles.root}>
      <View style={styles.header}>
        <Text variant="headlineSmall">Bridge</Text>
        <StatusPill label="not implemented" tone="warn" />
      </View>
      <WalletGate purpose="Bridge">
        <Section title="No bridge yet" subtitle="reserved route, intentionally empty">
          <Text variant="bodyMedium">
            This page is a placeholder. Nothing here creates a transaction, escrows an asset or
            contacts another chain.
          </Text>
          <HelperText type="info" visible>
            The route and its wallet gate exist so a future bridge can ship without moving URLs.
            Until then, SCCP and C960 move on the SCCP chain only; use the DEX to exchange them.
          </HelperText>
        </Section>
      </WalletGate>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { gap: 16 },
  header: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 12,
  },
});
