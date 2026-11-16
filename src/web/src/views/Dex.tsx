/**
 * The DEX: one page for swaps, pool creation and LP positions.
 *
 * Every action becomes an ordinary signed ledger transaction, reviewed before it
 * is signed. The page quotes with the same constant-product formula the chain
 * applies, but a quote is only a preview: the transaction carries a minimum
 * output and the node recomputes the fill from its own reserves.
 *
 * Pairs are not hard-coded to SCCP/C960. The token list comes from
 * `GET /v1/params`, so a network that adds another variant gets another pair in
 * the same pickers without a web release.
 */

import { useEffect, useState, type ReactNode } from "react";
import { StyleSheet, View } from "react-native";
import { Button, HelperText, SegmentedButtons, Text } from "react-native-paper";

import { fetchAccount, fetchPools, submitTransaction } from "../api";
import { FailureNotice, KeyValue, Loading, Section, StatusPill } from "../components/Chrome";
import { useNotice } from "../components/Notices";
import { WalletGate } from "../components/WalletGate";
import { useWalletSession } from "../components/WalletSession";
import { tokensFor } from "../lib/assets";
import { formatInteger, shortHash } from "../lib/format";
import { signTransaction, toWireTransaction, txId } from "../lib/tx";
import { useNode } from "../node";
import { useResource } from "../useResource";
import { CreatePanel } from "./dex/CreatePanel";
import { LiquidityPanel } from "./dex/LiquidityPanel";
import { SwapPanel } from "./dex/SwapPanel";
import type { DeskProps, Review } from "./dex/types";

type Tab = "swap" | "liquidity" | "create";

export function DexPage(): ReactNode {
  return (
    <View style={styles.root}>
      <View style={styles.header}>
        <Text variant="headlineSmall">DEX</Text>
        <StatusPill label="on-chain AMM" tone="info" />
      </View>
      <Text variant="bodyMedium" style={styles.muted}>
        Swap SCCP and the network&apos;s variant tokens, or provide liquidity to a
        constant-product pool. Every action is a signed transaction; fees are paid in SCCP.
      </Text>
      <WalletGate purpose="the DEX">
        <Desk />
      </WalletGate>
    </View>
  );
}

function Desk(): ReactNode {
  const { session, pendingNonce, setPendingNonce } = useWalletSession();
  const { params: paramsResource } = useNode();
  const notify = useNotice();
  const [tab, setTab] = useState<Tab>("swap");
  const [review, setReview] = useState<Review | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const address = session?.record.address ?? "";
  const account = useResource(
    (signal) => (address === "" ? Promise.resolve(null) : fetchAccount(address, signal)),
    [address],
    { pollMs: 8000 },
  );
  const pools = useResource((signal) => fetchPools(signal), [], { pollMs: 5000 });
  const params = paramsResource.data;
  const accountView = account.data ?? null;

  const chainNonce = accountView?.nonce;
  useEffect(() => {
    if (chainNonce !== undefined && pendingNonce !== null && chainNonce >= pendingNonce) {
      setPendingNonce(null);
    }
  }, [chainNonce, pendingNonce, setPendingNonce]);

  if (session === null) return null;
  if (params === undefined || accountView === null || pools.data === undefined) {
    if (pools.error !== undefined) return <FailureNotice error={pools.error} />;
    if (account.error !== undefined) return <FailureNotice error={account.error} />;
    return <Loading />;
  }

  const tokens = tokensFor(params);
  const nonce = pendingNonce ?? accountView.nonce;
  const reload = (): void => {
    account.reload();
    pools.reload();
  };
  const submit = async (): Promise<void> => {
    if (review === null) return;
    setBusy(true);
    setError(null);
    try {
      const signed = signTransaction(session.keypair, review.tx);
      await submitTransaction(toWireTransaction(signed));
      setPendingNonce(signed.nonce + 1);
      notify(`${review.title} accepted · pending at nonce ${formatInteger(signed.nonce)}`);
      setReview(null);
      reload();
    } catch (failure) {
      setError(failure instanceof Error ? failure : new Error(String(failure)));
      setPendingNonce(review.tx.nonce > accountView.nonce ? review.tx.nonce : null);
    } finally {
      setBusy(false);
    }
  };


  const desk: DeskProps = {
    address,
    network: session.record.network,
    keypair: session.keypair,
    account: accountView,
    params,
    pools: pools.data.pools,
    tokens,
    nonce,
    reload,
    onReview: (next) => {
      setError(null);
      setReview(next);
    },
  };

  return (
    <>
      <SegmentedButtons
        value={tab}
        onValueChange={(value) => {
          setReview(null);
          setError(null);
          setTab(value as Tab);
        }}
        buttons={[
          { value: "swap", label: "Swap" },
          { value: "liquidity", label: "Liquidity" },
          { value: "create", label: "Create pool" },
        ]}
      />
      {pools.error !== undefined && <FailureNotice error={pools.error} />}
      {tab === "swap" && <SwapPanel {...desk} />}
      {tab === "liquidity" && <LiquidityPanel {...desk} />}
      {tab === "create" && <CreatePanel {...desk} />}
      {review !== null && (
        <Section
          title="Review"
          subtitle="every field below is covered by the hybrid signature"
          action={<StatusPill label="not sent" tone="info" />}
        >
          <KeyValue label="Action">{review.title}</KeyValue>
          {review.rows.map(([label, value]) => (
            <KeyValue key={label} label={label} mono>
              {value}
            </KeyValue>
          ))}
          <KeyValue label="Txid" mono>
            {shortHash(txId(review.tx), 16)}
          </KeyValue>
          <HelperText type="info" visible>
            A swap&apos;s minimum output is part of the signed payload: if the pool moves beyond
            it before the block, the chain rejects the transaction instead of filling it.
          </HelperText>
          {error !== null && <FailureNotice error={error} />}
          <View style={styles.actions}>
            <Button mode="contained" loading={busy} disabled={busy} onPress={() => void submit()}>
              Sign and send
            </Button>
            <Button disabled={busy} onPress={() => setReview(null)}>
              Cancel
            </Button>
          </View>
        </Section>
      )}
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
  actions: { flexDirection: "row", alignItems: "center", gap: 8, flexWrap: "wrap" },
});

