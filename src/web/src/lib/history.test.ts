/**
 * The block scan behind the wallet's History tab.
 *
 * `sccp/history` has no endpoint to fake, so the test pins the rules the view
 * depends on: which transactions count as touching an address, what a `swap`
 * row says about both of its legs, and how a range of blocks maps to progress.
 * `fetchBlock` is mocked, so this runs offline.
 */

import { describe, expect, it, vi } from "vitest";

import type { Block, BlockHeader, BlockView, Transaction } from "../types";
import {
  mergeHistory,
  scanHistory,
  scanRange,
  swappedAsset,
  type HistoryEntry,
} from "./history";

vi.mock("../api", () => ({ fetchBlock: vi.fn() }));

import { fetchBlock } from "../api";

const ADDRESS = "sccpt1qowner";
const OTHER = "sccpt1qother";
const THIRD = "sccpt1qthird";
const POOL = "5e".repeat(32);
const C960 = "ab".repeat(32);

const header = { timestamp: 1763200000 } as BlockHeader;

function tx(partial: Partial<Transaction>): Transaction {
  return {
    network: "testnet",
    type: "transfer",
    from: OTHER,
    nonce: 0,
    fee: 1000,
    payload: {},
    signature: null,
    ...partial,
  };
}

function block(height: number, transactions: Transaction[], coinbase: Transaction | null): BlockView {
  const blockValue: Block = { header, coinbase, transactions, proofs: [] };
  return {
    network: "testnet",
    hash: `hash-${height}`,
    parent_hash: `hash-${height - 1}`,
    height,
    chain_work: height,
    main_chain: true,
    block: blockValue,
  };
}

const BLOCKS: Record<string, BlockView> = {
  "10": block(
    10,
    [
      tx({ from: OTHER, nonce: 0, payload: { to: ADDRESS, amount: 250 } }),
      tx({ from: ADDRESS, nonce: 4, payload: { to: OTHER, amount: 100 } }),
      tx({ from: ADDRESS, nonce: 5, type: "register", payload: { bond: 7 } }),
      tx({ from: THIRD, nonce: 0, payload: { to: OTHER, amount: 9 } }),
    ],
    tx({ from: ADDRESS, nonce: 0, fee: 0, payload: { to: ADDRESS, amount: 50 } }),
  ),
  "9": block(9, [tx({ from: OTHER, nonce: 1, payload: { to: THIRD, amount: 1 } })], null),
};

function mockBlocks(blocks: Record<string, BlockView> = BLOCKS): void {
  vi.mocked(fetchBlock).mockImplementation(async (reference) => {
    const value = blocks[String(reference)];
    if (value === undefined) throw new Error(`no block ${reference}`);
    return value;
  });
}

function entry(partial: Partial<HistoryEntry>): HistoryEntry {
  return {
    height: 20,
    blockHash: "hash-20",
    timestamp: 1763200000,
    type: "swap",
    direction: "swap",
    from: ADDRESS,
    to: null,
    amount: 1,
    asset: null,
    fee: 5000,
    nonce: 0,
    txid: "aa",
    swap: null,
    ...partial,
  };
}

describe("scanRange", () => {
  it("clamps to a real chain", () => {
    expect(scanRange(10, 5)).toEqual({ from: 10, to: 5 });
    expect(scanRange(-3, -9)).toEqual({ from: 0, to: 0 });
    expect(scanRange(4, 99)).toEqual({ from: 4, to: 4 });
    expect(scanRange(4.9, 2.2)).toEqual({ from: 4, to: 2 });
  });
});

