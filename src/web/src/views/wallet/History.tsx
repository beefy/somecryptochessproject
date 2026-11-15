/**
 * The wallet's History tab: what the block scan found.
 *
 * A scan is a window, so the view says which window: the heights it covered, how
 * many blocks it read, and a button to read further back or newer. Nothing here
 * invents a transaction: every row is a transaction in a block this tab fetched.
 *
 * The first visit reads the newest `INITIAL_WINDOW` blocks; later visits read only
 * what appeared since, because the window is cached in IndexedDB. A rescan is one
 * button away, because a cache is allowed to be wrong and the chain is not.
 */

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { StyleSheet, View } from "react-native";
import { Button, HelperText, ProgressBar, Text } from "react-native-paper";

import { Link, Loading, Mono, Section, StatusPill } from "../../components/Chrome";
import { labelFor, readContacts, type Contact } from "../../lib/contacts";
import { formatAge, formatAmount, formatInteger, shortAddress, shortHash } from "../../lib/format";
import {
  type CachedHistory,
  type HistoryDirection,
  type HistoryEntry,
  loadHistory,
  mergeHistory,
  saveHistory,
  scanHistory,
} from "../../lib/history";
import type { PendingTransaction } from "../../lib/pending";
import { useNode } from "../../node";
import { useNow } from "../../useResource";

/** Blocks a first visit reads. Enough to find recent activity quickly. */
const INITIAL_WINDOW = 200;

/** Blocks one "older" click reads. */
const OLDER_WINDOW = 200;

/** Rows the table renders; the cache keeps more. */
const VISIBLE_ROWS = 100;

const TONES: Record<HistoryDirection, "good" | "warn" | "bad" | "info" | "neutral"> = {
  in: "good",
  out: "neutral",
  self: "info",
  reward: "good",
};

const LABELS: Record<HistoryDirection, string> = {
  in: "in",
  out: "out",
  self: "self",
  reward: "reward",
};

/** The other side of the entry: a nickname when this browser has one. */
function counterparty(entry: HistoryEntry, contacts: readonly Contact[]): string {
  if (entry.direction === "in" || entry.direction === "reward") {
    return labelFor(contacts, entry.from) ?? shortAddress(entry.from);
  }
  if (entry.to === null) return "-";
  return labelFor(contacts, entry.to) ?? shortAddress(entry.to);
}

function amountText(entry: HistoryEntry): string {
  if (entry.amount === null) return "-";
  return formatAmount(entry.amount);
}

