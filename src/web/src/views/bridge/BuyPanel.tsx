import { useEffect, useRef, useState, type ReactNode } from "react";
import { ScrollView, View } from "react-native";
import { Button, HelperText, SegmentedButtons, Text } from "react-native-paper";

import { fetchHtlcs, fetchOrder, submitTake, submitTransaction } from "../../api";
import { EmptyState, KeyValue, Section, StatusPill } from "../../components/Chrome";
import { useNotice } from "../../components/Notices";
import {
  formatWei,
  hashlockOf,
  orderPrice,
  randomSecret,
  sccpTimelockHeight,
  signBridgeTake,
} from "../../lib/bridge";
import { forgetSwap, loadSwaps, saveSwap, swapFor, type LocalSwap } from "../../lib/bridgeStore";
import {
  connectEthAccount,
  ensureEthChain,
  lockEth,
  refundEth,
} from "../../lib/eth";
import { formatInteger, formatTokenAmount, shortAddress, shortHash } from "../../lib/format";
import { hybridPublicKeyHex } from "../../lib/keys";
import { buildHtlcClaim, signTransaction, toWireTransaction } from "../../lib/tx";
import { useNow, useResource } from "../../useResource";
import { EthLine, bridgeStyles as styles, ConfigNotice } from "./common";
import type { BridgeContext } from "./types";

