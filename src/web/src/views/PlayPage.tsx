/**
 * The Play page, with its own URL and enough room for the board.
 *
 * The table was a wallet tab when the wallet was the only page that held a key.
 * It is now a top-level view (`https://proofofchess.com/#/play`), gated by the
 * shared wallet session, and the page owns the title so the board can use the
 * full column instead of sharing it with a wallet header.
 */

import type { ReactNode } from "react";
import { StyleSheet, View } from "react-native";
import { Text } from "react-native-paper";

import { KeyValue, Mono, StatusPill } from "../components/Chrome";
import { WalletGate } from "../components/WalletGate";
import { useWalletSession } from "../components/WalletSession";
import { shortAddress } from "../lib/format";
import { Play } from "./Play";

export function PlayPage(): ReactNode {
  return (
    <View style={styles.root}>
      <View style={styles.header}>
        <View style={styles.headerText}>
          <Text variant="headlineSmall">Play</Text>
          <Text variant="bodyMedium" style={styles.muted}>
            A real seat in a cleared match: register, commit availability, play, and
            let the chain settle the proof.
          </Text>
        </View>
      </View>
      <WalletGate purpose="Play">
        <Table />
      </WalletGate>
    </View>
  );
}

function Table(): ReactNode {
  const { session } = useWalletSession();
  if (session === null) return null;
  const { record, keypair } = session;
  return (
    <>
      <View style={styles.seat}>
        <View style={styles.seatText}>
          <Text variant="titleSmall">Seat</Text>
          <Mono copy>{record.address}</Mono>
          <Text variant="bodySmall" style={styles.muted}>
            {shortAddress(record.address)} · {record.network} · signed with the unlocked wallet
          </Text>
        </View>
        <StatusPill label="active" tone="good" />
      </View>
      <KeyValue label="Network" mono>
        {record.network}
      </KeyValue>
      <Play keypair={keypair} network={record.network} />
    </>
  );
}

const styles = StyleSheet.create({
  root: { gap: 16 },
  header: { flexDirection: "row", justifyContent: "space-between", gap: 12 },
  headerText: { gap: 4, flexShrink: 1 },
  muted: { opacity: 0.66 },
  seat: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 12,
    flexWrap: "wrap",
  },
  seatText: { gap: 4, flexShrink: 1 },
});
