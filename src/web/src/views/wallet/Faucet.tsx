/**
 * The wallet's Faucet tab.
 *
 * Funding is a request to the node, not a local mint: the panel says so, keeps
 * the button disabled while the request is in flight, and names the grant's
 * transaction id afterwards. On mainnet there is no faucet, and the tab says
 * that instead of hiding a button and leaving a blank page.
 */

import { useState, type ReactNode } from "react";
import { StyleSheet, View } from "react-native";
import { Button, HelperText, ProgressBar, Text } from "react-native-paper";

import { requestFaucet } from "../../api";
import { FailureNotice, KeyValue, Mono, Section } from "../../components/Chrome";
import { useNotice } from "../../components/Notices";
import { labelFor, readContacts } from "../../lib/contacts";
import { formatAmount, formatCoins, shortHash } from "../../lib/format";
import type { AccountView, FaucetView } from "../../types";

export function FaucetPanel({
  address,
  network,
  account,
  onFunded,
}: {
  address: string;
  network: string;
  /** `null` while the node has not answered; the faucet itself needs only the address. */
  account: AccountView | null;
  onFunded: () => void;
}): ReactNode {
  const notify = useNotice();
  const [busy, setBusy] = useState(false);
  const [grant, setGrant] = useState<FaucetView | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const mainnet = network === "mainnet";
  const nickname = labelFor(readContacts(), address);

  const fund = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      const result = await requestFaucet(address);
      setGrant(result);
      notify(`faucet sent ${formatCoins(result.amount)} · ${shortHash(result.txid, 12)}…`);
      onFunded();
    } catch (failure) {
      const described = failure instanceof Error ? failure : new Error(String(failure));
      setError(described);
      notify(described.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section title="Faucet" subtitle={nickname === null ? network : `${nickname} · ${network}`}>
      <KeyValue label="Address" mono>
        {address}
      </KeyValue>
      {account !== null && (
        <KeyValue label="Spendable now" mono>
          {formatAmount(account.balance)}
        </KeyValue>
      )}

      {mainnet ? (
        <HelperText type="info" visible>
          The faucet is a testnet service. This wallet is on mainnet, where coins come from
          mining or a transfer, not from this page.
        </HelperText>
      ) : (
        <>
          <Text variant="bodyMedium">
            Ask the node to fund this address. The amount is the node&apos;s choice, not this
            page&apos;s. Faucet grants are native SCCP only; C960 comes from settled Chess960
            proofs or a transfer.
          </Text>
          <View style={styles.actions}>
            <Button mode="contained" loading={busy} disabled={busy} onPress={() => void fund()}>
              Request testnet funds
            </Button>
          </View>
          {busy && (
            <>
              <ProgressBar indeterminate />
              <Text variant="bodySmall" style={styles.muted}>
                Asking the faucet. It answers with a transaction id once the node accepts the
                grant; the balance refreshes when a block includes it.
              </Text>
            </>
          )}
          {error !== null && <FailureNotice error={error} />}
          {grant !== null && (
            <>
              <KeyValue label="Last grant" mono>
                {formatCoins(grant.amount)}
              </KeyValue>
              <KeyValue label="Transaction">
                <Mono copy>{grant.txid}</Mono>
              </KeyValue>
              <HelperText type="info" visible>
                The node accepted the grant; it appears in History once a scan reaches the block
                that carries it.
              </HelperText>
            </>
          )}
        </>
      )}
    </Section>
  );
}

const styles = StyleSheet.create({
  actions: { flexDirection: "row", alignItems: "center", gap: 8, flexWrap: "wrap" },
  muted: { opacity: 0.66 },
});
