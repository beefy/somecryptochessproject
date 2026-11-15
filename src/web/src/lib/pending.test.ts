/**
 * Pending rows: what the wallet remembers between "the node accepted it" and
 * "a block includes it". The rules that matter are the nonce comparison, the
 * dedupe, and that a cleared browser reads as empty rather than as an error.
 */

import { beforeEach, describe, expect, it } from "vitest";

import {
  PENDING_TTL_MS,
  addPending,
  parsePending,
  pendingFor,
  prunePending,
  readPending,
  removePending,
  writePending,
  type PendingTransaction,
} from "./pending";

const ADDRESS = "sccpt1qalice";
const NETWORK = "testnet";

const FIRST: PendingTransaction = {
  txid: "aa".repeat(32),
  to: "sccpt1qbob",
  amount: 5_000_000_000,
  fee: 1_000,
  nonce: 7,
  submittedAt: 1_700_000_000_000,
};

const SECOND: PendingTransaction = { ...FIRST, txid: "bb".repeat(32), nonce: 8 };

function memoryStorage(): Storage {
  const items = new Map<string, string>();
  return {
    get length() {
      return items.size;
    },
    clear: () => items.clear(),
    getItem: (key: string) => items.get(key) ?? null,
    key: (index: number) => [...items.keys()][index] ?? null,
    removeItem: (key: string) => {
      items.delete(key);
    },
    setItem: (key: string, value: string) => {
      items.set(key, value);
    },
  } as Storage;
}

beforeEach(() => {
  Object.defineProperty(globalThis, "localStorage", {
    value: memoryStorage(),
    configurable: true,
  });
});

describe("addPending", () => {
  it("orders by nonce and replaces a resent txid", () => {
    const added = addPending(addPending([], SECOND), FIRST);
    expect(added.map((entry) => entry.nonce)).toEqual([7, 8]);
    const replaced = addPending(added, { ...FIRST, to: "sccpt1qcarol" });
    expect(replaced).toHaveLength(2);
    expect(replaced[0]?.to).toBe("sccpt1qcarol");
  });
});

describe("prunePending", () => {
  it("drops rows the committed nonce has passed, and keeps the next one", () => {
    const now = FIRST.submittedAt + 1000;
    expect(prunePending([FIRST, SECOND], 8, now)).toEqual([SECOND]);
    expect(prunePending([FIRST, SECOND], 7, now)).toEqual([FIRST, SECOND]);
    expect(prunePending([FIRST, SECOND], 9, now)).toEqual([]);
  });

  it("drops rows older than the TTL", () => {
    const now = FIRST.submittedAt + PENDING_TTL_MS + 1;
    expect(prunePending([FIRST], 0, now)).toEqual([]);
  });
});

describe("the stored book", () => {
  it("reads junk as empty", () => {
    expect(parsePending("not json")).toEqual({});
    expect(parsePending("[]")).toEqual({});
    expect(parsePending('{"testnet:sccpt1qx":[{"txid":1}]}')).toEqual({ "testnet:sccpt1qx": [] });
  });

  it("round-trips per network and address", () => {
    expect(readPending(ADDRESS, NETWORK)).toEqual([]);
    writePending(ADDRESS, NETWORK, [FIRST, SECOND]);
    expect(readPending(ADDRESS, NETWORK)).toEqual([FIRST, SECOND]);
    expect(readPending(ADDRESS, "devnet")).toEqual([]);
    expect(pendingFor(parsePending(null), ADDRESS, NETWORK)).toEqual([]);
    expect(removePending(ADDRESS, NETWORK, FIRST.txid)).toEqual([SECOND]);
    writePending(ADDRESS, NETWORK, []);
    expect(readPending(ADDRESS, NETWORK)).toEqual([]);
  });
});