export function BuyPanel(context: BridgeContext): ReactNode {
  const {
    orders,
    bridge,
    address,
    keypair,
    params,
    status,
    ethAccount,
    connectEth,
    network,
  } = context;
  const notify = useNotice();
  const [selectedId, setSelectedId] = useState(() => orders[0]?.order_id ?? "");
  const [local, setLocal] = useState<LocalSwap | undefined>(() =>
    orders[0] === undefined ? undefined : swapFor(orders[0].order_id),
  );
  const [duration, setDuration] = useState("21600");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const [claimSent, setClaimSent] = useState(false);
  const now = useNow(5000);
  const recovered = useRef(false);

  useEffect(() => {
    if (orders.length === 0) {
      setSelectedId("");
      return;
    }
    if (!recovered.current) {
      recovered.current = true;
      const pending = Object.values(loadSwaps()).find(
        (swap) =>
          swap.role === "buy" &&
          swap.takerSccp === address &&
          orders.some((order) => order.order_id === swap.orderId),
      );
      if (pending !== undefined) {
        setSelectedId(pending.orderId);
        setLocal(pending);
        return;
      }
    }
    if (!orders.some((order) => order.order_id === selectedId)) {
      const first = orders[0]?.order_id ?? "";
      setSelectedId(first);
      setLocal(swapFor(first));
    }
  }, [orders, selectedId]);

  const detail = useResource(
    (signal) => (selectedId === "" ? Promise.resolve(undefined) : fetchOrder(selectedId, signal)),
    [selectedId],
    { pollMs: 5000 },
  );
  const order = detail.data?.order ?? orders.find((candidate) => candidate.order_id === selectedId);
  const take = detail.data?.take ?? null;
  const hashlock = local?.hashlock ?? take?.hashlock ?? null;
  const htlcs = useResource(
    (signal) => (hashlock === null ? Promise.resolve(undefined) : fetchHtlcs({ hashlock }, signal)),
    [hashlock],
    { pollMs: 5000 },
  );
  const record = htlcs.data?.htlcs.find(
    (candidate) =>
      candidate.beneficiary === address &&
      candidate.sender === order?.maker_sccp &&
      candidate.asset === null,
  );

  useEffect(() => {
    if (local !== undefined && record?.status === "claimed") {
      forgetSwap(local.orderId);
      setLocal(undefined);
      setClaimSent(false);
    }
  }, [local, record?.status]);

  if (orders.length === 0) {
    return (
      <Section title="Buy ZQWX with ETH" subtitle="open sell orders from ZQWX holders">
        <EmptyState>
          No one is selling ZQWX right now. A seller creates a signed order on the Sell tab; the
          order appears here for anyone to take.
        </EmptyState>
      </Section>
    );
  }
  if (order === undefined) return <Section title="Buy ZQWX with ETH">Loading order…</Section>;

  const choose = (orderId: string): void => {
    setSelectedId(orderId);
    setLocal(swapFor(orderId));
    setClaimSent(false);
    setError(null);
  };

  const ensureReady = async (): Promise<{ eth: string; contract: string }> => {
    if (!bridge.configured || bridge.eth_htlc_contract === null) {
      throw new Error(`this node has no ${bridge.eth_chain_name} HTLC contract configured`);
    }
    const eth = (await connectEth()) ?? (await connectEthAccount());
    if (eth === null) throw new Error("connect an Ethereum wallet");
    await ensureEthChain({
      chainId: bridge.eth_chain_id,
      chainName: bridge.eth_chain_name,
      rpcUrl: bridge.eth_rpc_url,
      explorerUrl: bridge.eth_explorer_url,
    });
    return { eth, contract: bridge.eth_htlc_contract };
  };

  const publishTake = async (swap: LocalSwap, eth: string): Promise<void> => {
    if (swap.orderId !== order.order_id) {
      throw new Error("this local swap belongs to a different order");
    }
    if (
      swap.ethTxHash === undefined ||
      swap.ethTimelock === undefined ||
      swap.sccpTimelockHeight === undefined
    ) {
      throw new Error("this local swap is missing its ETH lock details; refund it after the timelock");
    }
    await submitTake(
      order.order_id,
      signBridgeTake(keypair, {
        network,
        orderId: order.order_id,
        takerSccp: address,
        takerEth: eth,
        hashlock: swap.hashlock,
        ethTimelock: swap.ethTimelock,
        sccpTimelockHeight: swap.sccpTimelockHeight,
        ethTxHash: swap.ethTxHash,
        nonce: Date.now(),
      }),
    );
    notify("take published; waiting for the maker's SCCP leg");
    detail.reload();
    htlcs.reload();
    context.reload();
  };

  const lockAndTake = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      const ready = await ensureReady();
      const preimage = local?.preimage ?? randomSecret();
      const lockHash = hashlockOf(preimage);
      const ethSeconds = Number.parseInt(duration, 10);
      const ethTimelock = Math.floor(now / 1000) + ethSeconds;
      const sccpHeight = sccpTimelockHeight(
        status.height,
        ethSeconds,
        params.target_block_seconds,
        bridge.sccp_margin,
        order.min_sccp_timelock,
      );
      const locked = await lockEth(
        ready.contract,
        lockHash,
        ethTimelock,
        order.maker_eth,
        BigInt(order.want_amount),
        bridge.eth_confirmations,
      );
      if (locked.lockId === null) throw new Error("the ETH lock emitted no lock id");
      const next: LocalSwap = {
        role: "buy",
        orderId: order.order_id,
        takerSccp: address,
        hashlock: lockHash,
        preimage,
        ethTxHash: locked.txHash,
        ethLockId: locked.lockId,
        ethTimelock,
        sccpTimelockHeight: sccpHeight,
        createdAt: Date.now(),
      };
      saveSwap(next);
      setLocal(next);
      await publishTake(next, ready.eth);
      notify("ETH locked and the take was published; waiting for the maker's SCCP leg");
    } catch (failure) {
      setError(failure instanceof Error ? failure : new Error(String(failure)));
    } finally {
      setBusy(false);
    }
  };

  const retryTake = async (): Promise<void> => {
    if (local === undefined) return;
    setBusy(true);
    setError(null);
    try {
      const ready = await ensureReady();
      await publishTake(local, ready.eth);
    } catch (failure) {
      setError(failure instanceof Error ? failure : new Error(String(failure)));
    } finally {
      setBusy(false);
    }
  };

  const claimSccp = async (): Promise<void> => {
    if (local?.preimage === undefined || record === undefined) return;
    setBusy(true);
    setError(null);
    try {
      const signed = signTransaction(
        keypair,
        buildHtlcClaim({
          network,
          sender: address,
          nonce: context.nonce,
          fee: params.min_fee,
          htlcId: record.htlc_id,
          preimage: local.preimage,
          publicKeyHex: hybridPublicKeyHex(keypair),
        }),
      );
      await submitTransaction(toWireTransaction(signed));
      context.setPendingNonce(signed.nonce + 1);
      setClaimSent(true);
      notify("ZQWX claim submitted. The maker can claim the ETH once it is mined.");
      context.reload();
      htlcs.reload();
    } catch (failure) {
      setClaimSent(false);
      setError(failure instanceof Error ? failure : new Error(String(failure)));
    } finally {
      setBusy(false);
    }
  };

  const reclaimEth = async (): Promise<void> => {
    if (local?.ethLockId === undefined || local.ethTimelock === undefined) return;
    setBusy(true);
    setError(null);
    try {
      const ready = await ensureReady();
      if (Math.floor(now / 1000) < local.ethTimelock) {
        throw new Error("the ETH timelock has not passed yet");
      }
      await refundEth(ready.contract, local.ethLockId, bridge.eth_confirmations);
      forgetSwap(order.order_id);
      setLocal(undefined);
      notify("ETH refunded from the expired lock");
      context.reload();
    } catch (failure) {
      setError(failure instanceof Error ? failure : new Error(String(failure)));
    } finally {
      setBusy(false);
    }
  };


  const recordMatches =
    record === undefined ||
    (record.amount >= order.give_amount &&
      record.timelock_height >= (take?.sccp_timelock_height ?? 0));
  const lockConfirmations =
    record === undefined ? 0 : Math.max(0, status.height - record.created_at_height + 1);
  const lockFinal = record !== undefined && lockConfirmations >= params.reorg_depth + 1;
  const canClaim =
    local !== undefined &&
    record !== undefined &&
    record.status === "locked" &&
    recordMatches &&
    lockFinal &&
    !claimSent;
  const expired = local?.ethTimelock !== undefined && now / 1000 >= local.ethTimelock;

  return (
    <Section
      title="Buy ZQWX with ETH"
      subtitle="lock ETH first; the maker's SCCP leg is the second leg"
      action={<StatusPill label={bridge.eth_chain_name} tone="info" />}
    >
      <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.pills}>
        {orders.map((candidate) => (
          <Button
            key={candidate.order_id}
            compact
            mode={candidate.order_id === selectedId ? "contained" : "outlined"}
            onPress={() => choose(candidate.order_id)}
          >
            {formatTokenAmount(candidate.give_amount, "ZQWX")}
          </Button>
        ))}
      </ScrollView>
      <KeyValue label="You pay">{formatWei(order.want_amount, 8)} ETH</KeyValue>
      <KeyValue label="You receive">{formatTokenAmount(order.give_amount, "ZQWX")}</KeyValue>
      <KeyValue label="Price">{orderPrice(order)} ETH / ZQWX</KeyValue>
      <KeyValue label="Maker" mono>
        {shortAddress(order.maker_sccp)}
      </KeyValue>
      <KeyValue label="Order expires">{new Date(order.expiry * 1000).toLocaleString()}</KeyValue>
      <EthLine bridge={bridge} account={ethAccount} onConnect={connectEth} />
      <ConfigNotice bridge={bridge} />
      <Text variant="bodySmall" style={styles.muted}>
        ETH lock duration
      </Text>
      <SegmentedButtons
        value={duration}
        onValueChange={setDuration}
        buttons={[
          { value: "21600", label: "6 hours" },
          { value: "43200", label: "12 hours" },
          { value: "86400", label: "24 hours" },
        ]}
      />
      <HelperText type="info" visible>
        The ETH leg opens with a 32-byte secret generated here. It stays in this browser until the
        ZQWX claim reveals it; if the maker never locks ZQWX, refund the ETH after the timelock.
      </HelperText>
      {take !== null && (
        <KeyValue label="Taker's ETH transaction" mono>
          {shortHash(take.eth_tx_hash, 12)}
        </KeyValue>
      )}
      {record !== undefined && (
        <>
          <KeyValue label="ZQWX lock" mono>
            {shortHash(record.htlc_id, 12)}
          </KeyValue>
          <KeyValue label="SCCP timelock">height {formatInteger(record.timelock_height)}</KeyValue>
          <KeyValue label="ZQWX status">{record.status}</KeyValue>
        </>
      )}
      {local !== undefined && take === null && (
        <HelperText type="info" visible>
          ETH is locked and the take is pending in the node&apos;s order book. Waiting for the node
          to report the maker&apos;s ZQWX lock.
        </HelperText>
      )}
      {local !== undefined && take !== null && record === undefined && (
        <HelperText type="info" visible>
          Waiting for the maker to lock ZQWX for hashlock {shortHash(local.hashlock, 8)}.
        </HelperText>
      )}
      {claimSent && (
        <HelperText type="info" visible>
          Claim submitted. Keep this tab open until the SCCP block includes it; the secret is
          still saved here in case the transaction is dropped.
        </HelperText>
      )}
      {record !== undefined && !recordMatches && (
        <HelperText type="error" visible>
          The maker locked the wrong amount or a timelock shorter than the take. Do not claim;
          refund the ETH leg after its timelock.
        </HelperText>
      )}
      {record?.status === "locked" && local === undefined && (
        <HelperText type="error" visible>
          The ZQWX lock exists, but this browser does not hold the swap secret. Open the browser
          that took the order; do not reveal the secret to anyone else.
        </HelperText>
      )}
      {record?.status === "locked" && !lockFinal && (
        <HelperText type="info" visible>
          Waiting for the ZQWX lock to reach {params.reorg_depth + 1} confirmations (
          {formatInteger(lockConfirmations)}/{params.reorg_depth + 1}); do not claim before it
          is final.
        </HelperText>
      )}
      {record?.status === "claimed" && (
        <HelperText type="info" visible>
          ZQWX claimed at height {formatInteger(record.closed_at_height)}. The maker can now claim
          the ETH with the public preimage.
        </HelperText>
      )}
      {record?.status === "refunded" && (
        <HelperText type="error" visible>
          The maker refunded ZQWX after the timelock. Refund the ETH leg once its timelock passes.
        </HelperText>
      )}
      {error !== null && (
        <HelperText type="error" visible>
          {error.message}
        </HelperText>
      )}
      <View style={styles.actions}>
        {local === undefined && take === null && (
          <Button
            mode="contained"
            loading={busy}
            disabled={busy || !bridge.configured}
            onPress={() => void lockAndTake()}
          >
            Lock ETH and take order
          </Button>
        )}
        {local !== undefined && take === null && (
          <Button mode="contained" loading={busy} disabled={busy} onPress={() => void retryTake()}>
            Publish pending take
          </Button>
        )}
        {canClaim && (
          <Button mode="contained" loading={busy} disabled={busy} onPress={() => void claimSccp()}>
            Claim ZQWX and reveal secret
          </Button>
        )}
        {local !== undefined && expired && record === undefined && (
          <Button mode="outlined" loading={busy} disabled={busy} onPress={() => void reclaimEth()}>
            Refund expired ETH
          </Button>
        )}
      </View>
    </Section>
  );
}

