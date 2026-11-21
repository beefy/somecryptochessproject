/**
 * Account history, reconstructed by scanning blocks.
 *
 * There is no `/v1/history` endpoint: the node keeps account *state*, not the
 * transactions that produced it. What it does expose is `GET /v1/blocks` and one
 * block by height, so the wallet walks backwards from the tip, reads the blocks
 * and keeps the transactions that touch one address. That is the plan in
 * `docs/design/BIG_PICTURE.md` section 4.3, and it is honest about its limits: a scan is
 * a window, not an index, so the view always says which heights it covered.
 *
 * Results are cached in IndexedDB per `network:address`, together with the
 * heights already read, so reopening the tab reads only the blocks that appeared
 * since. A scan reads `concurrency` blocks at a time: enough to be quick on a
 * testnet, few enough not to look like a flood to the node.
 *
 * One row is one transaction, even when a transaction moves coins both ways. A
 * `swap` is that case: one transition spends `amount_in` of one asset and pays
 * out the other, so its row carries both legs. The input is exact; the received
 * side is the minimum the chain enforced, because a block records the bound
 * (`min_amount_out`) and not the fill.
 */

import { fetchBlock } from "../api";
import type { Block, Transaction } from "../types";
import { txId, type TxType } from "./tx";

/**
 * Which side of the ledger the address was on.
 *
 * `swap` is both sides at once, which is why its row names two assets: the DEX
 * settles the input and the output in one transition.
 */
export type HistoryDirection = "in" | "out" | "self" | "reward" | "swap";

/** The pool a swap routed through, and the output floor its signer accepted. */
export interface SwapDetail {
  poolId: string;
  /**
   * `min_amount_out`: the chain rejects a fill below it. It is the one exact
   * number a swap's received leg can be described with, because a block keeps
   * the bound and not the fill.
   */
  minAmountOut: number | null;
}

/** One transaction that touched the account, with the block that holds it. */
export interface HistoryEntry {
  height: number;
  blockHash: string;
  timestamp: number;
  type: string;
  direction: HistoryDirection;
  from: string;
  to: string | null;
  /** Base units, or `null` for a transaction type that moves no coins. */
  amount: number | null;
  /** Asset id for a token transfer; `null` means native ZQWX. */
  asset: string | null;
  fee: number;
  nonce: number;
  /** `H("sccp/v1/tx", body)`, which the explorer and the CLI both use. */
  txid: string;
  /** Set on a `swap` row; `null` for every other type. */
  swap: SwapDetail | null;
}

/** What one scan covered and what it found. */
export interface HistoryScan {
  entries: HistoryEntry[];
  /** Highest block read. */
  scannedFrom: number;
  /** Lowest block read. */
  scannedTo: number;
  /** True once the scan reached genesis; there is nothing older to read. */
  complete: boolean;
  /** Blocks the node could not answer for; they are simply not covered. */
  failures: number;
}

/** The cached view of one account's window. */
export interface CachedHistory {
  address: string;
  network: string;
  scannedFrom: number;
  scannedTo: number;
  complete: boolean;
  entries: HistoryEntry[];
  savedAt: number;
}

const DB_NAME = "sccp-wallet-history";
const DB_VERSION = 2;
const STORE = "accounts";

/** How many entries a cache keeps before dropping the oldest. */
export const HISTORY_LIMIT = 500;

const key = (address: string, network: string): string => `${network}:${address}`;

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const database = request.result;
      // Version 2 starts the cache over. Rows written before it describe a swap
      // as a single unreadable leg, and only a fresh scan of those heights can
      // repair them. The cache is a cache: one window scan refills it.
      if (database.objectStoreNames.contains(STORE)) database.deleteObjectStore(STORE);
      database.createObjectStore(STORE, { keyPath: "id" });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(new Error("cannot open the history database"));
  });
}

async function withStore<T>(
  mode: IDBTransactionMode,
  run: (store: IDBObjectStore) => IDBRequest<T>,
): Promise<T> {
  const database = await openDatabase();
  try {
    return await new Promise<T>((resolve, reject) => {
      const transaction = database.transaction(STORE, mode);
      const request = run(transaction.objectStore(STORE));
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(new Error("the history database rejected the request"));
    });
  } finally {
    database.close();
  }
}

/** The cached window for an account, or `undefined` if it was never scanned. */
export async function loadHistory(
  address: string,
  network: string,
): Promise<CachedHistory | undefined> {
  try {
    const record = await withStore<CachedHistory | undefined>("readonly", (store) =>
      store.get(key(address, network)),
    );
    return record;
  } catch {
    // A browser without IndexedDB still gets a working scan; it just re-reads.
    return undefined;
  }
}

/** Store the merged window for an account. */
export async function saveHistory(record: CachedHistory): Promise<void> {
  try {
    await withStore<IDBValidKey>("readwrite", (store) =>
      store.put({ ...record, id: key(record.address, record.network) }),
    );
  } catch {
    // Same as above: the cache is an optimisation, never the source of truth.
  }
}

export interface ScanOptions {
  address: string;
  fromHeight: number;
  /** Inclusive; clamped to `0`. */
  toHeight: number;
  /** Blocks read in parallel. */
  concurrency?: number;
  onProgress?: (done: number, total: number) => void;
  signal?: AbortSignal;
}

/** The highest and lowest block a scan reads, clamped to a real chain. */
export function scanRange(fromHeight: number, toHeight: number): { from: number; to: number } {
  const from = Math.max(0, Math.trunc(fromHeight));
  const to = Math.max(0, Math.min(Math.trunc(toHeight), from));
  return { from, to };
}

