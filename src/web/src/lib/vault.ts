/**
 * Where a keyfile lives in the browser: IndexedDB, still encrypted.
 *
 * The wallet stores the keyfile *document* -- scrypt ciphertext and public header
 * -- and never the seed or the passphrase. That is the same bytes `sccp wallet
 * new` writes, so unlocking is one scrypt run and exporting is one download, with
 * no second at-rest format to keep in sync.
 *
 * IndexedDB rather than `localStorage` because it is the only one of the two that
 * is not a synchronous string map shared with every script on the origin, and
 * because a keyfile is a structured document, not a setting. Neither is a
 * security boundary on its own: the ciphertext is the protection, which is why
 * the passphrase never reaches this module.
 */

import type { KeyfileDocument } from "./keystore";

const DB_NAME = "sccp-wallet";
const DB_VERSION = 1;
const STORE = "keyfiles";

export class VaultError extends Error {
  override name = "VaultError";
}

/** One stored keyfile, as the wallet list shows it. */
export interface StoredKeyfile {
  /** Primary key: the bech32 address, which is unique per key and network. */
  address: string;
  network: string;
  /** A person-chosen name; optional because a fresh keyfile has none. */
  label: string;
  document: KeyfileDocument;
  savedAt: number;
}

/** Whether this browser has IndexedDB at all (private modes sometimes do not). */
export function vaultAvailable(): boolean {
  try {
    return typeof indexedDB !== "undefined" && indexedDB !== null;
  } catch {
    return false;
  }
}

function openDatabase(): Promise<IDBDatabase> {
  if (!vaultAvailable()) {
    throw new VaultError(
      "this browser will not store wallets (IndexedDB is unavailable); open a keyfile instead",
    );
  }
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(STORE)) {
        database.createObjectStore(STORE, { keyPath: "address" });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(new VaultError("cannot open the wallet database"));
    request.onblocked = () =>
      reject(new VaultError("another tab is holding an older wallet database open; close it"));
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
      request.onerror = () => reject(new VaultError("the wallet database rejected the request"));
      transaction.onabort = () => reject(new VaultError("the wallet database transaction aborted"));
    });
  } finally {
    database.close();
  }
}

/** Every keyfile this browser holds, newest first. */
export async function listKeyfiles(): Promise<StoredKeyfile[]> {
  const records = await withStore<StoredKeyfile[]>("readonly", (store) => store.getAll());
  return records.sort((left, right) => right.savedAt - left.savedAt);
}

/** Store (or replace) a keyfile, keyed by address. */
export async function saveKeyfile(document: KeyfileDocument, label = ""): Promise<StoredKeyfile> {
  const record: StoredKeyfile = {
    address: document.address,
    network: document.network,
    label: label.trim(),
    document,
    savedAt: Date.now(),
  };
  await withStore<IDBValidKey>("readwrite", (store) => store.put(record));
  return record;
}

/** Read one stored keyfile, or `undefined` if it is gone. */
export async function loadKeyfile(address: string): Promise<StoredKeyfile | undefined> {
  return withStore<StoredKeyfile | undefined>("readonly", (store) => store.get(address));
}

/**
 * Rename a stored wallet without touching its document or its saved date.
 *
 * `saveKeyfile` is the create/import path and stamps `savedAt`; renaming is not
 * adding a wallet, so it goes through here to keep "added" honest. The nickname
 * is local, not part of the keyfile: the ciphertext is unchanged.
 */
export async function renameKeyfile(address: string, label: string): Promise<StoredKeyfile> {
  const record = await loadKeyfile(address);
  if (record === undefined) {
    throw new VaultError("that keyfile is no longer in this browser");
  }
  const renamed: StoredKeyfile = { ...record, label: label.trim() };
  await withStore<IDBValidKey>("readwrite", (store) => store.put(renamed));
  return renamed;
}

/** Forget a keyfile. The encrypted document is the only copy in the browser. */
export async function deleteKeyfile(address: string): Promise<void> {
  await withStore<undefined>("readwrite", (store) => store.delete(address));
}
