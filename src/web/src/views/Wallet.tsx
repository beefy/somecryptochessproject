/**
 * The wallet: a keyfile in this browser, unlocked by a passphrase, used to sign.
 *
 * What it holds: the keyfile document (scrypt ciphertext, never a plaintext seed)
 * in IndexedDB, and -- while unlocked -- the derived hybrid keypair in memory. The
 * passphrase is used once per unlock and dropped; no seed is written to storage,
 * the URL, the clipboard or a log. Locking overwrites the seed bytes before the
 * reference is released, and an idle timer does the same when the tab is left
 * alone.
 *
 * What it does: shows the committed balance and nonce the node reports, lets the
 * faucet fund a testnet account, builds and signs a transfer (after a review of
 * every signed field), and exports the keyfile the CLI can open. The BIP-39
 * phrase shown at creation is the backup, and it is shown exactly once.
 *
 * `#/` is this page when served from `wallet.chessproofofwork.com`; the same
 * routes work from the explorer, where the hamburger menu sends people here.
 */

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { StyleSheet, View } from "react-native";
import { Button, HelperText, Snackbar, Text } from "react-native-paper";

import { fetchAccount, requestFaucet } from "../api";
import { FailureNotice, KeyValue, Loading, Link, Mono, Section, StatusPill } from "../components/Chrome";
import { downloadText, keyfileName } from "../lib/download";
import { formatAmount, formatDuration, formatInteger, shortAddress, shortHash } from "../lib/format";
import type { HybridKeypair } from "../lib/keys";
import { keyfileText, unlockKeyfile, type KeyfileDocument } from "../lib/keystore";
import {
  AUTO_LOCK_CHOICES,
  readAutoLockSeconds,
  readLastAddress,
  writeAutoLockSeconds,
  writeLastAddress,
} from "../lib/settings";
import { deleteKeyfile, listKeyfiles, saveKeyfile, type StoredKeyfile } from "../lib/vault";
import { useNode } from "../node";
import { navigate } from "../router";
import { useResource } from "../useResource";
import { SendPanel, type SentTransaction } from "./wallet/Send";
import { WalletSetup } from "./wallet/Setup";

interface Session {
  record: StoredKeyfile;
  keypair: HybridKeypair;
}

/** How often the unlocked wallet re-reads the account. */
const ACCOUNT_POLL_MS = 8000;

/** How often the idle timer checks whether the tab has been left alone. */
const AUTO_LOCK_TICK_MS = 5000;

function describe(failure: unknown): Error {
  return failure instanceof Error ? failure : new Error(String(failure));
}

