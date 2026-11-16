/** Shared pieces of the bridge panels. */

import type { ReactNode } from "react";
import { StyleSheet, View } from "react-native";
import { HelperText, Text } from "react-native-paper";

import { formatTokenAmount, shortAddress, shortHash } from "../../lib/format";
import { formatWei, orderPrice } from "../../lib/bridge";
import type { BridgeView, OrderView } from "../../types";

export function orderBookRow(order: OrderView): ReactNode {
  return (
    <View style={styles.order} key={order.order_id}>
      <View style={styles.orderText}>
        <Text variant="titleSmall">
          {formatTokenAmount(order.give_amount, "SCCP")} for {formatWei(order.want_amount, 6)} ETH
        </Text>
        <Text variant="bodySmall" style={styles.muted}>
          {orderPrice(order)} ETH / SCCP · maker {shortAddress(order.maker_sccp)}
        </Text>
        <Text variant="bodySmall" style={styles.muted}>
          expires {new Date(order.expiry * 1000).toLocaleString()} · order{" "}
          {shortHash(order.order_id, 8)}
        </Text>
      </View>
    </View>
  );
}

export function EthLine({
  bridge,
  account,
  onConnect,
}: {
  bridge: BridgeView;
  account: string | null;
  onConnect: () => Promise<string | null>;
}): ReactNode {
  return (
    <View style={styles.ethLine}>
      <Text variant="bodySmall">
        {bridge.eth_chain_name} (chain {bridge.eth_chain_id})
      </Text>
      <Text variant="bodySmall" style={styles.muted}>
        {account === null ? "Ethereum wallet not connected" : shortAddress(account)}
      </Text>
      {account === null && (
        <Text variant="labelLarge" onPress={() => void onConnect()}>
          Connect Ethereum
        </Text>
      )}
    </View>
  );
}

export function ConfigNotice({ bridge }: { bridge: BridgeView }): ReactNode {
  if (bridge.configured) return null;
  return (
    <HelperText type="error" visible>
      This node has no {bridge.eth_chain_name} HTLC contract configured. Set
      SCCP_ETH_HTLC_CONTRACT on the node after deploying `EthHtlc`; until then no ETH leg can be
      created.
    </HelperText>
  );
}

export const bridgeStyles = StyleSheet.create({
  root: { gap: 16 },
  muted: { opacity: 0.66 },
  order: { flexDirection: "row", alignItems: "flex-start", gap: 12 },
  orderText: { gap: 2, flexShrink: 1 },
  block: {
    gap: 4,
    paddingVertical: 8,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: "rgba(128,128,128,0.24)",
  },
  ethLine: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 8,
    flexWrap: "wrap",
  },
  actions: { flexDirection: "row", alignItems: "center", gap: 8, flexWrap: "wrap" },
  pills: { gap: 8, paddingVertical: 4 },
});

const styles = bridgeStyles;
