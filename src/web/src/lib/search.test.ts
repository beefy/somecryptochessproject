/**
 * The Blocks search box.
 *
 * A block hash and a txid are both 32-byte hex, so only the node can tell them
 * apart. These tests pin the order of the two lookups: block first, transaction
 * second, and never hide a transport failure as "not found".
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    fetchBlock: vi.fn(),
    fetchTransaction: vi.fn(),
  };
});

import { ApiError, fetchBlock, fetchTransaction, NotFoundError } from "../api";
import type { BlockView, TransactionRecord } from "../types";
import { resolveSearchTarget } from "./search";

const BLOCK_HASH = "cd".repeat(32);
const TXID = "ab".repeat(32);

const RECORD: TransactionRecord = {
  txid: TXID,
  network: "devnet",
  type: "transfer",
  height: 7,
  in_mempool: false,
  block_hash: BLOCK_HASH,
};

beforeEach(() => {
  vi.mocked(fetchBlock).mockReset();
  vi.mocked(fetchTransaction).mockReset();
});

describe("resolveSearchTarget", () => {
  it("takes a height straight to the block page", async () => {
    await expect(resolveSearchTarget(" 12 ")).resolves.toEqual({
      name: "block",
      reference: "12",
    });
    expect(fetchBlock).not.toHaveBeenCalled();
    expect(fetchTransaction).not.toHaveBeenCalled();
  });

  it("resolves a block hash before trying the transaction route", async () => {
    vi.mocked(fetchBlock).mockResolvedValue({} as BlockView);
    await expect(resolveSearchTarget(BLOCK_HASH)).resolves.toEqual({
      name: "block",
      reference: BLOCK_HASH,
    });
    expect(fetchTransaction).not.toHaveBeenCalled();
  });

  it("falls back to the transaction route when no block has that hash", async () => {
    vi.mocked(fetchBlock).mockRejectedValue(new NotFoundError("unknown block"));
    vi.mocked(fetchTransaction).mockResolvedValue(RECORD);
    await expect(resolveSearchTarget(TXID)).resolves.toEqual({
      name: "transaction",
      txid: TXID,
    });
    expect(fetchBlock).toHaveBeenCalledWith(TXID, undefined);
    expect(fetchTransaction).toHaveBeenCalledWith(TXID, undefined);
  });

  it("normalises a pasted hex id to the chain's lowercase spelling", async () => {
    const upper = TXID.toUpperCase();
    vi.mocked(fetchBlock).mockRejectedValue(new NotFoundError("unknown block"));
    vi.mocked(fetchTransaction).mockResolvedValue(RECORD);
    await resolveSearchTarget(upper);
    expect(fetchBlock).toHaveBeenCalledWith(TXID, undefined);
    expect(fetchTransaction).toHaveBeenCalledWith(TXID, undefined);
  });

  it("reports a miss when neither route knows the reference", async () => {
    vi.mocked(fetchBlock).mockRejectedValue(new NotFoundError("unknown block"));
    vi.mocked(fetchTransaction).mockRejectedValue(new NotFoundError("unknown transaction"));
    await expect(resolveSearchTarget("not-a-reference")).resolves.toBeNull();
  });

  it("does not turn a node outage into a miss", async () => {
    const offline = new ApiError("cannot reach the node", 0);
    vi.mocked(fetchBlock).mockRejectedValue(offline);
    await expect(resolveSearchTarget(TXID)).rejects.toBe(offline);
    expect(fetchTransaction).not.toHaveBeenCalled();
  });
});