/**
 * Read a range of blocks and keep the transactions that touch `address`.
 *
 * One block that cannot be read increments `failures` rather than failing the
 * scan: a testnet node mid-restart should not throw away the 150 blocks that did
 * answer. An aborted scan throws, so the caller can tell "stopped" from "done".
 */
export async function scanHistory(options: ScanOptions): Promise<HistoryScan> {
  const { from, to } = scanRange(options.fromHeight, options.toHeight);
  const heights: number[] = [];
  for (let height = from; height >= to; height -= 1) heights.push(height);

  const total = heights.length;
  const entries: HistoryEntry[] = [];
  let failures = 0;
  let done = 0;
  let cursor = 0;

  const read = async (height: number): Promise<void> => {
    try {
      const view = await fetchBlock(String(height), options.signal);
      for (const tx of view.block.transactions) {
        const entry = entryFor(tx, view.block, view.hash, height, options.address);
        if (entry !== null) entries.push(entry);
      }
      if (view.block.coinbase !== null) {
        const entry = entryFor(
          view.block.coinbase,
          view.block,
          view.hash,
          height,
          options.address,
          true,
        );
        if (entry !== null) entries.push(entry);
      }
    } catch (failure) {
      if (failure instanceof DOMException && failure.name === "AbortError") throw failure;
      failures += 1;
    } finally {
      done += 1;
      options.onProgress?.(done, total);
    }
  };

  const workers = Array.from(
    { length: Math.max(1, Math.min(options.concurrency ?? 4, heights.length)) },
    async () => {
      for (;;) {
        const index = cursor;
        cursor += 1;
        const height = heights[index];
        if (height === undefined) return;
        await read(height);
      }
    },
  );
  await Promise.all(workers);

  entries.sort((left, right) => right.height - left.height || left.nonce - right.nonce);
  return { entries, scannedFrom: from, scannedTo: to, complete: to === 0, failures };
}



function payloadText(tx: Transaction, field: string): string | null {
  const value = tx.payload[field];
  return typeof value === "string" ? value : null;
}

/** A payload field that has to be a safe integer, or `null` when it is not one. */
function payloadInt(tx: Transaction, field: string): number | null {
  const value = tx.payload[field];
  return typeof value === "number" && Number.isSafeInteger(value) ? value : null;
}

/** The txid of a wire transaction, or `""` if its payload has no canonical form. */
function safeTxId(tx: Transaction): string {
  try {
    return txId({
      network: tx.network,
      type: tx.type as TxType,
      from: tx.from,
      nonce: tx.nonce,
      fee: tx.fee,
      payload: tx.payload,
    });
  } catch {
    return "";
  }
}

function entryFor(
  tx: Transaction,
  block: Block,
  blockHash: string,
  height: number,
  address: string,
  coinbase = false,
): HistoryEntry | null {
  const to = payloadText(tx, "to");
  let direction: HistoryDirection;
  let amount = payloadInt(tx, "amount");
  let asset = payloadText(tx, "asset");
  let swap: SwapDetail | null = null;
  if (coinbase) {
    if (tx.from !== address) return null;
    direction = "reward";
  } else if (tx.type === "swap") {
    // A swap names its two sides `amount_in`/`asset_in` and touches nobody but
    // its sender: the pool is not an account, so this row has no counterparty
    // address, and `min_amount_out` is all the block says about the payout.
    if (tx.from !== address) return null;
    direction = "swap";
    amount = payloadInt(tx, "amount_in");
    asset = payloadText(tx, "asset_in");
    const poolId = payloadText(tx, "pool_id");
    if (poolId !== null) swap = { poolId, minAmountOut: payloadInt(tx, "min_amount_out") };
  } else if (tx.from === address) {
    direction = to === address ? "self" : "out";
  } else if (to === address) {
    direction = "in";
  } else {
    return null;
  }
  return {
    height,
    blockHash,
    timestamp: block.header.timestamp,
    type: coinbase ? "coinbase" : tx.type,
    direction,
    from: tx.from,
    to,
    amount,
    asset,
    fee: tx.fee,
    nonce: tx.nonce,
    txid: safeTxId(tx),
    swap,
  };
}

/** Merge windows of entries, de-duplicated by txid, newest first, bounded. */
export function mergeHistory(...groups: HistoryEntry[][]): HistoryEntry[] {
  const seen = new Map<string, HistoryEntry>();
  for (const entry of groups.flat()) {
    const id = entry.txid === "" ? `${entry.height}:${entry.from}:${entry.nonce}` : entry.txid;
    if (!seen.has(id)) seen.set(id, entry);
  }
  return [...seen.values()]
    .sort((left, right) => right.height - left.height || left.nonce - right.nonce)
    .slice(0, HISTORY_LIMIT);
}

/** A pool's two assets, in the order consensus stores them; `null` is native ZQWX. */
export type PoolPair = readonly [string | null, string | null];

/**
 * The asset a `swap` row received, or `undefined` when it cannot be named.
 *
 * Today's pools answer for a swap mined long ago: `pool_id` hashes the pair, and
 * a pair is fixed when the pool is created — only the reserves move. The amount is
 * not recoverable the same way, so the row falls back to `min_amount_out`.
 */
export function swappedAsset(
  entry: HistoryEntry,
  pools: ReadonlyMap<string, PoolPair>,
): string | null | undefined {
  if (entry.swap === null) return undefined;
  const pair = pools.get(entry.swap.poolId);
  if (pair === undefined) return undefined;
  const spent = entry.asset ?? "";
  const [assetA, assetB] = pair;
  if ((assetA ?? "") === spent) return assetB;
  if ((assetB ?? "") === spent) return assetA;
  return undefined;
}

