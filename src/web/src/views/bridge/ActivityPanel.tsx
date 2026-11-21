import type { ReactNode } from "react";
import { View } from "react-native";
import { HelperText, Text } from "react-native-paper";

import { EmptyState, KeyValue, Section } from "../../components/Chrome";
import { formatTokenAmount, shortAddress, shortHash } from "../../lib/format";
import type { AccountView, BridgeView } from "../../types";
import { bridgeStyles as styles } from "./common";

export function ActivityPanel({
  account,
  bridge,
}: {
  account: AccountView;
  bridge: BridgeView;
}): ReactNode {
  return (
    <Section title="Activity" subtitle="SCCP hash locks involving this wallet">
      {account.htlcs.length === 0 ? (
        <EmptyState>No bridge locks yet. A completed swap leaves both legs on chain.</EmptyState>
      ) : (
        account.htlcs.map((htlc) => (
          <View key={htlc.htlc_id} style={styles.block}>
            <Text variant="titleSmall">
              {formatTokenAmount(htlc.amount, htlc.asset === null ? "ZQWX" : "asset", 8)}
            </Text>
            <Text variant="bodySmall" style={styles.muted}>
              {htlc.sender === account.address ? "locked by you" : "payable to you"} ·{" "}
              {htlc.status} · {shortHash(htlc.htlc_id, 8)}
            </Text>
            <KeyValue label="Counterparty" mono>
              {shortAddress(htlc.sender === account.address ? htlc.beneficiary : htlc.sender)}
            </KeyValue>
            <KeyValue label="Timelock">height {htlc.timelock_height}</KeyValue>
            {htlc.preimage !== null && (
              <KeyValue label="Revealed preimage" mono>
                {shortHash(htlc.preimage, 10)}
              </KeyValue>
            )}
          </View>
        ))
      )}
      {!bridge.configured && (
        <HelperText type="info" visible>
          ZQWX locks are visible without a configured Ethereum contract; the ETH actions are
          disabled until one is set.
        </HelperText>
      )}
    </Section>
  );
}
