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
 * The tabs are the wallet's own: Overview, Send, Receive, Faucet, History and
 * Settings. Play lives on its own page now; the shared session provider keeps the
 * unlocked key available there without putting the chessboard in every wallet
 * load. A nickname is a local label -- the wallet's own in the vault, and one per
 * address it sends to -- so every screen can name an account without pretending
 * the name is on chain. Transfers the node accepted but no block has carried yet
 * are remembered here and listed on Overview and History until the committed
 * nonce passes them.
 *
 * `#/wallet` is this page on `https://app.proofofchess.com`; the hamburger menu and
 * every wallet link point there without leaving the origin.
 */

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { StyleSheet, View } from "react-native";
import { Button, HelperText, Text, TextInput } from "react-native-paper";

import { fetchAccount } from "../api";
import {
  CopyButton,
  FailureNotice,
  KeyValue,
  Loading,
  Link,
  Mono,
  Section,
  StatusPill,
} from "../components/Chrome";
import { useNotice } from "../components/Notices";
import { useWalletSession } from "../components/WalletSession";
import { labelFor, readContacts } from "../lib/contacts";
import { tokenImmature, tokensFor, tokenBalance } from "../lib/assets";
import { downloadText, keyfileName } from "../lib/download";
import {
  formatAmount,
  formatDuration,
  formatInteger,
  formatTokenAmount,
  shortAddress,
  shortHash,
} from "../lib/format";
import { keyfileText } from "../lib/keystore";
import {
  addPending,
  prunePending,
  readPending,
  writePending,
  type PendingTransaction,
} from "../lib/pending";
import { AUTO_LOCK_CHOICES } from "../lib/settings";
import { useNode } from "../node";
import { useResource } from "../useResource";
import { FaucetPanel } from "./wallet/Faucet";
import { HistoryPanel } from "./wallet/History";
import { SendPanel } from "./wallet/Send";
import { WalletSetup } from "./wallet/Setup";

/** How often the unlocked wallet re-reads the account. */
const ACCOUNT_POLL_MS = 8000;

