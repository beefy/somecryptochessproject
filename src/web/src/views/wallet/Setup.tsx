/**
 * The wallet before it is unlocked: pick one, create one, restore one.
 *
 * Three ways in, and all of them end at the same place -- a keyfile *document*
 * (scrypt ciphertext plus header) and, for a moment, the keypair derived from it
 * and the passphrase just typed. The document goes to the caller to store; the
 * keypair goes to the session; the passphrase goes nowhere.
 *
 * Creating a wallet shows the 24-word backup exactly once, before the key is
 * saved. There is no "later": a key that is funded before its phrase is written
 * down is a key somebody loses, and the screen says so instead of hiding it in a
 * settings tab.
 */

import { useState, type ReactNode } from "react";
import { Pressable, StyleSheet, View } from "react-native";
import { Button, HelperText, Text, TextInput, useTheme } from "react-native-paper";

import {
  MIN_PASSPHRASE_CHARS,
  type KeyfileDocument,
  createKeyfile,
  keyfileText,
  parseKeyfile,
  unlockKeyfile,
} from "../../lib/keystore";
import { downloadText, keyfileName } from "../../lib/download";
import { formatInteger } from "../../lib/format";
import { mnemonicToSeed, seedToMnemonic } from "../../lib/mnemonic";
import { pickTextFile, readTextFile } from "../../lib/upload";
import type { HybridKeypair } from "../../lib/keys";
import { Mono, Section, StatusPill } from "../../components/Chrome";
import { monospace } from "../../theme";
import type { StoredKeyfile } from "../../lib/vault";

type Mode = "none" | "unlock" | "create" | "restore" | "open";

interface SetupProps {
  /** The network the node reports, or `null` before it answers. */
  network: string | null;
  stored: StoredKeyfile[];
  storeError: Error | null;
  selected: string | null;
  onSelect: (address: string) => void;
  onUnlock: (record: StoredKeyfile, passphrase: string) => Promise<void>;
  onImport: (document: KeyfileDocument, keypair: HybridKeypair) => Promise<void>;
  onCreated: (document: KeyfileDocument, keypair: HybridKeypair) => Promise<void>;
  onForget: (address: string) => Promise<void>;
}

export function WalletSetup(props: SetupProps): ReactNode {
  const [mode, setMode] = useState<Mode>("none");
  const [confirming, setConfirming] = useState<string | null>(null);
  const selected = props.stored.find((record) => record.address === props.selected) ?? null;
  const cancel = (): void => setMode("none");

  return (
    <>
      <Section
        title="Open a wallet"
        subtitle="a hybrid Ed25519 + ML-DSA-65 keyfile, unlocked in this tab"
      >
        {props.storeError !== null ? (
          <HelperText type="error" visible>
            {props.storeError.message}
          </HelperText>
        ) : props.stored.length === 0 ? (
          <Text variant="bodyMedium" style={styles.muted}>
            This browser holds no keyfile yet, and this page never keeps a seed of its own.
          </Text>
        ) : (
          props.stored.map((record) => (
            <View key={record.address} style={styles.walletRow}>
              <View style={styles.walletMain}>
                <Text variant="bodyMedium" style={styles.walletLabel}>
                  {record.label === "" ? record.address : record.label}
                </Text>
                {record.label !== "" && <Mono copy>{record.address}</Mono>}
                <Text variant="bodySmall" style={styles.muted}>
                  {record.network} · added {new Date(record.savedAt).toLocaleString()}
                </Text>
              </View>
              <View style={styles.walletActions}>
                {props.selected === record.address && <StatusPill label="selected" tone="info" />}
                <Button
                  compact
                  mode={props.selected === record.address ? "contained-tonal" : "outlined"}
                  onPress={() => {
                    props.onSelect(record.address);
                    setMode("unlock");
                  }}
                >
                  Unlock
                </Button>
                <Button compact onPress={() => setConfirming(record.address)}>
                  Forget
                </Button>
              </View>
              {confirming === record.address && (
                <View style={styles.confirmRow}>
                  <Text variant="bodySmall" style={styles.muted}>
                    Removing the encrypted keyfile leaves this browser with no copy. Without the
                    download or the 24 words the account cannot be reached again.
                  </Text>
                  <View style={styles.walletActions}>
                    <Button compact mode="contained" onPress={() => void props.onForget(record.address)}>
                      Remove it
                    </Button>
                    <Button compact onPress={() => setConfirming(null)}>
                      Keep it
                    </Button>
                  </View>
                </View>
              )}
            </View>
          ))
        )}

        <View style={styles.actions}>
          <Button mode="contained" onPress={() => setMode("create")}>
            Create a wallet
          </Button>
          <Button mode="outlined" onPress={() => setMode("restore")}>
            Restore 24 words
          </Button>
          <Button mode="outlined" onPress={() => setMode("open")}>
            Open a keyfile
          </Button>
        </View>
        <Text variant="bodySmall" style={styles.muted}>
          A keyfile is the file <Text style={styles.mono}>sccp wallet new</Text> writes. Its seed is
          encrypted with scrypt (n = {formatInteger(2 ** 17)}) and AES-256-GCM before this browser
          stores it; the passphrase is never stored.
        </Text>
      </Section>

      {mode === "unlock" && selected !== null && (
        <UnlockPanel
          record={selected}
          onCancel={cancel}
          onUnlock={props.onUnlock}
        />
      )}

      {mode === "create" && (
        <CreatePanel
          network={props.network}
          onCancel={cancel}
          onCreated={props.onCreated}
        />
      )}

      {mode === "restore" && (
        <RestorePanel
          network={props.network}
          onCancel={cancel}
          onCreated={props.onCreated}
        />
      )}

      {mode === "open" && <OpenPanel onCancel={cancel} onImport={props.onImport} />}
    </>
  );
}

