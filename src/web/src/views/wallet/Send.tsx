/**
 * Sending SCCP: build, review, sign, submit.
 *
 * The review step is not decoration. It shows every field the signature covers,
 * plus the txid those fields hash to, so what is approved is what is signed --
 * the browser generates the transaction, and nothing is sent until the person
 * has seen the amount, the fee and the recipient in full.
 *
 * The nonce is the one piece of state a wallet must keep locally:
 * `GET /v1/accounts/{address}` reports the *committed* nonce, so two transfers
 * submitted before the first is mined would both claim the same one and the
 * second would be rejected. The caller passes a `pendingNonce` that it advances
 * on every accepted transaction and clears once the chain catches up.
 */

import { useMemo, useState, type ReactNode } from "react";
import { StyleSheet, View } from "react-native";
import { Button, Divider, HelperText, SegmentedButtons, Text, TextInput } from "react-native-paper";

import { submitTransaction } from "../../api";
import { KeyValue, Mono, Section } from "../../components/Chrome";
import { useNotice } from "../../components/Notices";
import {
  tokenBalance,
  tokenForVariant,
  type Token,
} from "../../lib/assets";
import { addressHrp } from "../../lib/bech32";
import { labelFor, readContacts, saveContact } from "../../lib/contacts";
import {
  formatAmount,
  formatInteger,
  formatTokenAmount,
  parseAmount,
  parseTokenAmount,
  shortAddress,
} from "../../lib/format";
import { type HybridKeypair, hybridPublicKeyHex } from "../../lib/keys";
import type { PendingTransaction } from "../../lib/pending";
import {
  buildTransfer,
  signTransaction,
  toWireTransaction,
  txId,
  type Transaction,
} from "../../lib/tx";
import { monospace } from "../../theme";
import type { AccountView } from "../../types";

interface SendProps {
  keypair: HybridKeypair;
  address: string;
  network: string;
  account: AccountView;
  minFee: number;
  /** Every token this network settles, SCCP first. */
  tokens: Token[];
  /** The next unused nonce, or `null` while the chain is authoritative. */
  pendingNonce: number | null;
  onAccepted: (sent: PendingTransaction) => void;
  onPendingNonce: (nonce: number | null) => void;
}

function feeText(minFee: number): string {
  return formatAmount(minFee).replace(" SCCP", "").replace(/,/g, "");
}