describe("scanHistory", () => {
  it("keeps only the transactions that touch the address", async () => {
    mockBlocks();
    const progress: [number, number][] = [];
    const result = await scanHistory({
      address: ADDRESS,
      fromHeight: 10,
      toHeight: 9,
      onProgress: (done, total) => progress.push([done, total]),
    });

    expect(result.failures).toBe(0);
    expect(result.entries.map((entry) => entry.direction).sort()).toEqual([
      "in",
      "out",
      "out",
      "reward",
    ]);
    expect(result.entries.find((entry) => entry.direction === "in")?.amount).toBe(250);
    expect(result.entries.find((entry) => entry.direction === "reward")?.amount).toBe(50);
    expect(result.entries.find((entry) => entry.type === "register")?.amount).toBeNull();
    expect(result.entries.every((entry) => entry.txid.length === 64)).toBe(true);
    expect(result.entries[0]?.height).toBe(10);
    expect(progress.at(-1)).toEqual([2, 2]);
  });

  it("counts a block the node will not answer for instead of failing the scan", async () => {
    mockBlocks();
    const result = await scanHistory({ address: ADDRESS, fromHeight: 12, toHeight: 9 });
    expect(result.failures).toBe(2);
    expect(result.entries.length).toBeGreaterThan(0);
  });

  it("stops when the caller aborts", async () => {
    vi.mocked(fetchBlock).mockRejectedValue(new DOMException("aborted", "AbortError"));
    await expect(
      scanHistory({ address: ADDRESS, fromHeight: 10, toHeight: 9 }),
    ).rejects.toThrow(/aborted/);
  });
});

describe("swap rows", () => {
  const SWAP_BLOCKS: Record<string, BlockView> = {
    "20": block(
      20,
      [
        // Native in: the pool pays its token side.
        tx({
          from: ADDRESS,
          nonce: 7,
          type: "swap",
          payload: { pool_id: POOL, amount_in: 10_0000_0000, min_amount_out: 2_450_0000_0000 },
        }),
        // Token in: the pool pays native SCCP, and this signer accepted no floor.
        tx({
          from: ADDRESS,
          nonce: 8,
          type: "swap",
          payload: { pool_id: POOL, asset_in: C960, amount_in: 300, min_amount_out: 0 },
        }),
        // Somebody else's swap touches only that sender: a pool is not an account.
        tx({ from: OTHER, nonce: 2, type: "swap", payload: { pool_id: POOL, amount_in: 5 } }),
      ],
      null,
    ),
  };

  it("keeps one row per swap, with the input and the minimum output", async () => {
    mockBlocks(SWAP_BLOCKS);
    const result = await scanHistory({ address: ADDRESS, fromHeight: 20, toHeight: 20 });

    expect(result.entries.map((row) => row.direction)).toEqual(["swap", "swap"]);
    const nativeIn = result.entries.find((row) => row.nonce === 7);
    expect(nativeIn?.amount).toBe(10_0000_0000);
    expect(nativeIn?.asset).toBeNull();
    expect(nativeIn?.swap).toEqual({ poolId: POOL, minAmountOut: 2_450_0000_0000 });
    const tokenIn = result.entries.find((row) => row.nonce === 8);
    expect(tokenIn?.amount).toBe(300);
    expect(tokenIn?.asset).toBe(C960);
    expect(tokenIn?.swap).toEqual({ poolId: POOL, minAmountOut: 0 });
    expect(result.entries.every((row) => row.txid.length === 64)).toBe(true);
  });

  it("names the received asset from the pool's pair, not from its reserves", () => {
    const pools = new Map([[POOL, [null, C960] as const]]);
    const swapped = (partial: Partial<HistoryEntry>): HistoryEntry =>
      entry({ swap: { poolId: POOL, minAmountOut: 1 }, ...partial });

    expect(swappedAsset(swapped({}), pools)).toBe(C960);
    expect(swappedAsset(swapped({ asset: C960 }), pools)).toBeNull();
    // A pool the node does not list, or a row that is not a swap: nothing to say.
    expect(swappedAsset(swapped({}), new Map())).toBeUndefined();
    expect(swappedAsset(entry({ direction: "in" }), pools)).toBeUndefined();
  });
});

describe("mergeHistory", () => {
  it("de-duplicates on txid and keeps the newest first", () => {
    const older = [
      { height: 5, txid: "aa", nonce: 0 },
      { height: 6, txid: "bb", nonce: 0 },
    ] as never;
    const newer = [
      { height: 6, txid: "bb", nonce: 0 },
      { height: 7, txid: "", nonce: 3 },
      { height: 7, txid: "", nonce: 4 },
    ] as never;
    const merged = mergeHistory(older, newer) as { height: number }[];
    expect(merged).toHaveLength(4);
    expect(merged[0]?.height).toBe(7);
    expect(merged.at(-1)?.height).toBe(5);
  });
});