function PassphraseFields({
  passphrase,
  setPassphrase,
  confirm,
  setConfirm,
  confirmLabel,
}: {
  passphrase: string;
  setPassphrase: (value: string) => void;
  confirm: string;
  setConfirm: (value: string) => void;
  confirmLabel: string;
}): ReactNode {
  const short = passphrase.length > 0 && passphrase.length < MIN_PASSPHRASE_CHARS;
  const mismatch = confirm.length > 0 && confirm !== passphrase;
  return (
    <>
      <TextInput
        label="Passphrase"
        mode="outlined"
        secureTextEntry
        autoCapitalize="none"
        autoCorrect={false}
        value={passphrase}
        onChangeText={setPassphrase}
      />
      <HelperText type={short ? "error" : "info"} visible={passphrase.length > 0}>
        {short
          ? `at least ${MIN_PASSPHRASE_CHARS} characters`
          : "Long and random; it is never stored, so losing it means restoring from the 24 words."}
      </HelperText>
      <TextInput
        label={confirmLabel}
        mode="outlined"
        secureTextEntry
        autoCapitalize="none"
        autoCorrect={false}
        value={confirm}
        onChangeText={setConfirm}
      />
      <HelperText type="error" visible={mismatch}>
        the two passphrases differ
      </HelperText>
    </>
  );
}

function describe(failure: unknown): Error {
  return failure instanceof Error ? failure : new Error(String(failure));
}

/**
 * A checkbox drawn from primitives.
 *
 * Paper's `Checkbox` paints a Material icon, and this bundle ships no icon
 * library, so it would render as an empty, unreadable box. A filled square is the
 * state; `accessibilityRole="checkbox"` is what a screen reader announces.
 */
function CheckRow({
  checked,
  onPress,
  children,
}: {
  checked: boolean;
  onPress: () => void;
  children: string;
}): ReactNode {
  const { colors } = useTheme();
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="checkbox"
      accessibilityState={{ checked }}
      style={styles.checkRow}
    >
      <View
        style={[
          styles.checkBox,
          { borderColor: colors.primary },
          checked && { backgroundColor: colors.primary },
        ]}
      />
      <Text variant="bodyMedium">{children}</Text>
    </Pressable>
  );
}