export function Wallet({
  view,
}: {
  view: "overview" | "send" | "receive" | "faucet" | "history" | "settings";
}): ReactNode {
  const { status, params, network } = useNode();
  const notify = useNotice();
  const {
    session,
    stored,
    storeError,
    selected,
    select,
    unlock,
    save,
    forget: forgetKeyfile,
    rename,
    lock: lockSession,
    pendingNonce,
    setPendingNonce,
    autoLockSeconds,
    setAutoLockSeconds,
  } = useWalletSession();
  const [pending, setPending] = useState<PendingTransaction[]>([]);
  const [nickname, setNickname] = useState("");
  const [forgetting, setForgetting] = useState<string | null>(null);

  const address = session?.record.address ?? null;
  const account = useResource(
    (signal) => (address === null ? Promise.resolve(null) : fetchAccount(address, signal)),
    [address],
    { pollMs: ACCOUNT_POLL_MS },
  );
  const accountView = account.data ?? null;

  useEffect(() => {
    setNickname(session?.record.label ?? "");
  }, [session?.record.address, session?.record.label]);

  const lock = useCallback(
    (reason?: string): void => {
      setPending([]);
      setForgetting(null);
      lockSession(reason);
    },
    [lockSession],
  );

  const forget = useCallback(
    async (target: string): Promise<void> => {
      await forgetKeyfile(target);
      setPending([]);
      setForgetting(null);
    },
    [forgetKeyfile],
  );

  // Pending rows belong to one key: read them when a wallet is adopted, and let
  // the account effect below retire them once the chain has passed their nonce.
  const sessionAddress = session?.record.address ?? null;
  const sessionNetwork = session?.record.network ?? null;
  useEffect(() => {
    if (sessionAddress === null || sessionNetwork === null) {
      setPending([]);
      return;
    }
    setPending(readPending(sessionAddress, sessionNetwork));
  }, [sessionAddress, sessionNetwork]);

  // The chain is the truth about the nonce; once it reaches the optimistic one,
  // the local override has nothing left to say. The same comparison retires a
  // pending row: it is included when the committed nonce has passed its nonce.
  const chainNonce = accountView?.nonce;
  useEffect(() => {
    if (chainNonce === undefined) return;
    if (pendingNonce !== null && chainNonce >= pendingNonce) setPendingNonce(null);
    if (pending.length === 0) return;
    const included = pending.filter((entry) => entry.nonce < chainNonce);
    const next = prunePending(pending, chainNonce);
    if (included.length === 0 && next.length === pending.length) return;
    if (session !== null) {
      writePending(session.record.address, session.record.network, next);
    }
    setPending(next);
    for (const entry of included) {
      const name = labelFor(readContacts(), entry.to) ?? shortAddress(entry.to);
      notify(
        `included: ${formatAmount(entry.amount)} to ${name} (nonce ${formatInteger(entry.nonce)})`,
      );
    }
  }, [chainNonce, notify, pending, pendingNonce, session]);

  /**
   * A send is accepted by the node before a block includes it. Record it here so
   * History and Overview can show it as pending, and say so immediately: the
   * review screen's "Sign and send" is not the end of the story.
   */
  const accepted = useCallback(
    (tx: PendingTransaction): void => {
      if (session === null) return;
      // Read the book rather than the rendered state: two acceptances in one
      // render would otherwise both start from the same list and lose one row.
      const book = readPending(session.record.address, session.record.network);
      const next = addPending(book, tx);
      writePending(session.record.address, session.record.network, next);
      setPending(next);
      const name = labelFor(readContacts(), tx.to) ?? shortAddress(tx.to);
      notify(
        `sent ${formatTokenAmount(tx.amount, tx.symbol ?? "ZQWX")} to ${name} · pending at nonce ${formatInteger(tx.nonce)}`,
      );
    },
    [notify, session],
  );

  const title = ((): ReactNode => {
    if (session === null) return null;
    const { record } = session;
    return (
      <View style={styles.titleRow}>
        <View style={styles.titleBlock}>
          <Text variant="headlineSmall">{record.label === "" ? "Wallet" : record.label}</Text>
          <Mono copy>{record.address}</Mono>
          <Text variant="bodySmall" style={styles.muted}>
            {record.network} · unlocked in this tab
          </Text>
        </View>
        <View style={styles.titleActions}>
          <StatusPill
            label={accountView === null ? `${record.network} (no node)` : record.network}
            tone={accountView === null ? "warn" : "info"}
          />
          <CopyButton value={record.address} />
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
          onSelect={select}
          onUnlock={unlock}
          onImport={save}
          onCreated={save}
          onForget={forget}
        />
      );
    }

    const { record, keypair } = session;
    const tokens = tokensFor(params.data);

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
            tokens={tokens}
            pendingNonce={pendingNonce}
            onAccepted={accepted}
            onPendingNonce={setPendingNonce}
          />
          {pending.length > 0 && (
            <HelperText type="info" visible>
              {pending.length === 1 ? "One transfer is" : `${pending.length} transfers are`} waiting
              for a block at nonce {formatInteger(pending[pending.length - 1]?.nonce ?? 0)}; the
              node&apos;s committed nonce is what retires them, and History lists them while they
              wait.
            </HelperText>
          )}
        </>
      );
    }

    if (view === "receive") {
      return (
        <>
          {title}
          <Section
            title="Receive"
            subtitle={`${record.network} only`}
            action={<CopyButton value={record.address} />}
          >
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
              <View style={styles.tokenBlock}>
                <Text variant="titleSmall">Balances at this address</Text>
                {tokens.map((token) => (
                  <KeyValue key={token.asset ?? "native"} label={token.symbol} mono>
                    {formatTokenAmount(
                      tokenBalance(accountView, token),
                      token.symbol,
                      token.decimals,
                    )}
                  </KeyValue>
                ))}
                <HelperText type="info" visible>
                  One address can receive every token this network settles: ZQWX and assets such
                  as C960. The sender&apos;s transaction names the asset; this address needs no
                  separate token account. Holding C960 does not pay fees, though -- the same
                  account needs a little ZQWX to move it.
                </HelperText>
              </View>
            )}
            {account.error !== undefined && <FailureNotice error={account.error} />}
          </Section>
        </>
      );
    }

    if (view === "faucet") {
      return (
        <>
          {title}
          <FaucetPanel
            address={record.address}
            network={record.network}
            account={accountView}
            onFunded={() => account.reload()}
          />
          <HelperText type="info" visible>
            A grant is a transaction like any other: the spendable balance moves when a block
            includes it, and the History tab finds it on its next scan.
          </HelperText>
        </>
      );
    }

    if (view === "history") {
      return (
        <>
          {title}
          <HistoryPanel
            address={record.address}
            network={record.network}
            pending={pending}
            committedNonce={accountView?.nonce ?? 0}
          />
        </>
      );
    }

    if (view === "settings") {
      return (
        <>
          {title}
          <Section title="Nickname" subtitle="how this wallet is listed in this browser">
            <TextInput
              label="Nickname"
              mode="outlined"
              value={nickname}
              onChangeText={setNickname}
              placeholder="no nickname"
            />
            <View style={styles.actions}>
              <Button
                mode="contained"
                disabled={nickname.trim() === record.label}
                onPress={() => void rename(nickname)}
              >
                Save nickname
              </Button>
              {record.label !== "" && <Button onPress={() => void rename("")}>Clear</Button>}
            </View>
            <HelperText type="info" visible>
              A nickname is local to this browser and is not part of the keyfile: it is never
              signed and never leaves this device.
            </HelperText>
          </Section>

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
          <Section
            title="Balances"
            subtitle="as the node reports them, refreshed every few seconds"
            action={
              <Button compact mode="outlined" onPress={() => account.reload()}>
                Refresh
              </Button>
            }
          >
            {tokens.map((token) => {
              const spendable = tokenBalance(accountView, token);
              const immature = tokenImmature(accountView, token);
              return (
                <View key={token.asset ?? "native"} style={styles.tokenBlock}>
                  <Text variant="titleSmall">
                    {token.name} · {token.symbol}
                  </Text>
                  <KeyValue label="Spendable" mono>
                    {formatTokenAmount(spendable, token.symbol, token.decimals)}
                  </KeyValue>
                  {immature > 0 && (
                    <KeyValue label="Immature" mono>
                      {formatTokenAmount(immature, token.symbol, token.decimals)}
                    </KeyValue>
                  )}
                  {token.native && (
                    <>
                      <KeyValue label="Bonded" mono>
                        {formatAmount(accountView.bonded)}
                      </KeyValue>
                      <KeyValue label="Total ZQWX" mono>
                        {formatAmount(accountView.total)}
                      </KeyValue>
                    </>
                  )}
                </View>
              );
            })}
            <KeyValue label="Next nonce" mono>
              {formatInteger(pendingNonce ?? accountView.nonce)}
            </KeyValue>
            {pending.map((entry) => (
              <KeyValue key={entry.txid} label="Pending">
                {formatTokenAmount(entry.amount, entry.symbol ?? "ZQWX")} to{" "}
                {labelFor(readContacts(), entry.to) ?? shortAddress(entry.to)} · nonce{" "}
                {formatInteger(entry.nonce)}
              </KeyValue>
            ))}
            <KeyValue label="Explorer">
              <Link href={`#/accounts/${encodeURIComponent(record.address)}`}>this account</Link>
            </KeyValue>
          </Section>
        )}
        <HelperText type="info" visible>
          The node has no transaction-history endpoint yet, so this page shows committed balances
          and the transfers this browser is still waiting on; the History tab scans blocks for the
          rest.
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
  tokenBlock: {
    gap: 2,
    paddingVertical: 8,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: "rgba(128,128,128,0.24)",
  },
  muted: { opacity: 0.66 },
});
