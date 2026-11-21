import { useState, type ReactNode } from "react";
import { View } from "react-native";
import { Button, HelperText, Text, TextInput } from "react-native-paper";

import { fetchHtlcs, fetchOrder, submitOrder, submitTransaction } from "../../api";
import { EmptyState, KeyValue, Section, StatusPill } from "../../components/Chrome";
import { useNotice } from "../../components/Notices";
import {
  formatWei,
  orderPrice,
  parseEthToWei,
  signBridgeOrder,
} from "../../lib/bridge";
import {
  claimEth,
  connectEthAccount,
  ensureEthChain,
  verifyEthLock,
} from "../../lib/eth";
import {
  formatInteger,
  formatTokenAmount,
  parseTokenAmount,
  shortAddress,
  shortHash,
} from "../../lib/format";
import { hybridPublicKeyHex } from "../../lib/keys";
import {
  buildHtlcLock,
  buildHtlcRefund,
  signTransaction,
  toWireTransaction,
} from "../../lib/tx";
import type { OrderView } from "../../types";
import { useResource } from "../../useResource";
import { EthLine, bridgeStyles as styles, ConfigNotice } from "./common";
import type { BridgeContext } from "./types";

export function SellPanel(context: BridgeContext): ReactNode {
  const {
    orders,
    bridge,
    address,
    keypair,
    account,
    params,
    status,
    ethAccount,
    connectEth,
    network,
  } = context;
  const notify = useNotice();
  const [amount, setAmount] = useState("");
  const [price, setPrice] = useState("");
  const [expiryHours, setExpiryHours] = useState("24");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const mine = orders.filter((order) => order.maker_sccp === address);

  const publish = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      if (!bridge.configured || bridge.eth_htlc_contract === null) {
        throw new Error(`this node has no ${bridge.eth_chain_name} HTLC contract configured`);
      }
      const sccp = parseTokenAmount(amount, "ZQWX", 8);
      if (!Number.isSafeInteger(sccp) || sccp <= 0) throw new Error("enter a positive ZQWX amount");
      if (sccp + params.min_fee > account.balance) {
        throw new Error(
          `the wallet holds ${formatTokenAmount(account.balance, "ZQWX")}; the order needs the amount plus a fee`,
        );
      }
      const want = parseEthToWei(price);
      const hours = Number.parseFloat(expiryHours);
      if (!Number.isFinite(hours) || hours <= 0) throw new Error("enter a positive expiry");
      const eth = (await connectEth()) ?? (await connectEthAccount());
      if (eth === null) throw new Error("connect an Ethereum wallet");
      await ensureEthChain({
        chainId: bridge.eth_chain_id,
        chainName: bridge.eth_chain_name,
        rpcUrl: bridge.eth_rpc_url,
        explorerUrl: bridge.eth_explorer_url,
      });
      const order = signBridgeOrder(keypair, {
        network,
        makerSccp: address,
        makerEth: eth,
        giveAmount: sccp,
        wantAmount: want.toString(),
        minSccpTimelock: status.height + 1,
        expiry: Math.floor(Date.now() / 1000) + Math.ceil(hours * 3600),
        nonce: Date.now(),
      });
      const accepted = await submitOrder(order);
      notify(`order ${shortHash(accepted.order_id, 8)} published`);
      setAmount("");
      setPrice("");
      context.reload();
    } catch (failure) {
      setError(failure instanceof Error ? failure : new Error(String(failure)));
    } finally {
      setBusy(false);
    }
  };

  return (
    <View style={styles.root}>
      <Section
        title="Sell ZQWX for ETH"
        subtitle="publish a signed order; the taker locks ETH first"
        action={<StatusPill label={bridge.eth_chain_name} tone="info" />}
      >
        <TextInput
          label="ZQWX to sell"
          mode="outlined"
          value={amount}
          onChangeText={setAmount}
          keyboardType="decimal-pad"
          right={<TextInput.Affix text="ZQWX" />}
        />
        <TextInput
          label="ETH to receive"
          mode="outlined"
          value={price}
          onChangeText={setPrice}
          keyboardType="decimal-pad"
          right={<TextInput.Affix text="ETH" />}
        />
        <TextInput
          label="Order expiry (hours)"
          mode="outlined"
          value={expiryHours}
          onChangeText={setExpiryHours}
          keyboardType="decimal-pad"
        />
        <EthLine bridge={bridge} account={ethAccount} onConnect={connectEth} />
        <ConfigNotice bridge={bridge} />
        <HelperText type="info" visible>
          The order is whole-order and signed with the wallet's hybrid key. Your ETH address is bound
          into the signature, so nobody can redirect the ETH leg. Nothing is escrowed until a taker
          locks ETH and you verify that lock.
        </HelperText>
        {error !== null && (
          <HelperText type="error" visible>
            {error.message}
          </HelperText>
        )}
        <Button
          mode="contained"
          loading={busy}
          disabled={busy || !bridge.configured}
          onPress={() => void publish()}
        >
          Publish sell order
        </Button>
      </Section>
      <Section title="My orders" subtitle="takes and the two legs">
        {mine.length === 0 ? (
          <EmptyState>No open orders from this wallet.</EmptyState>
        ) : (
          mine.map((order) => <MakerOrder key={order.order_id} context={context} orderId={order.order_id} />)
        )}
      </Section>
    </View>
  );
}