function CreatePanel({
  network,
  onCancel,
  onCreated,
}: {
  network: string | null;
  onCancel: () => void;
  onCreated: (document: KeyfileDocument, keypair: HybridKeypair) => Promise<void>;
}): ReactNode {
  const [passphrase, setPassphrase] = useState("");
  const [confirm, setConfirm] = useState("");
  const [pending, setPending] = useState<{
    document: KeyfileDocument;
    keypair: HybridKeypair;
    phrase: string;
  } | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const ready =
    passphrase.length >= MIN_PASSPHRASE_CHARS && passphrase === confirm && network !== null;

  const generate = async (): Promise<void> => {
    if (network === null) return;
    setBusy(true);
    setError(null);
    try {
      const { document, keypair } = await createKeyfile({ network, passphrase });
      setPending({ document, keypair, phrase: seedToMnemonic(keypair.seed) });
    } catch (failure) {
      setError(describe(failure));
    } finally {
      setBusy(false);
    }
  };

  const finish = async (): Promise<void> => {
    if (pending === null) return;
    setBusy(true);
    setError(null);
    try {
      await onCreated(pending.document, pending.keypair);
    } catch (failure) {
      setError(describe(failure));
      setBusy(false);
    }
  };

  if (pending !== null) {
    return (
      <Section
        title="Write down your backup"
        subtitle="24 words, in this order: the only way back if this browser loses the keyfile"
      >
        <View style={styles.words}>
          {pending.phrase.split(" ").map((word, index) => (
            <Text key={`${index}-${word}`} variant="bodyMedium" style={styles.word}>
              {index + 1}. {word}
            </Text>
          ))}
        </View>
        <HelperText type="info" visible>
          These words are the key. Anyone who reads them owns this wallet, and this page will not
          show them again.
        </HelperText>
        <View style={styles.actions}>
          <Button
            mode="outlined"
           
            onPress={() =>
              downloadText(keyfileName(pending.document.address), keyfileText(pending.document))
            }
          >
            Download keyfile
          </Button>
          <Button onPress={() => setPending(null)} disabled={busy}>
            Back
          </Button>
        </View>
        <CheckRow checked={acknowledged} onPress={() => setAcknowledged(!acknowledged)}>
          I wrote the 24 words down
        </CheckRow>
        {error !== null && (
          <HelperText type="error" visible>
            {error.message}
          </HelperText>
        )}
        <View style={styles.actions}>
          <Button
            mode="contained"
            loading={busy}
            disabled={!acknowledged || busy}
            onPress={() => void finish()}
          >
            Save in this browser
          </Button>
          <Button onPress={onCancel} disabled={busy}>
            Cancel
          </Button>
        </View>
      </Section>
    );
  }

  return (
    <Section
      title="Create a wallet"
      subtitle={network === null ? "waiting for the node" : `${network} · scrypt n = 2^17`}
    >
      {network === null ? (
        <HelperText type="error" visible>
          The node has not reported its network yet. A keyfile names a network, so there is nothing
          safe to create until it does.
        </HelperText>
      ) : (
        <Text variant="bodySmall" style={styles.muted}>
          The key is generated in this tab from the browser&apos;s CSPRNG and encrypted immediately.
        </Text>
      )}
      <PassphraseFields
        passphrase={passphrase}
        setPassphrase={setPassphrase}
        confirm={confirm}
        setConfirm={setConfirm}
        confirmLabel="Repeat passphrase"
      />
      {error !== null && (
        <HelperText type="error" visible>
          {error.message}
        </HelperText>
      )}
      <View style={styles.actions}>
        <Button
          mode="contained"
          loading={busy}
          disabled={!ready || busy}
          onPress={() => void generate()}
        >
          Generate
        </Button>
        <Button onPress={onCancel} disabled={busy}>
          Cancel
        </Button>
      </View>
    </Section>
  );
}

