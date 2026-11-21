import { useEffect, useState, type ReactNode } from "react";
import { View } from "react-native";
import { Button, Divider, HelperText, SegmentedButtons, Text, TextInput } from "react-native-paper";

import { EmptyState, FailureNotice, KeyValue, Section, StatusPill } from "../../components/Chrome";
import { tokenBalance, tokenForAsset } from "../../lib/assets";
import { addedShares, proportionalAmount, removedAmounts, sharePercent } from "../../lib/dex";
import { formatInteger, formatTokenAmount } from "../../lib/format";
import { hybridPublicKeyHex } from "../../lib/keys";
import { buildAddLiquidity, buildRemoveLiquidity } from "../../lib/tx";
import { amountOrNull, dexStyles as styles, pairLabel, PoolPicker } from "./common";
import type { DeskProps } from "./types";

export function LiquidityPanel({
  address,
  network,
  keypair,
  account,
  params,
  pools,
  tokens,
  nonce,
  onReview,
}: DeskProps): ReactNode {
  const [selectedId, setSelectedId] = useState(() => {
    const own = pools.find((pool) => (pool.shares[address] ?? 0) > 0);
    return own?.pool_id ?? pools[0]?.pool_id ?? "";
  });
  const [mode, setMode] = useState<"add" | "remove">("add");
  const [addAmountText, setAddAmountText] = useState("");
  const [removePercent, setRemovePercent] = useState(50);
  const selected = pools.find((pool) => pool.pool_id === selectedId) ?? pools[0];

  useEffect(() => {
    if (selected === undefined) {
      setSelectedId("");
      return;
    }
    if (selected.pool_id !== selectedId) setSelectedId(selected.pool_id);
  }, [selected, selectedId]);

  useEffect(() => {
    setAddAmountText("");
    setRemovePercent(50);
  }, [selectedId]);

  if (pools.length === 0 || selected === undefined) {
    return (
      <Section title="Liquidity" subtitle="no pools on this chain yet">
        <EmptyState>Create a pool first; your LP position will appear here.</EmptyState>
      </Section>
    );
  }

  const tokenA = tokenForAsset(tokens, selected.asset_a);
  const tokenB = tokenForAsset(tokens, selected.asset_b);
  if (tokenA === undefined || tokenB === undefined) {
    return <FailureNotice error={new Error("the pool names a token this network does not expose")} />;
  }
  const position = selected.shares[address] ?? 0;
  const underlying =
    position > 0
      ? removedAmounts(selected.reserve_a, selected.reserve_b, selected.total_shares, position)
      : { amountA: 0, amountB: 0 };
  const fee = params.min_fee;

  let addAmountA = 0;
  let parseFailure: string | null = null;
  try {
    addAmountA = amountOrNull(addAmountText, tokenA) ?? 0;
  } catch (failure) {
    parseFailure = (failure as Error).message;
  }
  const addAmountB =
    addAmountA > 0 ? proportionalAmount(addAmountA, selected.reserve_a, selected.reserve_b) : 0;
  const newShares =
    addAmountA > 0 && addAmountB > 0
      ? addedShares(
          selected.reserve_a,
          selected.reserve_b,
          selected.total_shares,
          addAmountA,
          addAmountB,
        )
      : 0;
  const removeShares =
    position === 0
      ? 0
      : removePercent === 100
        ? position
        : Number((BigInt(position) * BigInt(removePercent)) / 100n);
  const removal =
    removeShares > 0
      ? removedAmounts(selected.reserve_a, selected.reserve_b, selected.total_shares, removeShares)
      : { amountA: 0, amountB: 0 };

  const addProblem = ((): string | null => {
    if (mode !== "add") return null;
    if (parseFailure !== null) return parseFailure;
    if (addAmountText.trim() === "") return "enter the first side of the deposit";
    if (addAmountA <= 0 || addAmountB <= 0) return "the deposit is too small";
    if (newShares <= 0) return "the deposit is too small to mint a share";
    const nativeNeeded =
      (tokenA.native ? addAmountA : 0) + (tokenB.native ? addAmountB : 0) + fee;
    if (nativeNeeded > account.balance) {
      return `the deposit needs ${formatTokenAmount(nativeNeeded, "ZQWX")} including the fee`;
    }
    if (!tokenA.native && addAmountA > tokenBalance(account, tokenA)) {
      return `${tokenA.symbol} balance is too small`;
    }
    if (!tokenB.native && addAmountB > tokenBalance(account, tokenB)) {
      return `${tokenB.symbol} balance is too small`;
    }
    return null;
  })();
  const removeProblem = ((): string | null => {
    if (mode !== "remove") return null;
    if (position === 0) return "this wallet holds no shares in the pool";
    if (removeShares <= 0) return "that percentage redeems no shares";
    if (removal.amountA <= 0 || removal.amountB <= 0) {
      return "the redemption is too small to pay both sides";
    }
    if (account.balance < fee) return "the wallet needs ZQWX for the network fee";
    return null;
  })();

  const review = (): void => {
    if (mode === "add") {
      onReview({
        tx: buildAddLiquidity({
          network,
          sender: address,
          nonce,
          fee,
          poolId: selected.pool_id,
          amountA: addAmountA,
          amountB: addAmountB,
          publicKeyHex: hybridPublicKeyHex(keypair),
        }),
        title: `Add liquidity to ${pairLabel(selected, tokens)}`,
        rows: [
          ["Pool", pairLabel(selected, tokens)],
          [
            "Deposit",
            `${formatTokenAmount(addAmountA, tokenA.symbol, tokenA.decimals)} + ${formatTokenAmount(addAmountB, tokenB.symbol, tokenB.decimals)}`,
          ],
          ["LP shares", formatInteger(newShares)],
          ["Fee", formatTokenAmount(fee, "ZQWX")],
          ["Nonce", formatInteger(nonce)],
        ],
      });
      return;
    }
    onReview({
      tx: buildRemoveLiquidity({
        network,
        sender: address,
        nonce,
        fee,
        poolId: selected.pool_id,
        shares: removeShares,
        publicKeyHex: hybridPublicKeyHex(keypair),
      }),
      title: `Remove ${removePercent}% of ${pairLabel(selected, tokens)}`,
      rows: [
        ["Pool", pairLabel(selected, tokens)],
        ["Shares redeemed", formatInteger(removeShares)],
        [
          "Receive",
          `${formatTokenAmount(removal.amountA, tokenA.symbol, tokenA.decimals)} + ${formatTokenAmount(removal.amountB, tokenB.symbol, tokenB.decimals)}`,
        ],
        ["Fee", formatTokenAmount(fee, "ZQWX")],
        ["Nonce", formatInteger(nonce)],
      ],
    });
  };

  const problem = mode === "add" ? addProblem : removeProblem;


  return (
    <Section title="Liquidity" subtitle="pool status and this wallet's position">
      <PoolPicker pools={pools} value={selected.pool_id} onChange={setSelectedId} tokens={tokens} />
      <View style={styles.position}>
        <View style={styles.positionText}>
          <Text variant="titleSmall">{pairLabel(selected, tokens)}</Text>
          <Text variant="bodySmall" style={styles.muted}>
            {formatTokenAmount(selected.reserve_a, tokenA.symbol, tokenA.decimals)} /{" "}
            {formatTokenAmount(selected.reserve_b, tokenB.symbol, tokenB.decimals)} reserves ·{" "}
            {selected.fee_bps / 100}% LP fee
          </Text>
        </View>
        <StatusPill
          label={
            position > 0
              ? `${sharePercent(position, selected.total_shares).toFixed(2)}% owned`
              : "no position"
          }
          tone={position > 0 ? "good" : "neutral"}
        />
      </View>
      <KeyValue label="Total LP shares" mono>
        {formatInteger(selected.total_shares)}
      </KeyValue>
      <KeyValue label="Your shares" mono>
        {formatInteger(position)}
      </KeyValue>
      {position > 0 && (
        <KeyValue label="Your underlying" mono>
          {formatTokenAmount(underlying.amountA, tokenA.symbol, tokenA.decimals)} +{" "}
          {formatTokenAmount(underlying.amountB, tokenB.symbol, tokenB.decimals)}
        </KeyValue>
      )}
      <KeyValue label="Price" mono>
        1 {tokenA.symbol} = {(selected.reserve_b / selected.reserve_a).toFixed(6)} {tokenB.symbol}
      </KeyValue>
      <Divider />
      <SegmentedButtons
        value={mode}
        onValueChange={(value) => setMode(value as "add" | "remove")}
        buttons={[
          { value: "add", label: "Add" },
          { value: "remove", label: "Remove" },
        ]}
      />
      {mode === "add" ? (
        <>
          <TextInput
            label={`Amount of ${tokenA.symbol}`}
            mode="outlined"
            value={addAmountText}
            onChangeText={setAddAmountText}
            keyboardType="decimal-pad"
            right={<TextInput.Affix text={tokenA.symbol} />}
          />
          <HelperText type="info" visible>
            {formatTokenAmount(addAmountB, tokenB.symbol, tokenB.decimals)} of {tokenB.symbol}{" "}
            keeps the current ratio. Spendable:{" "}
            {formatTokenAmount(tokenBalance(account, tokenA), tokenA.symbol, tokenA.decimals)} /{" "}
            {formatTokenAmount(tokenBalance(account, tokenB), tokenB.symbol, tokenB.decimals)}.
          </HelperText>
          <KeyValue label="Shares minted" mono>
            {formatInteger(newShares)}
          </KeyValue>
        </>
      ) : (
        <>
          <Text variant="bodySmall" style={styles.muted}>
            Redeem {removePercent}% of your shares
          </Text>
          <SegmentedButtons
            value={String(removePercent)}
            onValueChange={(value) => setRemovePercent(Number(value))}
            buttons={[
              { value: "25", label: "25%" },
              { value: "50", label: "50%" },
              { value: "75", label: "75%" },
              { value: "100", label: "Max" },
            ]}
          />
          <KeyValue label="Receive" mono>
            {formatTokenAmount(removal.amountA, tokenA.symbol, tokenA.decimals)} +{" "}
            {formatTokenAmount(removal.amountB, tokenB.symbol, tokenB.decimals)}
          </KeyValue>
        </>
      )}
      {problem !== null && (
        <HelperText type="error" visible>
          {problem}
        </HelperText>
      )}
      <Button mode="contained" disabled={problem !== null} onPress={review}>
        {mode === "add" ? "Review deposit" : "Review withdrawal"}
      </Button>
    </Section>
  );
}