function MakerOrder({ context, orderId }: { context: BridgeContext; orderId: string }): ReactNode {
  const { bridge, address, keypair, params, status, network, account, ethAccount, connectEth } =
    context;
  const notify = useNotice();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const [ethLockId, setEthLockId] = useState<string | null>(null);
  const [claimSent, setClaimSent] = useState(false);
  const detail = useResource((signal) => fetchOrder(orderId, signal), [orderId], { pollMs: 5000 });
  const order: OrderView | undefined = detail.data?.order;
  const take = detail.data?.take ?? null;
  const hashlock = take?.hashlock ?? null;
  const htlcs = useResource(
    (signal) => (hashlock === null ? Promise.resolve(undefined) : fetchHtlcs({ hashlock }, signal)),
    [hashlock],
    { pollMs: 5000 },
  );
  const record = htlcs.data?.htlcs.find(
    (candidate) => candidate.sender === address && candidate.hashlock === hashlock,
  );
  const claimFinal =
    record?.status === "claimed" &&
    record.closed_at_height !== null &&
    status.height >= record.closed_at_height + params.reorg_depth;

  if (detail.error !== undefined) {
    return (
      <View style={styles.block}>
        <Text variant="bodyMedium">Order {shortHash(orderId, 8)} is no longer available.</Text>
      </View>
    );
  }
  if (order === undefined) return <Text variant="bodySmall">Loading order…</Text>;

  const contract = bridge.eth_htlc_contract;
  const verify = async (): Promise<string> => {
    if (contract === null) throw new Error("the bridge contract is not configured");
    if (take === null) throw new Error("the order has no take yet");
    if (ethAccount === null) await connectEth();
    await ensureEthChain({
      chainId: bridge.eth_chain_id,
      chainName: bridge.eth_chain_name,
      rpcUrl: bridge.eth_rpc_url,
      explorerUrl: bridge.eth_explorer_url,
    });
    const id = await verifyEthLock(take.eth_tx_hash, contract, {
      depositor: take.taker_eth,
      hashlock: take.hashlock,
      timelock: take.eth_timelock,
      beneficiary: order.maker_eth,
      value: BigInt(order.want_amount),
      confirmations: bridge.eth_confirmations,
    });
    setEthLockId(id);
    return id;
  };

  const lockSccp = async (): Promise<void> => {
    if (take === null) return;
    setBusy(true);
    setError(null);
    try {
      await verify();
      const nowSeconds = Math.floor(Date.now() / 1000);
      const blocksUntilEth = Math.floor(
        Math.max(0, take.eth_timelock - nowSeconds) / params.target_block_seconds,
      );
      const safeMax = status.height + blocksUntilEth - bridge.sccp_margin;
      if (take.sccp_timelock_height <= status.height) {
        throw new Error("the take's SCCP timelock has already passed; ask the taker to refund");
      }
      if (take.sccp_timelock_height > safeMax) {
        throw new Error(
          `the take's SCCP timelock is not safely before the ETH timelock (max ${safeMax})`,
        );
      }
      if (account.balance < order.give_amount + params.min_fee) {
        throw new Error(
          `the wallet holds ${formatTokenAmount(account.balance, "ZQWX")}; locking needs the amount plus a fee`,
        );
      }
      const signed = signTransaction(
        keypair,
        buildHtlcLock({
          network,
          sender: address,
          nonce: context.nonce,
          fee: params.min_fee,
          hashlock: take.hashlock,
          timelockHeight: take.sccp_timelock_height,
          beneficiary: take.taker_sccp,
          amount: order.give_amount,
          publicKeyHex: hybridPublicKeyHex(keypair),
        }),
      );
      await submitTransaction(toWireTransaction(signed));
      context.setPendingNonce(signed.nonce + 1);
      notify("ZQWX locked for the taker");
      context.reload();
      htlcs.reload();
      detail.reload();
    } catch (failure) {
      setError(failure instanceof Error ? failure : new Error(String(failure)));
    } finally {
      setBusy(false);
    }
  };


  const claim = async (): Promise<void> => {
    if (record?.preimage === null || record?.preimage === undefined || contract === null) return;
    setBusy(true);
    setError(null);
    try {
      if (!claimFinal) {
        throw new Error("the ZQWX claim is not final yet; wait for more confirmations");
      }
      const id = ethLockId ?? (await verify());
      await claimEth(contract, id, record.preimage, bridge.eth_confirmations);
      setClaimSent(true);
      notify("ETH claimed");
      context.reload();
    } catch (failure) {
      setClaimSent(false);
      setError(failure instanceof Error ? failure : new Error(String(failure)));
    } finally {
      setBusy(false);
    }
  };

  const refundSccp = async (): Promise<void> => {
    if (record === undefined) return;
    setBusy(true);
    setError(null);
    try {
      if (status.height < record.timelock_height) {
        throw new Error(`the SCCP timelock is height ${record.timelock_height}`);
      }
      const signed = signTransaction(
        keypair,
        buildHtlcRefund({
          network,
          sender: address,
          nonce: context.nonce,
          fee: params.min_fee,
          htlcId: record.htlc_id,
          publicKeyHex: hybridPublicKeyHex(keypair),
        }),
      );
      await submitTransaction(toWireTransaction(signed));
      context.setPendingNonce(signed.nonce + 1);
      notify("ZQWX refunded");
      context.reload();
      htlcs.reload();
    } catch (failure) {
      setError(failure instanceof Error ? failure : new Error(String(failure)));
    } finally {
      setBusy(false);
    }
  };

  return (
    <View style={styles.block}>
      <Text variant="titleSmall">
        {formatTokenAmount(order.give_amount, "ZQWX")} for {formatWei(order.want_amount, 6)} ETH
      </Text>
      <Text variant="bodySmall" style={styles.muted}>
        {orderPrice(order)} ETH / ZQWX · {take === null ? "waiting for a taker" : "taken"} ·{" "}
        {shortHash(order.order_id, 8)}
      </Text>
      {take !== null && (
        <>
          <KeyValue label="Taker" mono>
            {shortAddress(take.taker_sccp)}
          </KeyValue>
          <KeyValue label="ETH lock" mono>
            {shortHash(take.eth_tx_hash, 12)}
          </KeyValue>
          <KeyValue label="ETH timelock">
            {new Date(take.eth_timelock * 1000).toLocaleString()}
          </KeyValue>
          <KeyValue label="SCCP timelock">height {formatInteger(take.sccp_timelock_height)}</KeyValue>
        </>
      )}
      {record !== undefined && (
        <>
          <KeyValue label="ZQWX lock" mono>
            {shortHash(record.htlc_id, 12)}
          </KeyValue>
          <KeyValue label="ZQWX status">{record.status}</KeyValue>
        </>
      )}
      {claimSent && (
        <HelperText type="info" visible>
          ETH claim submitted. The contract closes the lock on success.
        </HelperText>
      )}
      {record?.status === "claimed" && (
        <HelperText type="info" visible>
          {claimFinal
            ? "The taker revealed the preimage. Claim the ETH leg with it."
            : `The taker's ZQWX claim needs ${params.reorg_depth} more block(s) before the preimage is final.`}
        </HelperText>
      )}
      {record?.status === "refunded" && (
        <HelperText type="error" visible>
          ZQWX was refunded after the timelock; this order is finished.
        </HelperText>
      )}
      {error !== null && (
        <HelperText type="error" visible>
          {error.message}
        </HelperText>
      )}
      <View style={styles.actions}>
        {take !== null && record === undefined && (
          <Button mode="contained" loading={busy} disabled={busy} onPress={() => void lockSccp()}>
            Verify ETH lock and lock ZQWX
          </Button>
        )}
        {record?.status === "claimed" && (
          <Button
            mode="contained"
            loading={busy}
            disabled={busy || !claimFinal || claimSent}
            onPress={() => void claim()}
          >
            Claim ETH with preimage
          </Button>
        )}
        {record?.status === "locked" && status.height >= record.timelock_height && (
          <Button mode="outlined" loading={busy} disabled={busy} onPress={() => void refundSccp()}>
            Refund ZQWX
          </Button>
        )}
      </View>
    </View>
  );
}