function UnlockPanel({
  record,
  onCancel,
  onUnlock,
}: {
  record: StoredKeyfile;
  onCancel: () => void;
  onUnlock: (record: StoredKeyfile, passphrase: string) => Promise<void>;
}): ReactNode {
  const [passphrase, setPassphrase] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const submit = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      await onUnlock(record, passphrase);
    } catch (failure) {
      setError(describe(failure));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section title="Unlock" subtitle={`${record.network} · ${record.address}`}>
      <TextInput
        label="Passphrase"
        mode="outlined"
        secureTextEntry
        autoCapitalize="none"
        autoCorrect={false}
        value={passphrase}
        onChangeText={setPassphrase}
        onSubmitEditing={() => void submit()}
      />
      {error !== null && (
        <HelperText type="error" visible>
          {error.message}
        </HelperText>
      )}
      <View style={styles.actions}>
        <Button
          mode="contained"
          loading={busy}
          disabled={busy || passphrase.length === 0}
          onPress={() => void submit()}
        >
          Unlock
        </Button>
        <Button onPress={onCancel} disabled={busy}>
          Cancel
        </Button>
      </View>
      <HelperText type="info" visible>
        scrypt runs in this tab: 128 MiB of memory and a moment, which is what makes guessing the
        passphrase expensive.
      </HelperText>
    </Section>
  );
}

function RestorePanel({
  network,
  onCancel,
  onCreated,
}: {
  network: string | null;
  onCancel: () => void;
  onCreated: (document: KeyfileDocument, keypair: HybridKeypair) => Promise<void>;
}): ReactNode {
  const [phrase, setPhrase] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const words = phrase.trim().split(/\s+/).filter((word) => word.length > 0).length;
  const ready =
    network !== null &&
    words === 24 &&
    passphrase.length >= MIN_PASSPHRASE_CHARS &&
    passphrase === confirm;

  const restore = async (): Promise<void> => {
    if (network === null) return;
    setBusy(true);
    setError(null);
    try {
      const seed = mnemonicToSeed(phrase);
      const { document, keypair } = await createKeyfile({ network, passphrase, seed });
      await onCreated(document, keypair);
    } catch (failure) {
      setError(describe(failure));
      setBusy(false);
    }
  };

  return (
    <Section
      title="Restore from 24 words"
      subtitle={network === null ? "waiting for the node" : `${network} · the phrase is the seed`}
    >
      <TextInput
        label="Backup phrase"
        mode="outlined"
        multiline
        numberOfLines={3}
        autoCapitalize="none"
        autoCorrect={false}
        value={phrase}
        onChangeText={setPhrase}
        style={styles.phraseInput}
      />
      <HelperText type={words === 24 ? "info" : "error"} visible>
        {words === 24
          ? "24 words: the phrase restores both halves of the key, without a derivation path."
          : `${words} of 24 words`}
      </HelperText>
      <PassphraseFields
        passphrase={passphrase}
        setPassphrase={setPassphrase}
        confirm={confirm}
        setConfirm={setConfirm}
        confirmLabel="Repeat passphrase"
      />
      <HelperText type="info" visible>
        Restoring writes a new keyfile around the same seed, with a fresh salt and nonce.
      </HelperText>
      {error !== null && (
        <HelperText type="error" visible>
          {error.message}
        </HelperText>
      )}
      <View style={styles.actions}>
        <Button
          mode="contained"
          loading={busy}
          disabled={!ready || busy}
          onPress={() => void restore()}
        >
          Restore
        </Button>
        <Button onPress={onCancel} disabled={busy}>
          Cancel
        </Button>
      </View>
    </Section>
  );
}

/**
 * Bringing in a keyfile that exists somewhere else: the file `sccp wallet new`
 * wrote, or its JSON.
 *
 * Both routes end in the same text field, because the field is the review step —
 * what is imported is what is on screen, and the keyfile is a small document a
 * person can actually read. Choosing a file only fills the field; unlocking still
 * takes an explicit press and the passphrase, which is the expensive part (scrypt
 * at n = 2^17) and the one thing this tab must never do by accident.
 *
 * A dismissed picker resolves `null` and is not an error: nothing changed, so
 * nothing is reported.
 */
