/**
 * The wallet's state, visible on every page.
 *
 * It is deliberately a strip under the header rather than a chip in the crowded
 * top-right: active/inactive is the thing a person needs before opening Play,
 * DEX or Bridge, and an address plus a Lock button has to stay readable on a
 * phone. Locking here clears the in-memory key for the whole tab.
 */

import type { ReactNode } from "react";
import { StyleSheet, View } from "react-native";
import { Button, Text, useTheme } from "react-native-paper";

import { shortAddress } from "../lib/format";
import { navigate } from "../router";
import { useWalletSession } from "./WalletSession";

export function WalletStatusBar(): ReactNode {
  const { session, lock } = useWalletSession();
  const { dark } = useTheme();
  const active = session !== null;
  const background = active
    ? dark
      ? "rgba(76, 175, 80, 0.18)"
      : "rgba(46, 125, 50, 0.10)"
    : dark
      ? "rgba(255, 255, 255, 0.06)"
      : "rgba(95, 99, 104, 0.08)";
  const colour = active ? (dark ? "#9adf9f" : "#1e6b27") : dark ? "#c7c7c7" : "#4b4f52";

  return (
    <View style={[styles.bar, { backgroundColor: background, borderColor: `${colour}55` }]}>
      <View style={styles.message}>
        <Text variant="labelLarge" style={[styles.state, { color: colour }]}>
          {active ? "Wallet active" : "Wallet inactive"}
        </Text>
        <Text variant="bodySmall" style={{ color: colour }} numberOfLines={1}>
          {active && session !== null
            ? `${shortAddress(session.record.address)} · ${session.record.network}`
            : "unlock the wallet to use Play, DEX or Bridge"}
        </Text>
      </View>
      {active ? (
        <Button compact mode="outlined" textColor={colour} onPress={() => lock("wallet locked")}>
          Lock
        </Button>
      ) : (
        <Button compact mode="outlined" textColor={colour} onPress={() => navigate("#/wallet")}>
          Open wallet
        </Button>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  bar: {
    marginTop: 4,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderBottomWidth: StyleSheet.hairlineWidth,
    paddingVertical: 5,
    paddingHorizontal: 16,
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    maxWidth: 1080,
    width: "100%",
    alignSelf: "center",
  },
  message: {
    flex: 1,
    flexDirection: "row",
    alignItems: "baseline",
    gap: 8,
    flexWrap: "wrap",
  },
  state: { fontWeight: "700" },
});