export function Wallet({ view }: { view: "overview" | "send" | "receive" | "settings" }): ReactNode {
  const { status, params, network } = useNode();
  const [stored, setStored] = useState<StoredKeyfile[] | null>(null);
  const [storeError, setStoreError] = useState<Error | null>(null);
  const [selected, setSelected] = useState<string | null>(readLastAddress);
  const [session, setSession] = useState<Session | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [pendingNonce, setPendingNonce] = useState<number | null>(null);
  const [sent, setSent] = useState<SentTransaction | null>(null);
  const [busy, setBusy] = useState(false);
  const [autoLockSeconds, setAutoLockSeconds] = useState(readAutoLockSeconds);
  const [forgetting, setForgetting] = useState<string | null>(null);

  const address = session?.record.address ?? null;
  const account = useResource(
    (signal) => (address === null ? Promise.resolve(null) : fetchAccount(address, signal)),
    [address],
    { pollMs: ACCOUNT_POLL_MS },
  );
  const accountView = account.data ?? null;

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

  const lock = useCallback((reason?: string): void => {
    setSession((current) => {
      if (current !== null) {
        current.keypair.seed.fill(0);
        current.keypair.ed25519.seed.fill(0);
      }
      return null;
    });
    setPendingNonce(null);
    setSent(null);
    setForgetting(null);
    if (reason !== undefined) setNotice(reason);
  }, []);

  const adopt = useCallback((record: StoredKeyfile, keypair: HybridKeypair): void => {
    setSession({ record, keypair });
    setSelected(record.address);
    writeLastAddress(record.address);
    setNotice(`unlocked ${shortAddress(record.address)}`);
  }, []);

  const unlock = useCallback(
    async (record: StoredKeyfile, passphrase: string): Promise<void> => {
      adopt(record, await unlockKeyfile(record.document, passphrase));
    },
    [adopt],
  );

  const store = useCallback(
    async (document: KeyfileDocument, keypair: HybridKeypair): Promise<void> => {
      adopt(await saveKeyfile(document), keypair);
      await refresh();
    },
    [adopt, refresh],
  );

  const forget = useCallback(
    async (target: string): Promise<void> => {
      await deleteKeyfile(target);
      if (session?.record.address === target) lock();
      if (selected === target) {
        setSelected(null);
        writeLastAddress(null);
      }
      await refresh();
      setForgetting(null);
      setNotice(`forgot ${shortAddress(target)}`);
    },
    [lock, refresh, selected, session],
  );


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

  // The chain is the truth about the nonce; once it reaches the optimistic one,
  // the local override has nothing left to say.
  const chainNonce = accountView?.nonce;
  useEffect(() => {
    if (chainNonce === undefined) return;
    if (pendingNonce !== null && chainNonce >= pendingNonce) setPendingNonce(null);
    if (sent !== null && chainNonce > sent.nonce) {
      setNotice(
        `included: ${formatAmount(sent.amount)} to ${shortAddress(sent.to)} (nonce ${formatInteger(
          sent.nonce,
        )})`,
      );
      setSent(null);
    }
  }, [chainNonce, pendingNonce, sent]);

  const fund = async (): Promise<void> => {
    if (address === null) return;
    setBusy(true);
    try {
      const grant = await requestFaucet(address);
      setNotice(`the faucet sent ${formatAmount(grant.amount)} (${shortHash(grant.txid)}…)`);
      account.reload();
    } catch (failure) {
      setNotice(describe(failure).message);
    } finally {
      setBusy(false);
    }
  };

  const title = ((): ReactNode => {
    if (session === null) return null;
    const { record } = session;
    return (
      <View style={styles.titleRow}>
        <View style={styles.titleBlock}>
          <Text variant="headlineSmall">Wallet</Text>
          <Mono copy>{record.address}</Mono>
          <Text variant="bodySmall" style={styles.muted}>
            {record.label === "" ? record.network : `${record.label} · ${record.network}`} · unlocked
            in this tab
          </Text>
        </View>
        <View style={styles.titleActions}>
          <StatusPill
            label={accountView === null ? `${record.network} (no node)` : record.network}
            tone={accountView === null ? "warn" : "info"}
          />
          <Button mode="outlined" onPress={() => lock("locked")}>
            Lock
          </Button>
        </View>
      </View>
    );
  })();

  const body = ((): ReactNode => {
    if (session === null) {
      if (stored === null && storeError === null) return <Loading />;
      return (
        <WalletSetup
          network={network}
          stored={stored ?? []}
          storeError={storeError}
          selected={selected}
          onSelect={setSelected}
          onUnlock={unlock}
          onImport={store}
          onCreated={store}
          onForget={forget}
        />
      );
    }

    const { record, keypair } = session;
    const mainnet = record.network === "mainnet";

    if (view === "send") {
      if (accountView === null || params.data === undefined) {
        return (
          <>
            {title}
            {account.error !== undefined ? <FailureNotice error={account.error} /> : <Loading />}
            <HelperText type="info" visible>
              Sending needs the node: it reports the balance and nonce the transaction has to
              satisfy. Unlocking and receiving work offline.
            </HelperText>
          </>
        );
      }
      return (
        <>
          {title}
          <SendPanel
            keypair={keypair}
            address={record.address}
            network={record.network}
            account={accountView}
            minFee={params.data.min_fee}
            pendingNonce={pendingNonce}
            onAccepted={setSent}
            onPendingNonce={setPendingNonce}
          />
          {sent !== null && (
            <HelperText type="info" visible>
              {shortHash(sent.txid, 12)}… is in the mempool at nonce {formatInteger(sent.nonce)};
              this page refreshes until the chain includes it.
            </HelperText>
          )}
        </>
      );
    }

    if (view === "receive") {
      return (
        <>
          {title}
          <Section title="Receive" subtitle={`${record.network} only`}>
            <Mono copy>{record.address}</Mono>
            <HelperText type="info" visible>
              Share this address. It is derived from the hybrid public key below, so a sender does
              not need anything else -- but an address on another network is a different account.
            </HelperText>
            <KeyValue label="Public key fingerprint" mono>
              {shortHash(record.document.public_key, 20)}…
            </KeyValue>
            <KeyValue label="Network" mono>
              {record.network}
            </KeyValue>
            {accountView !== null && (
              <KeyValue label="Spendable now" mono>
                {formatAmount(accountView.balance)}
              </KeyValue>
            )}
            {!mainnet && (
              <View style={styles.actions}>
                <Button
                  mode="outlined"
                 
                  loading={busy}
                  disabled={busy}
                  onPress={() => void fund()}
                >
                  Fund from the faucet
                </Button>
              </View>
            )}
            {account.error !== undefined && <FailureNotice error={account.error} />}
          </Section>
        </>
      );
    }

    if (view === "settings") {
      return (
        <>
          {title}
          <Section title="Export" subtitle="the file `sccp wallet new` writes">
            <View style={styles.actions}>
              <Button
                mode="contained"
               
                onPress={() =>
                  downloadText(keyfileName(record.document.address), keyfileText(record.document))
                }
              >
                Download keyfile
              </Button>
            </View>
            <HelperText type="info" visible>
              Still encrypted with your passphrase. Keep it where a backup belongs; the 24 words
              restore the same key if the file is lost.
            </HelperText>
          </Section>

          <Section title="Auto-lock" subtitle="idle time before the key is cleared from memory">
            <View style={styles.actions}>
              {AUTO_LOCK_CHOICES.map((seconds) => (
                <Button
                  key={seconds}
                  mode={autoLockSeconds === seconds ? "contained-tonal" : "outlined"}
                  onPress={() => {
                    setAutoLockSeconds(seconds);
                    writeAutoLockSeconds(seconds);
                  }}
                >
                  {formatDuration(seconds)}
                </Button>
              ))}
            </View>
            <HelperText type="info" visible>
              Locking overwrites the seed in this tab. The keyfile stays in the browser, still
              encrypted.
            </HelperText>
          </Section>

          <Section title="This browser" subtitle="what is stored, and how to remove it">
            <KeyValue label="Keyfile" mono>
              {record.address}
            </KeyValue>
            <KeyValue label="Added">{new Date(record.savedAt).toLocaleString()}</KeyValue>
            <HelperText type="info" visible>
              Forgetting removes the encrypted keyfile from this browser. Without the download or
              the 24 words, the account is unreachable.
            </HelperText>
            {forgetting === record.address ? (
              <View style={styles.actions}>
                <Button
                  mode="contained"
                  buttonColor="#b3261e"
                  textColor="#ffffff"
                  onPress={() => void forget(record.address)}
                >
                  Forget for good
                </Button>
                <Button onPress={() => setForgetting(null)}>Keep it</Button>
              </View>
            ) : (
              <View style={styles.actions}>
                <Button mode="outlined" onPress={() => setForgetting(record.address)}>
                  Forget this wallet
                </Button>
              </View>
            )}
          </Section>
        </>
      );
    }

    return (
      <>
        {title}
        {account.error !== undefined ? (
          <FailureNotice error={account.error} />
        ) : accountView === null ? (
          <Loading />
        ) : (
          <Section title="Balance" subtitle="as the node reports it, refreshed every few seconds">
            <KeyValue label="Spendable" mono>
              {formatAmount(accountView.balance)}
            </KeyValue>
            <KeyValue label="Immature" mono>
              {formatAmount(accountView.immature)}
            </KeyValue>
            <KeyValue label="Bonded" mono>
              {formatAmount(accountView.bonded)}
            </KeyValue>
            <KeyValue label="Total" mono>
              {formatAmount(accountView.total)}
            </KeyValue>
            <KeyValue label="Next nonce" mono>
              {formatInteger(pendingNonce ?? accountView.nonce)}
            </KeyValue>
            {sent !== null && (
              <KeyValue label="Pending">
                {formatAmount(sent.amount)} to {shortAddress(sent.to)} · nonce{" "}
                {formatInteger(sent.nonce)}
              </KeyValue>
            )}
            <View style={styles.actions}>
              {!mainnet && (
                <Button
                  mode="outlined"
                 
                  loading={busy}
                  disabled={busy}
                  onPress={() => void fund()}
                >
                  Fund from the faucet
                </Button>
              )}
              <Button mode="contained" onPress={() => navigate("#/wallet/send")}>
                Send SCCP
              </Button>
              <Button mode="text" onPress={() => navigate("#/wallet/receive")}>
                Receive
              </Button>
            </View>
            <KeyValue label="Explorer">
              <Link href={`#/accounts/${encodeURIComponent(record.address)}`}>this account</Link>
            </KeyValue>
          </Section>
        )}
        <HelperText type="info" visible>
          The node has no transaction-history endpoint yet, so this page shows committed balances
          and the transaction you just sent; the explorer&apos;s block list is the history.
        </HelperText>
      </>
    );
  })();

  return (
    <View style={styles.root}>
      {status.error !== undefined && (
        <HelperText type="error" visible>
          {status.error.message}
        </HelperText>
      )}
      {body}
      <Snackbar
        visible={notice !== null}
        onDismiss={() => setNotice(null)}
        duration={6000}
        action={{ label: "Dismiss", onPress: () => setNotice(null) }}
      >
        {notice ?? ""}
      </Snackbar>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { gap: 16 },
  titleRow: {
    flexDirection: "row",
    alignItems: "flex-start",
    justifyContent: "space-between",
    gap: 12,
    flexWrap: "wrap",
  },
  titleBlock: { gap: 4, flexShrink: 1 },
  titleActions: { flexDirection: "row", alignItems: "center", gap: 10, flexWrap: "wrap" },
  actions: { flexDirection: "row", alignItems: "center", gap: 8, flexWrap: "wrap" },
  muted: { opacity: 0.66 },
});