function OpenPanel({
  onCancel,
  onImport,
}: {
  onCancel: () => void;
  onImport: (document: KeyfileDocument, keypair: HybridKeypair) => Promise<void>;
}): ReactNode {
  const [text, setText] = useState("");
  const [filename, setFilename] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  const [passphrase, setPassphrase] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const choose = async (): Promise<void> => {
    setPicking(true);
    setError(null);
    try {
      const file = await pickTextFile();
      // `null` is a dismissed dialog: nothing was chosen and nothing is wrong.
      if (file === null) return;
      setText(await readTextFile(file));
      setFilename(file.name);
    } catch (failure) {
      setError(describe(failure));
    } finally {
      setPicking(false);
    }
  };

  const open = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      const document = parseKeyfile(text);
      const keypair = await unlockKeyfile(document, passphrase);
      await onImport(document, keypair);
    } catch (failure) {
      setError(describe(failure));
      setBusy(false);
    }
  };

  return (
    <Section
      title="Open a keyfile"
      subtitle="pick the file `sccp wallet new` wrote, or paste its JSON"
    >
      <View style={styles.actions}>
        <Button
          mode="contained-tonal"
          loading={picking}
          disabled={busy || picking}
          onPress={() => void choose()}
        >
          Choose a file…
        </Button>
        {filename !== null ? (
          <Text variant="bodySmall" numberOfLines={1} style={styles.fileNote}>
            read <Text style={styles.mono}>{filename}</Text>
          </Text>
        ) : (
          <Text variant="bodySmall" style={styles.fileNote}>
            or paste it below
          </Text>
        )}
      </View>
      <TextInput
        label="Keyfile JSON"
        mode="outlined"
        multiline
        numberOfLines={6}
        autoCapitalize="none"
        autoCorrect={false}
        value={text}
        onChangeText={(value) => {
          setText(value);
          setFilename(null);
        }}
        style={styles.keyfileInput}
      />
      <TextInput
        label="Passphrase"
        mode="outlined"
        secureTextEntry
        autoCapitalize="none"
        autoCorrect={false}
        value={passphrase}
        onChangeText={setPassphrase}
      />
      <HelperText type="info" visible>
        The file is read in this tab and never uploaded. Parsing validates the header, the cipher
        and the salt; unlocking decrypts the seed and checks that it really derives the address the
        file names.
      </HelperText>
      {error !== null && (
        <HelperText type="error" visible>
          {error.message}
        </HelperText>
      )}
      <View style={styles.actions}>
        <Button
          mode="contained"
          loading={busy}
          disabled={busy || text.trim().length === 0 || passphrase.length === 0}
          onPress={() => void open()}
        >
          Open
        </Button>
        <Button onPress={onCancel} disabled={busy || picking}>
          Cancel
        </Button>
      </View>
    </Section>
  );
}

const styles = StyleSheet.create({
  muted: { opacity: 0.66 },
  mono: { fontFamily: monospace, fontSize: 13 },
  walletRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 12,
    flexWrap: "wrap",
    paddingVertical: 8,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: "rgba(128,128,128,0.32)",
  },
  walletMain: { gap: 2, flexShrink: 1 },
  walletLabel: { fontWeight: "600" },
  walletActions: { flexDirection: "row", alignItems: "center", gap: 8, flexWrap: "wrap" },
  confirmRow: { flexBasis: "100%", gap: 6, paddingBottom: 6 },
  checkRow: { flexDirection: "row", alignItems: "center", gap: 10, paddingVertical: 6 },
  checkBox: { width: 20, height: 20, borderRadius: 4, borderWidth: 2 },
  actions: { flexDirection: "row", alignItems: "center", gap: 8, flexWrap: "wrap" },
  fileNote: { flexShrink: 1, opacity: 0.66 },
  words: { flexDirection: "row", flexWrap: "wrap", gap: 8, paddingVertical: 4 },
  word: {
    fontFamily: monospace,
    backgroundColor: "rgba(128,128,128,0.14)",
    borderRadius: 8,
    paddingVertical: 4,
    paddingHorizontal: 8,
    minWidth: 150,
  },
  phraseInput: { minHeight: 96 },
  keyfileInput: { minHeight: 160, fontFamily: monospace },
});

