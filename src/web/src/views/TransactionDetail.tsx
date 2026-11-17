/**
 * One transaction, found by the id in the explorer's search box.
 *
 * `GET /v1/transactions/{txid}` is an index: it says whether the transaction is
 * still in the mempool or which block mined it, but it does not repeat the
 * body. When the block is still held by this node, the page looks the body up
 * in that block and shows the sender, nonce, fee and payload; a pending
 * transaction has no body route, so the page says only what the pool knows.
 */

import type { ReactNode } from "react";
import { StyleSheet, View } from "react-native";
import { Text } from "react-native-paper";

import { fetchBlock, fetchTransaction } from "../api";
import {
  CodeBlock,
  FailureNotice,
  KeyValue,
  Link,
  Loading,
  Mono,
  Section,
  StatusPill,
} from "../components/Chrome";
import { formatInteger, formatTokenAmount, prettyJson, shortHash } from "../lib/format";
import { tokenForAsset, tokensFor } from "../lib/assets";
import { txId as computeTxId, type TxType } from "../lib/tx";
import { useNode } from "../node";
import { useResource } from "../useResource";
import type { Block, Transaction } from "../types";

export function TransactionDetail({ txid }: { txid: string }): ReactNode {
  const { params } = useNode();
  const tokens = tokensFor(params.data);
  const record = useResource(
    (signal) => fetchTransaction(txid, signal),
    [txid],
    { pollMs: 10000 },
  );

  // The body is optional: a node that has snapshotted the block away still
  // answers the index route, and a pending transaction has no block at all.
  const body = useResource(
    async (signal) => {
      const hash = record.data?.block_hash;
      const digest = record.data?.txid;
      if (hash === null || hash === undefined || digest === undefined) return undefined;
      try {
        return findTransaction((await fetchBlock(hash, signal)).block, digest);
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") throw error;
        return undefined;
      }
    },
    [record.data?.block_hash, record.data?.txid],
  );

  if (record.error !== undefined) return <FailureNotice error={record.error} />;
  if (record.data === undefined) return <Loading />;

  const view = record.data;
  const pending = view.in_mempool;

  return (
    <>
      <View style={styles.titleRow}>
        <View style={styles.titleBlock}>
          <Text variant="headlineSmall">Transaction</Text>
          <Mono copy>{view.txid}</Mono>
        </View>
        <StatusPill label={pending ? "pending" : "in a block"} tone={pending ? "warn" : "good"} />
      </View>

      <Section title="Index" subtitle="where this node last saw the transaction">
        <KeyValue label="Transaction id">
          <Mono copy>{view.txid}</Mono>
        </KeyValue>
        <KeyValue label="Type" mono>
          {view.type}
        </KeyValue>
        <KeyValue label="Network" mono>
          {view.network}
        </KeyValue>
        <KeyValue label="State">
          {pending ? (
            "waiting in the mempool"
          ) : (
            `mined in block ${formatInteger(view.height ?? 0)}`
          )}
        </KeyValue>
        <KeyValue label="Height">
          {view.height === null ? (
            "not in a block yet"
          ) : (
            <Link href={`#/blocks/${view.height}`} mono>
              {formatInteger(view.height)}
            </Link>
          )}
        </KeyValue>
        <KeyValue label="Block">
          {view.block_hash === null ? (
            "not mined yet"
          ) : (
            <Link href={`#/blocks/${view.block_hash}`} mono>
              {view.block_hash}
            </Link>
          )}
        </KeyValue>
      </Section>

      <Section
        title="Body"
        subtitle={
          pending
            ? "the mempool exposes ids, not transaction bodies"
            : "sender, nonce, fee and payload, read from the block that mined it"
        }
      >
        {pending ? (
          <Text variant="bodyMedium" style={styles.muted}>
            This transaction is waiting for a block. The node will show its body here once a block
            includes it.
          </Text>
        ) : body.loading ? (
          <Loading />
        ) : body.data === undefined ? (
          <Text variant="bodyMedium" style={styles.muted}>
            This node no longer holds the block body, so only the index entry above is available.
          </Text>
        ) : (
          <>
            <KeyValue label="From">
              <Link href={`#/accounts/${body.data.from}`} mono>
                {body.data.from}
              </Link>
            </KeyValue>
            <KeyValue label="Nonce" mono>
              {formatInteger(body.data.nonce)}
            </KeyValue>
            <KeyValue label="Fee" mono>
              {formatInteger(body.data.fee)}
            </KeyValue>
            {body.data.type === "transfer" && (
              <KeyValue label="Transfer" mono>
                {(() => {
                  const amount = Number(body.data.payload.amount);
                  const asset =
                    typeof body.data.payload.asset === "string" ? body.data.payload.asset : null;
                  const token = tokenForAsset(tokens, asset);
                  return formatTokenAmount(
                    amount,
                    token?.symbol ?? (asset === null ? "SCCP" : "asset"),
                    token?.decimals ?? 8,
                  );
                })()}
              </KeyValue>
            )}
            {body.data.type === "create_pool" && (
              <KeyValue label="Create pool" mono>
                {(() => {
                  const payload = body.data.payload;
                  const assetA = typeof payload.asset_a === "string" ? payload.asset_a : null;
                  const assetB = typeof payload.asset_b === "string" ? payload.asset_b : null;
                  const tokenA = tokenForAsset(tokens, assetA);
                  const tokenB = tokenForAsset(tokens, assetB);
                  return `${formatTokenAmount(Number(payload.amount_a), tokenA?.symbol ?? "SCCP", tokenA?.decimals ?? 8)} + ${formatTokenAmount(Number(payload.amount_b), tokenB?.symbol ?? "asset", tokenB?.decimals ?? 8)}`;
                })()}
              </KeyValue>
            )}
            {body.data.type === "add_liquidity" && (
              <KeyValue label="Add liquidity" mono>
                {`${formatInteger(Number(body.data.payload.amount_a))} + ${formatInteger(
                  Number(body.data.payload.amount_b),
                )} to pool ${shortHash(String(body.data.payload.pool_id), 8)}`}
              </KeyValue>
            )}
            {body.data.type === "remove_liquidity" && (
              <KeyValue label="Remove liquidity" mono>
                {`${formatInteger(Number(body.data.payload.shares))} shares from pool ${shortHash(
                  String(body.data.payload.pool_id),
                  8,
                )}`}
              </KeyValue>
            )}
            {body.data.type === "swap" && (
              <KeyValue label="Swap" mono>
                {(() => {
                  const payload = body.data.payload;
                  const assetIn = typeof payload.asset_in === "string" ? payload.asset_in : null;
                  const token = tokenForAsset(tokens, assetIn);
                  return `${formatTokenAmount(Number(payload.amount_in), token?.symbol ?? "SCCP", token?.decimals ?? 8)} in pool ${shortHash(String(payload.pool_id), 8)}`;
                })()}
              </KeyValue>
            )}
            {body.data.type === "htlc_lock" && (
              <KeyValue label="HTLC lock" mono>
                {(() => {
                  const payload = body.data.payload;
                  const asset = typeof payload.asset === "string" ? payload.asset : null;
                  const token = tokenForAsset(tokens, asset);
                  return `${formatTokenAmount(
                    Number(payload.amount),
                    token?.symbol ?? "SCCP",
                    token?.decimals ?? 8,
                  )} until height ${formatInteger(Number(payload.timelock_height))} for ${String(payload.beneficiary)}`;
                })()}
              </KeyValue>
            )}
            {body.data.type === "htlc_claim" && (
              <KeyValue label="HTLC claim" mono>
                {`${shortHash(String(body.data.payload.htlc_id), 12)} with preimage ${shortHash(
                  String(body.data.payload.preimage),
                  12,
                )}`}
              </KeyValue>
            )}
            {body.data.type === "htlc_refund" && (
              <KeyValue label="HTLC refund" mono>
                {shortHash(String(body.data.payload.htlc_id), 12)}
              </KeyValue>
            )}
            <KeyValue label="Payload">
              {Object.keys(body.data.payload).length === 0 ? (
                "empty"
              ) : (
                <CodeBlock>{prettyJson(body.data.payload)}</CodeBlock>
              )}
            </KeyValue>
          </>
        )}
      </Section>
    </>
  );
}

/** Find the transaction in a block whose canonical id is `digest`. */
function findTransaction(block: Block, digest: string): Transaction | undefined {
  const candidates =
    block.coinbase === null ? block.transactions : [block.coinbase, ...block.transactions];
  return candidates.find((tx) => transactionHash(tx) === digest);
}

/** `H("sccp/v1/tx", body)` for a block's wire transaction, or `null` if invalid. */
function transactionHash(tx: Transaction): string | null {
  try {
    return computeTxId({
      network: tx.network,
      type: tx.type as TxType,
      from: tx.from,
      nonce: tx.nonce,
      fee: tx.fee,
      payload: tx.payload,
    });
  } catch {
    return null;
  }
}

const styles = StyleSheet.create({
  titleRow: {
    flexDirection: "row",
    alignItems: "flex-start",
    justifyContent: "space-between",
    gap: 12,
    flexWrap: "wrap",
  },
  titleBlock: { gap: 4, flexShrink: 1 },
  muted: { opacity: 0.66 },
});