export function HistoryPanel({
  address,
  network,
  pending,
  committedNonce,
}: {
  address: string;
  network: string;
  /** Rows this browser signed that no block has carried yet. */
  pending: readonly PendingTransaction[];
  /** The node's next unused nonce; a row below it has been included. */
  committedNonce: number;
}): ReactNode {
  const { status } = useNode();
  const tip = status.data?.height;
  const now = useNow(30000);
  const contacts = readContacts();
  const [cache, setCache] = useState<CachedHistory | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [scan, setScan] = useState<{ done: number; total: number } | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const abort = useRef<AbortController | null>(null);

  useEffect(() => {
    let active = true;
    setLoaded(false);
    setCache(null);
    setError(null);
    loadHistory(address, network).then((record) => {
      if (!active) return;
      setCache(record ?? null);
      setLoaded(true);
    });
    return () => {
      active = false;
    };
  }, [address, network]);

  const run = useCallback(
    async (fromHeight: number, toHeight: number): Promise<void> => {
      abort.current?.abort();
      const controller = new AbortController();
      abort.current = controller;
      const total = Math.max(1, fromHeight - toHeight + 1);
      setScan({ done: 0, total });
      setError(null);
      try {
        const result = await scanHistory({
          address,
          fromHeight,
          toHeight,
          signal: controller.signal,
          onProgress: (done, count) => setScan({ done, total: count }),
        });
        const merged = mergeHistory(cache?.entries ?? [], result.entries);
        const record: CachedHistory = {
          address,
          network,
          scannedFrom: Math.max(cache?.scannedFrom ?? result.scannedFrom, result.scannedFrom),
          scannedTo: Math.min(cache?.scannedTo ?? result.scannedTo, result.scannedTo),
          complete: (cache?.complete ?? false) || result.complete,
          entries: merged,
          savedAt: Date.now(),
        };
        await saveHistory(record);
        setCache(record);
        if (result.failures > 0) {
          setError(
            new Error(
              `${formatInteger(result.failures)} block(s) could not be read, so the window has gaps; scan again to fill them`,
            ),
          );
        }
      } catch (failure) {
        if (failure instanceof DOMException && failure.name === "AbortError") return;
        setError(failure instanceof Error ? failure : new Error(String(failure)));
      } finally {
        setScan(null);
        abort.current = null;
      }
    },
    [address, cache, network],
  );

  // First visit: read the newest window once the node has told us the tip.
  useEffect(() => {
    if (!loaded || cache !== null || tip === undefined || scan !== null) return;
    void run(tip, Math.max(0, tip - INITIAL_WINDOW + 1));
  }, [cache, loaded, run, scan, tip]);

  useEffect(() => () => abort.current?.abort(), []);

  const newerAvailable = cache !== null && tip !== undefined && tip > cache.scannedFrom;
  const olderAvailable = cache !== null && cache.scannedTo > 0;
  const entries = cache?.entries ?? [];
  const pendingNote = pending.length === 0 ? "" : ` · ${pending.length} pending`;

  return (
    <Section
      title="History"
      subtitle={
        cache === null
          ? `no window read yet · ${network}${pendingNote}`
          : `${formatInteger(entries.length)} entries · heights ${formatInteger(
              cache.scannedTo,
            )}–${formatInteger(cache.scannedFrom)} · ${network}${pendingNote}`
      }
    >
      {!loaded ? (
        <Loading />
      ) : scan !== null ? (
        <>
          <ProgressBar
            progress={scan.total === 0 ? 0 : scan.done / scan.total}
            indeterminate={scan.total === 0}
          />
          <Text variant="bodySmall" style={styles.muted}>
            Reading block {formatInteger(scan.done)} of {formatInteger(scan.total)}…
          </Text>
        </>
      ) : (
        <View style={styles.actions}>
          <Button
            mode="outlined"
            disabled={!newerAvailable || tip === undefined}
            onPress={() => {
              if (cache === null || tip === undefined) return;
              void run(tip, cache.scannedFrom + 1);
            }}
          >
            Scan newer
          </Button>
          <Button
            mode="outlined"
            disabled={!olderAvailable}
            onPress={() => {
              if (cache === null) return;
              void run(cache.scannedTo - 1, cache.scannedTo - OLDER_WINDOW);
            }}
          >
            Scan {OLDER_WINDOW} older
          </Button>
          <Button
            mode="text"
            disabled={tip === undefined}
            onPress={() => {
              if (tip === undefined) return;
              void run(tip, Math.max(0, tip - INITIAL_WINDOW + 1));
            }}
          >
            Rescan
          </Button>
        </View>
      )}

      {error !== null && (
        <HelperText type="error" visible>
          {error.message}
        </HelperText>
      )}

      {pending.length > 0 && (
        <View style={styles.pendingBlock}>
          <Text variant="titleSmall">Pending from this browser</Text>
          <Text variant="bodySmall" style={styles.muted}>
            signed here and accepted by the node; a row leaves this list when a block includes its
            nonce
          </Text>
          {pending.map((entry) => (
            <View key={entry.txid} style={styles.row}>
              <StatusPill
                label={committedNonce > entry.nonce ? "included" : "pending"}
                tone={committedNonce > entry.nonce ? "info" : "warn"}
              />
              <Text variant="bodyMedium" style={styles.amount}>
                {formatAmount(entry.amount)}
              </Text>
              <Text variant="bodySmall" style={styles.muted}>
                to {labelFor(contacts, entry.to) ?? shortAddress(entry.to)} · nonce{" "}
                {formatInteger(entry.nonce)}
              </Text>
              <Text variant="bodySmall" style={styles.muted}>
                {formatAge(Math.floor(entry.submittedAt / 1000), now)}
              </Text>
              <Mono copy>{shortHash(entry.txid, 16)}</Mono>
            </View>
          ))}
        </View>
      )}

      {cache !== null && entries.length === 0 && scan === null && (
        <Text variant="bodyMedium" style={styles.muted}>
          No transaction touching this address in heights {formatInteger(cache.scannedTo)}–
          {formatInteger(cache.scannedFrom)}. Scan further back if the account is older than that.
        </Text>
      )}

      {entries.slice(0, VISIBLE_ROWS).map((entry) => (
        <View key={`${entry.txid}:${entry.height}`} style={styles.row}>
          <Link href={`#/blocks/${entry.height}`}>{`#${formatInteger(entry.height)}`}</Link>
          <StatusPill label={LABELS[entry.direction]} tone={TONES[entry.direction]} />
          <Text variant="bodyMedium" style={styles.amount}>
            {amountText(entry)}
          </Text>
          <Text variant="bodySmall" style={styles.muted}>
            {entry.direction === "in" || entry.direction === "reward" ? "from" : "to"}{" "}
            {counterparty(entry, contacts)} · {entry.type} · nonce {formatInteger(entry.nonce)}
          </Text>
          <Text variant="bodySmall" style={styles.muted}>
            {formatAge(entry.timestamp, now)}
          </Text>
          {entry.txid !== "" && <Mono copy>{shortHash(entry.txid, 16)}</Mono>}
        </View>
      ))}

      {entries.length > VISIBLE_ROWS && (
        <Text variant="bodySmall" style={styles.muted}>
          Showing the newest {VISIBLE_ROWS} of {formatInteger(entries.length)}; the rest stay in the
          cache.
        </Text>
      )}

      <HelperText type="info" visible>
        The node has no history endpoint, so this list is a block scan: the window named above is
        what was actually read, and it is cached in this browser. A transfer outside the window
        will not appear until a scan reaches it. Pending rows are this browser&apos;s record of
        what it signed, not the node&apos;s.
      </HelperText>
    </Section>
  );
}

const styles = StyleSheet.create({
  actions: { flexDirection: "row", alignItems: "center", gap: 8, flexWrap: "wrap" },
  pendingBlock: { gap: 4 },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    flexWrap: "wrap",
    paddingVertical: 6,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: "rgba(128,128,128,0.32)",
  },
  amount: { fontVariant: ["tabular-nums"], minWidth: 130 },
  muted: { opacity: 0.66 },
});
