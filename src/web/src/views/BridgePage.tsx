/**
 * The bridge: a trustless ETH<->ZQWX atomic swap over signed orders.
 *
 * A maker sells ZQWX and binds an Ethereum address in a hybrid-signed order. A
 * taker locks ETH first under a SHA-256 hashlock; the maker verifies that lock,
 * then locks ZQWX with the same hash under a longer timelock. The taker claims
 * ZQWX (revealing the secret), and the maker uses the now-public secret to claim
 * ETH. Neither chain holds a custodian's key, and every failure path has a
 * timelock refund.
 *
 * The page needs both keys: the SCCP wallet signs orders, takes and HTLC
 * transactions; an EIP-1193 wallet signs the Ethereum leg. The chain ids are
 * checked against `GET /v1/bridge` before anything is signed. Buy, Sell and
 * Activity are separate hash routes, so the shell header renders them as tabs
 * and each one is linkable.
 */

import { useEffect, useState, type ReactNode } from "react";
import { StyleSheet, View } from "react-native";
import { Text } from "react-native-paper";

import { fetchAccount, fetchBridge, fetchOrders } from "../api";
import { FailureNotice, Loading, StatusPill } from "../components/Chrome";
import { useNotice } from "../components/Notices";
import { WalletGate } from "../components/WalletGate";
import { useWalletSession } from "../components/WalletSession";
import { tokensFor } from "../lib/assets";
import { connectEthAccount, currentEthAccount, ensureEthChain } from "../lib/eth";
import { formatInteger } from "../lib/format";
import { useNode } from "../node";
import { useResource } from "../useResource";
import { ActivityPanel } from "./bridge/ActivityPanel";
import { BuyPanel } from "./bridge/BuyPanel";
import { SellPanel } from "./bridge/SellPanel";
import type { BridgeContext } from "./bridge/types";

export type BridgeTab = "buy" | "sell" | "activity";

export function BridgePage({ view }: { view: BridgeTab }): ReactNode {
  return (
    <View style={styles.root}>
      <View style={styles.header}>
        <Text variant="headlineSmall">Bridge</Text>
        <StatusPill label="ETH ↔ ZQWX" tone="info" />
      </View>
      <Text variant="bodyMedium" style={styles.muted}>
        Swap ETH for ZQWX without a custodian. Orders are signed off-chain; both legs are
        hash-time-locked, so a stalled swap refunds instead of stranding funds.
      </Text>
      <WalletGate purpose="the Bridge">
        <Desk view={view} />
      </WalletGate>
    </View>
  );
}

function Desk({ view }: { view: BridgeTab }): ReactNode {
  const { session, pendingNonce, setPendingNonce } = useWalletSession();
  const { status: statusResource, params: paramsResource } = useNode();
  const notify = useNotice();
  const [ethAccount, setEthAccount] = useState<string | null>(null);
  const [ethError, setEthError] = useState<Error | null>(null);
  const address = session?.record.address ?? "";
  const account = useResource(
    (signal) => (address === "" ? Promise.resolve(null) : fetchAccount(address, signal)),
    [address],
    { pollMs: 8000 },
  );
  const bridge = useResource((signal) => fetchBridge(signal), [], { pollMs: 30_000 });
  const orders = useResource((signal) => fetchOrders({}, signal), [], { pollMs: 10_000 });
  const params = paramsResource.data;
  const status = statusResource.data;
  const accountView = account.data ?? null;
  const chainNonce = accountView?.nonce;

  useEffect(() => {
    if (chainNonce !== undefined && pendingNonce !== null && chainNonce >= pendingNonce) {
      setPendingNonce(null);
    }
  }, [chainNonce, pendingNonce, setPendingNonce]);

  useEffect(() => {
    if (bridge.data === undefined) return;
    void currentEthAccount().then(setEthAccount);
  }, [bridge.data]);

  if (session === null) return null;
  if (
    params === undefined ||
    status === undefined ||
    accountView === null ||
    bridge.data === undefined ||
    orders.data === undefined
  ) {
    if (bridge.error !== undefined) return <FailureNotice error={bridge.error} />;
    if (orders.error !== undefined) return <FailureNotice error={orders.error} />;
    if (account.error !== undefined) return <FailureNotice error={account.error} />;
    return <Loading />;
  }

  if (bridge.data.network !== status.network || orders.data.network !== status.network) {
    return (
      <FailureNotice
        error={new Error("the node answered for a different network; reload after it settles")}
      />
    );
  }
  const tokens = tokensFor(params);
  if (tokens.length === 0) return null;
  const bridgeInfo = bridge.data;
  const reload = (): void => {
    account.reload();
    orders.reload();
  };
  const connectEth = async (): Promise<string | null> => {
    setEthError(null);
    try {
      await ensureEthChain({
        chainId: bridgeInfo.eth_chain_id,
        chainName: bridgeInfo.eth_chain_name,
        rpcUrl: bridgeInfo.eth_rpc_url,
        explorerUrl: bridgeInfo.eth_explorer_url,
      });
      const eth = await connectEthAccount();
      setEthAccount(eth);
      return eth;
    } catch (failure) {
      setEthError(failure instanceof Error ? failure : new Error(String(failure)));
      return null;
    }
  };
  const context: BridgeContext = {
    network: session.record.network,
    address,
    keypair: session.keypair,
    account: accountView,
    params,
    status,
    bridge: bridgeInfo,
    orders: orders.data.orders,
    nonce: pendingNonce ?? accountView.nonce,
    reload,
    setPendingNonce,
    notify,
    ethAccount,
    connectEth,
  };


  return (
    <>
      <View style={styles.summary}>
        <StatusPill
          label={
            bridgeInfo.configured
              ? `${bridgeInfo.eth_chain_name} ready`
              : "ETH not configured"
          }
          tone={bridgeInfo.configured ? "good" : "warn"}
        />
        <Text variant="bodySmall" style={styles.muted}>
          SCCP height {formatInteger(status.height)} · {bridgeInfo.eth_confirmations} ETH
          confirmations · margin {bridgeInfo.sccp_margin} blocks
        </Text>
      </View>
      {bridge.error !== undefined && <FailureNotice error={bridge.error} />}
      {orders.error !== undefined && <FailureNotice error={orders.error} />}
      {ethError !== null && <FailureNotice error={ethError} />}
      {view === "buy" && <BuyPanel {...context} />}
      {view === "sell" && <SellPanel {...context} />}
      {view === "activity" && <ActivityPanel account={accountView} bridge={bridge.data} />}
    </>
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
  muted: { opacity: 0.66 },
  summary: { gap: 6 },
});
