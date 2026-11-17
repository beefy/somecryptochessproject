/**
 * One unlocked wallet for every route in the tab.
 *
 * The wallet used to own its keypair in component state. Play and DEX are now
 * separate pages, so the key has to outlive a tab switch: this provider holds the
 * keyfile index, the in-memory keypair, the optimistic nonce and the idle timer,
 * and every page reads the same session. The provider itself imports no crypto;
 * `unlock` loads the scrypt/AES module on demand, so an explorer visit does not
 * pay for the wallet's key derivation.
 *
 * The keypair is never persisted, never written to a URL and never placed in a
 * cookie. Locking fills its seed buffers with zeroes before dropping the
 * reference. The in-app menu switches with a hash, so the one origin keeps its
 * IndexedDB keyfile and this session stays alive.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { useNotice } from "../components/Notices";
import { formatDuration, shortAddress } from "../lib/format";
import type { HybridKeypair } from "../lib/keys";
import type { KeyfileDocument } from "../lib/keystore";
import {
  readAutoLockSeconds,
  readLastAddress,
  writeAutoLockSeconds,
  writeLastAddress,
} from "../lib/settings";
import {
  deleteKeyfile,
  listKeyfiles,
  renameKeyfile,
  saveKeyfile,
  type StoredKeyfile,
} from "../lib/vault";

export interface WalletSession {
  record: StoredKeyfile;
  keypair: HybridKeypair;
}

export interface WalletSessionValue {
  session: WalletSession | null;
  stored: StoredKeyfile[] | null;
  storeError: Error | null;
  selected: string | null;
  pendingNonce: number | null;
  autoLockSeconds: number;
  refresh: () => Promise<void>;
  select: (address: string) => void;
  unlock: (record: StoredKeyfile, passphrase: string) => Promise<void>;
  save: (document: KeyfileDocument, keypair: HybridKeypair) => Promise<void>;
  forget: (address: string) => Promise<void>;
  rename: (label: string) => Promise<void>;
  lock: (reason?: string) => void;
  setPendingNonce: (nonce: number | null) => void;
  setAutoLockSeconds: (seconds: number) => void;
}

const WalletSessionContext = createContext<WalletSessionValue | null>(null);

/** How often the idle timer checks whether the tab has been left alone. */
const AUTO_LOCK_TICK_MS = 5000;

function describe(failure: unknown): Error {
  return failure instanceof Error ? failure : new Error(String(failure));
}

function wipe(keypair: HybridKeypair): void {
  keypair.seed.fill(0);
  keypair.ed25519.seed.fill(0);
}

export function WalletSessionProvider({ children }: { children: ReactNode }): ReactNode {
  const notify = useNotice();
  const [stored, setStored] = useState<StoredKeyfile[] | null>(null);
  const [storeError, setStoreError] = useState<Error | null>(null);
  const [selected, setSelected] = useState<string | null>(readLastAddress);
  const [session, setSession] = useState<WalletSession | null>(null);
  const [pendingNonce, setPendingNonce] = useState<number | null>(null);
  const [autoLockSeconds, setAutoLockState] = useState(readAutoLockSeconds);

  const refresh = useCallback(async (): Promise<void> => {
    try {
      setStored(await listKeyfiles());
      setStoreError(null);
    } catch (failure) {
      setStoreError(describe(failure));
      setStored([]);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const select = useCallback((address: string): void => {
    setSelected(address);
    writeLastAddress(address);
  }, []);

  const lock = useCallback(
    (reason?: string): void => {
      setSession((current) => {
        if (current !== null) wipe(current.keypair);
        return null;
      });
      setPendingNonce(null);
      if (reason !== undefined) notify(reason);
    },
    [notify],
  );


  const adopt = useCallback(
    (record: StoredKeyfile, keypair: HybridKeypair): void => {
      setSession((current) => {
        if (current !== null && current.keypair !== keypair) wipe(current.keypair);
        return { record, keypair };
      });
      setSelected(record.address);
      writeLastAddress(record.address);
      setPendingNonce(null);
      notify(`unlocked ${shortAddress(record.address)}`);
    },
    [notify],
  );

  const unlock = useCallback(
    async (record: StoredKeyfile, passphrase: string): Promise<void> => {
      const { unlockKeyfile } = await import("../lib/keystore");
      adopt(record, await unlockKeyfile(record.document, passphrase));
    },
    [adopt],
  );

  const save = useCallback(
    async (document: KeyfileDocument, keypair: HybridKeypair): Promise<void> => {
      adopt(await saveKeyfile(document), keypair);
      await refresh();
    },
    [adopt, refresh],
  );

  const forget = useCallback(
    async (target: string): Promise<void> => {
      await deleteKeyfile(target);
      setSession((current) => {
        if (current?.record.address !== target) return current;
        wipe(current.keypair);
        return null;
      });
      setPendingNonce(null);
      setSelected((current) => {
        if (current !== target) return current;
        writeLastAddress(null);
        return null;
      });
      await refresh();
      notify(`forgot ${shortAddress(target)}`);
    },
    [notify, refresh],
  );

  const rename = useCallback(
    async (label: string): Promise<void> => {
      if (session === null) return;
      try {
        const record = await renameKeyfile(session.record.address, label);
        setSession((current) => (current === null ? current : { ...current, record }));
        await refresh();
        notify(record.label === "" ? "nickname cleared" : `nickname set to ${record.label}`);
      } catch (failure) {
        notify(describe(failure).message);
      }
    },
    [notify, refresh, session],
  );

  const setAutoLockSeconds = useCallback((seconds: number): void => {
    setAutoLockState(seconds);
    writeAutoLockSeconds(seconds);
  }, []);

  // Auto-lock: any pointer or key in the tab counts as activity; silence for the
  // chosen window locks. A timer, not one `setTimeout`, so a sleeping tab wakes up
  // hours later and still notices that the window has passed.
  const lastActivity = useRef(Date.now());
  useEffect(() => {
    if (session === null) return undefined;
    lastActivity.current = Date.now();
    const bump = (): void => {
      lastActivity.current = Date.now();
    };
    globalThis.addEventListener("pointerdown", bump);
    globalThis.addEventListener("keydown", bump);
    const timer = setInterval(() => {
      if (Date.now() - lastActivity.current >= autoLockSeconds * 1000) {
        lock(`locked after ${formatDuration(autoLockSeconds)} idle`);
      }
    }, AUTO_LOCK_TICK_MS);
    return () => {
      globalThis.removeEventListener("pointerdown", bump);
      globalThis.removeEventListener("keydown", bump);
      clearInterval(timer);
    };
  }, [autoLockSeconds, lock, session]);

  const value = useMemo<WalletSessionValue>(
    () => ({
      session,
      stored,
      storeError,
      selected,
      pendingNonce,
      autoLockSeconds,
      refresh,
      select,
      unlock,
      save,
      forget,
      rename,
      lock,
      setPendingNonce,
      setAutoLockSeconds,
    }),
    [
      autoLockSeconds,
      forget,
      lock,
      pendingNonce,
      refresh,
      rename,
      save,
      select,
      selected,
      session,
      storeError,
      stored,
      unlock,
    ],
  );

  return (
    <WalletSessionContext.Provider value={value}>{children}</WalletSessionContext.Provider>
  );
}

export function useWalletSession(): WalletSessionValue {
  const value = useContext(WalletSessionContext);
  if (value === null) {
    throw new Error("useWalletSession must be used inside WalletSessionProvider");
  }
  return value;
}