export function SendPanel({
  keypair,
  address,
  network,
  account,
  minFee,
  tokens,
  pendingNonce,
  onAccepted,
  onPendingNonce,
}: SendProps): ReactNode {
  const [to, setTo] = useState("");
  const [amount, setAmount] = useState("");
  const [fee, setFee] = useState(() => feeText(minFee));
  const [variant, setVariant] = useState(() => tokens[0]?.variant ?? "standard");
  const [review, setReview] = useState<Transaction | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const [nickname, setNickname] = useState("");
  const [contacts, setContacts] = useState(() => readContacts());
  const notify = useNotice();

  const token = tokenForVariant(tokens, variant) ?? tokens[0];
  const nonce = pendingNonce ?? account.nonce;
  const spendable = token === undefined ? 0 : tokenBalance(account, token);
  const nativeSpendable = account.balance;

  const parsed = useMemo(() => {
    try {
      // An empty amount is "not typed yet", not a malformed number: the field
      // must not show a format error before the first keystroke.
      const amountUnits =
        amount.trim() === "" || token === undefined
          ? 0
          : parseTokenAmount(amount, token.symbol, token.decimals);
      return { amount: amountUnits, fee: parseAmount(fee), failure: null as string | null };
    } catch (failure) {
      return { amount: 0, fee: 0, failure: (failure as Error).message };
    }
  }, [amount, fee, token]);

  const recipient = to.trim();
  const known = labelFor(contacts, recipient);
  const reviewName = known ?? (nickname.trim() === "" ? null : nickname.trim());
  const problem = ((): string | null => {
    if (parsed.failure !== null) return parsed.failure;
    if (token === undefined) return "this network exposes no token to send";
    if (recipient === "") return "enter a recipient address";
    if (!recipient.startsWith(`${addressHrp(network)}1`)) {
      return `a ${network} address starts with ${addressHrp(network)}1`;
    }
    if (recipient === address) return "a transfer must name a different recipient";
    if (amount.trim() === "") return "enter an amount";
    if (parsed.amount <= 0) return "a transfer amount must be positive";
    if (parsed.fee < minFee) {
      return `the fee is below the network minimum of ${formatAmount(minFee)}`;
    }
    if (parsed.fee > nativeSpendable) {
      return `the fee is ${formatAmount(parsed.fee)}, but only ${formatAmount(
        nativeSpendable,
      )} SCCP is spendable`;
    }
    if (token.native) {
      if (parsed.amount + parsed.fee > spendable) {
        return `spendable balance is ${formatAmount(spendable)}; this needs ${formatAmount(
          parsed.amount + parsed.fee,
        )} including the fee`;
      }
    } else if (parsed.amount > spendable) {
      return `${token.symbol} balance is ${formatTokenAmount(
        spendable,
        token.symbol,
        token.decimals,
      )}; this transfer needs ${formatTokenAmount(
        parsed.amount,
        token.symbol,
        token.decimals,
      )} plus its SCCP fee`;
    }
    return null;
  })();

  const prepare = (): void => {
    setError(null);
    setReview(
      buildTransfer({
        network,
        sender: address,
        nonce,
        fee: parsed.fee,
        to: recipient,
        amount: parsed.amount,
        asset: token?.asset ?? null,
        publicKeyHex: hybridPublicKeyHex(keypair),
      }),
    );
  };

  const submit = async (): Promise<void> => {
    if (review === null) return;
    setBusy(true);
    setError(null);
    try {
      const signed = signTransaction(keypair, review);
      const accepted = await submitTransaction(toWireTransaction(signed));
      // A nickname typed for a first transfer is saved with it: the person just
      // named the recipient, and asking again on the next send would be silly.
      if (nickname.trim() !== "") {
        setContacts(saveContact(String(review.payload.to), network, nickname));
      }
      onAccepted({
        txid: accepted.txid,
        to: String(review.payload.to),
        amount: parsed.amount,
        asset: token?.asset ?? null,
        symbol: token?.symbol ?? "SCCP",
        fee: parsed.fee,
        nonce: signed.nonce,
        submittedAt: Date.now(),
      });
      onPendingNonce(signed.nonce + 1);
      setReview(null);
      setTo("");
      setAmount("");
      setNickname("");
    } catch (failure) {
      setError(failure instanceof Error ? failure : new Error(String(failure)));
      // The node refused it: this nonce is still unused, but an earlier accepted
      // transaction may have consumed the ones below it, so keep the optimistic
      // counter unless the chain has already caught up with it.
      onPendingNonce(review.nonce > account.nonce ? review.nonce : null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section
      title="Send"
      subtitle={`${formatTokenAmount(
        spendable,
        token?.symbol ?? "SCCP",
        token?.decimals ?? 8,
      )} spendable · next nonce ${formatInteger(nonce)}`}
    >
      {tokens.length > 1 && review === null && (
        <SegmentedButtons
          value={variant}
          onValueChange={setVariant}
          buttons={tokens.map((candidate) => ({
            value: candidate.variant,
            label: candidate.symbol,
          }))}
        />
      )}
      <TextInput
        label="Recipient"
        mode="outlined"
        autoCapitalize="none"
        autoCorrect={false}
        value={to}
        onChangeText={setTo}
        style={styles.monoInput}
      />
      <View style={styles.row}>
        <TextInput
          label="Nickname for this recipient (optional)"
          mode="outlined"
          value={nickname}
          onChangeText={setNickname}
          placeholder={known ?? "no nickname yet"}
          style={styles.half}
          autoCapitalize="words"
        />
        <View style={styles.actions}>
          <Button
            compact
            mode="text"
            disabled={recipient === "" || nickname.trim() === "" || nickname.trim() === known}
            onPress={() => {
              setContacts(saveContact(recipient, network, nickname));
              notify(`nickname saved for ${shortAddress(recipient)}`);
              setNickname("");
            }}
          >
            Save nickname
          </Button>
        </View>
      </View>
      {known !== null && (
        <Text variant="bodySmall" style={styles.muted}>
          {known} is the nickname this browser keeps for that address.
        </Text>
      )}
      <View style={styles.row}>
        <TextInput
          label={`Amount (${token?.symbol ?? "SCCP"})`}
          mode="outlined"
          keyboardType="decimal-pad"
          value={amount}
          onChangeText={setAmount}
          style={styles.half}
        />
        <TextInput
          label="Fee (SCCP)"
          mode="outlined"
          keyboardType="decimal-pad"
          value={fee}
          onChangeText={setFee}
          style={styles.half}
        />
      </View>
      <View style={styles.actions}>
        {[1, 2, 5].map((factor) => (
          <Button
            key={factor}
            compact
            mode="text"
            onPress={() => setFee(feeText(minFee * factor))}
          >
            {factor === 1 ? "Minimum fee" : `${factor}× minimum`}
          </Button>
        ))}
      </View>
      <HelperText type={problem === null ? "info" : "error"} visible>
        {problem ??
          `Up to ${token?.decimals ?? 8} decimals. The fee is always paid in SCCP; the network rejects anything below ${formatAmount(
            minFee,
          )}.`}
      </HelperText>
      {error !== null && review === null && (
        <HelperText type="error" visible>
          {error.message}
        </HelperText>
      )}
      <View style={styles.actions}>
        <Button mode="contained" disabled={problem !== null || review !== null} onPress={prepare}>
          Review
        </Button>
      </View>

      {review !== null && (
        <>
          <Divider />
          <Text variant="titleMedium">Review</Text>
          <Text variant="bodySmall" style={styles.muted}>
            the signature covers exactly these fields
          </Text>
          <KeyValue label="Network" mono>
            {review.network}
          </KeyValue>
          <KeyValue label="From" mono>
            {review.from}
          </KeyValue>
          <KeyValue label="To">
            {reviewName !== null && <Text variant="bodyMedium">{reviewName}</Text>}
            <Mono>{String(review.payload.to)}</Mono>
          </KeyValue>
          <KeyValue label="Asset" mono>
            {review.payload.asset === undefined ? "SCCP (native)" : (token?.symbol ?? "variant asset")}
          </KeyValue>
          <KeyValue label="Amount" mono>
            {formatTokenAmount(
              Number(review.payload.amount),
              token?.symbol ?? "SCCP",
              token?.decimals ?? 8,
            )}
          </KeyValue>
          <KeyValue label="Fee" mono>
            {formatAmount(review.fee)}
          </KeyValue>
          <KeyValue label="Nonce" mono>
            {formatInteger(review.nonce)}
          </KeyValue>
          <KeyValue label="Txid" mono>
            {txId(review)}
          </KeyValue>
          <Text variant="bodySmall" style={styles.muted}>
            from {shortAddress(review.from)} to {shortAddress(String(review.payload.to))}: both key
            halves sign it in this tab, and no seed leaves it.
          </Text>
          {error !== null && (
            <HelperText type="error" visible>
              {error.message}
            </HelperText>
          )}
          <View style={styles.actions}>
            <Button mode="contained" loading={busy} disabled={busy} onPress={() => void submit()}>
              Sign and send
            </Button>
            <Button disabled={busy} onPress={() => setReview(null)}>
              Edit
            </Button>
          </View>
        </>
      )}
      <HelperText type="info" visible>
        The node&apos;s account view reports committed state, so a second transfer before the first
        is mined uses the next nonce from this tab, not the one on chain.
      </HelperText>
    </Section>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: "row", gap: 12, flexWrap: "wrap" },
  half: { flexGrow: 1, minWidth: 180 },
  monoInput: { fontFamily: monospace },
  actions: { flexDirection: "row", alignItems: "center", gap: 8, flexWrap: "wrap" },
  muted: { opacity: 0.66 },
});
