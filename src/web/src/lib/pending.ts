/**
 * Transfers this browser signed and the node accepted, until a block includes them.
 *
 * `GET /v1/accounts/{address}` reports *committed* state, so there is no endpoint
 * that answers "is my nonce still waiting?". The wallet knows what it submitted
 * and which nonce each transaction consumed, so that is what it keeps: written
 * when `/v1/tx` accepts a transaction, dropped when the committed nonce passes it.
 *
 * The record is local and the History tab says so. Clearing the browser forgets
 * the pending row, which is not the same as the chain forgetting the transaction;
 * the scan finds it again once a block carries it.
 */

/** A transaction the wallet signed, with the nonce it consumed. */
export interface PendingTransaction {
  txid: string;
  to: string;
  /** Base units. */
  amount: number;
  fee: number;
  nonce: number;
  submittedAt: number;
}

const PENDING_KEY = "sccp.wallet.pending";

/**
 * A row kept this long without being mined is treated as dropped.
 *
 * The node does not promise a mempool lifetime, so the wallet keeps the row for a
 * week -- long enough that a quiet testnet is not mistaken for a lost transfer,
 * short enough that the book cannot grow forever.
 */
export const PENDING_TTL_MS = 7 * 24 * 60 * 60 * 1000;

/** `network:address` -> that account's pending rows. */
type PendingBook = Record<string, PendingTransaction[]>;

const bookKey = (address: string, network: string): string => `${network}:${address}`;

function isPending(value: unknown): value is PendingTransaction {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.txid === "string" &&
    typeof candidate.to === "string" &&
    typeof candidate.amount === "number" &&
    typeof candidate.fee === "number" &&
    typeof candidate.nonce === "number" &&
    typeof candidate.submittedAt === "number"
  );
}

/** Parse the stored book; a malformed entry is dropped, not thrown. */
export function parsePending(text: string | null): PendingBook {
  if (text === null) return {};
  try {
    const parsed: unknown = JSON.parse(text);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return {};
    const book: PendingBook = {};
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (!Array.isArray(value)) continue;
      book[key] = value.filter(isPending).sort((left, right) => left.nonce - right.nonce);
    }
    return book;
  } catch {
    return {};
  }
}

/** The pending rows stored for one account. */
export function pendingFor(
  book: PendingBook,
  address: string,
  network: string,
): PendingTransaction[] {
  return book[bookKey(address, network)] ?? [];
}

/** Add `tx`, replacing an earlier row with the same txid, ordered by nonce. */
export function addPending(
  entries: readonly PendingTransaction[],
  tx: PendingTransaction,
): PendingTransaction[] {
  return [...entries.filter((entry) => entry.txid !== tx.txid), tx].sort(
    (left, right) => left.nonce - right.nonce,
  );
}

/**
 * Drop rows the chain has passed and rows too old to mean anything.
 *
 * `committedNonce` is the account's next unused nonce: a transaction with nonce
 * `n` is included once the committed nonce is greater than `n`, so a row is kept
 * while `n >= committedNonce`.
 */
export function prunePending(
  entries: readonly PendingTransaction[],
  committedNonce: number,
  now: number = Date.now(),
): PendingTransaction[] {
  return entries
    .filter((entry) => entry.nonce >= committedNonce && now - entry.submittedAt < PENDING_TTL_MS)
    .sort((left, right) => left.nonce - right.nonce);
}

function storage(): Storage | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

function readBook(): PendingBook {
  const store = storage();
  if (store === null) return {};
  try {
    return parsePending(store.getItem(PENDING_KEY));
  } catch {
    return {};
  }
}

function writeBook(book: PendingBook): void {
  const store = storage();
  if (store === null) return;
  try {
    store.setItem(PENDING_KEY, JSON.stringify(book));
  } catch {
    // Losing the reminder is not losing the transaction.
  }
}

/** Read one account's pending rows. */
export function readPending(address: string, network: string): PendingTransaction[] {
  return pendingFor(readBook(), address, network);
}

/** Replace one account's pending rows; an empty list removes the key. */
export function writePending(
  address: string,
  network: string,
  entries: readonly PendingTransaction[],
): void {
  const book = readBook();
  const key = bookKey(address, network);
  if (entries.length === 0) delete book[key];
  else book[key] = [...entries];
  writeBook(book);
}

/** Forget one pending row by txid. */
export function removePending(
  address: string,
  network: string,
  txid: string,
): PendingTransaction[] {
  const entries = readPending(address, network).filter((entry) => entry.txid !== txid);
  writePending(address, network, entries);
  return entries;
}
