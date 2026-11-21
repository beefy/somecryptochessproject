import { useEffect, useState, type ReactNode } from "react";
import { Button, HelperText, SegmentedButtons, Text, TextInput } from "react-native-paper";

import { EmptyState, FailureNotice, KeyValue, Section } from "../../components/Chrome";
import { tokenBalance, tokenForAsset } from "../../lib/assets";
import { swapOutput } from "../../lib/dex";
import { formatInteger, formatTokenAmount } from "../../lib/format";
import { hybridPublicKeyHex } from "../../lib/keys";
import { buildSwap } from "../../lib/tx";
import {
  amountOrNull,
  dexStyles as styles,
  minAfterSlippage,
  pairLabel,
  PoolPicker,
} from "./common";
import type { DeskProps } from "./types";

export function SwapPanel({
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
  const [selectedId, setSelectedId] = useState(() => pools[0]?.pool_id ?? "");
  const [direction, setDirection] = useState<"a-b" | "b-a">("a-b");
  const [amountText, setAmountText] = useState("");
  const [slippageBps, setSlippageBps] = useState(50);
  const selected = pools.find((pool) => pool.pool_id === selectedId) ?? pools[0];

  useEffect(() => {
    if (selected === undefined) {
      setSelectedId("");
      return;
    }
    if (selected.pool_id !== selectedId) setSelectedId(selected.pool_id);
  }, [selected, selectedId]);

  if (pools.length === 0 || selected === undefined) {
    return (
      <Section title="Swap" subtitle="no pools on this chain yet">
        <EmptyState>Create the first pool, then swaps can route through it.</EmptyState>
      </Section>
    );
  }

  const tokenA = tokenForAsset(tokens, selected.asset_a);
  const tokenB = tokenForAsset(tokens, selected.asset_b);
  if (tokenA === undefined || tokenB === undefined) {
    return <FailureNotice error={new Error("the pool names a token this network does not expose")} />;
  }
  const fromA = direction === "a-b";
  const tokenIn = fromA ? tokenA : tokenB;
  const tokenOut = fromA ? tokenB : tokenA;
  const assetIn = fromA ? selected.asset_a : selected.asset_b;
  const reserveIn = fromA ? selected.reserve_a : selected.reserve_b;
  const reserveOut = fromA ? selected.reserve_b : selected.reserve_a;
  const spendable = tokenBalance(account, tokenIn);

  let amountIn = 0;
  let parseFailure: string | null = null;
  try {
    amountIn = amountOrNull(amountText, tokenIn) ?? 0;
  } catch (failure) {
    parseFailure = (failure as Error).message;
  }
  const quote = amountIn > 0 ? swapOutput(reserveIn, reserveOut, amountIn, selected.fee_bps) : 0;
  const minimumOut = quote > 0 ? minAfterSlippage(quote, slippageBps) : 0;
  const fee = params.min_fee;
  const problem = ((): string | null => {
    if (parseFailure !== null) return parseFailure;
    if (amountText.trim() === "") return "enter an amount to swap";
    if (amountIn <= 0) return "the input amount must be positive";
    if (quote <= 0) return "that input is too small to produce an output";
    if (tokenIn.native && amountIn + fee > spendable) {
      return `this needs ${formatTokenAmount(amountIn + fee, "ZQWX")}; the wallet holds ${formatTokenAmount(spendable, "ZQWX")}`;
    }
    if (!tokenIn.native && amountIn > spendable) {
      return `${tokenIn.symbol} balance is ${formatTokenAmount(spendable, tokenIn.symbol, tokenIn.decimals)}`;
    }
    if (account.balance < fee) {
      return `the ZQWX fee is ${formatTokenAmount(fee, "ZQWX")}, but only ${formatTokenAmount(account.balance, "ZQWX")} is spendable`;
    }
    return null;
  })();

  const review = (): void => {
    onReview({
      tx: buildSwap({
        network,
        sender: address,
        nonce,
        fee,
        poolId: selected.pool_id,
        assetIn,
        amountIn,
        minAmountOut: minimumOut,
        publicKeyHex: hybridPublicKeyHex(keypair),
      }),
      title: `Swap ${tokenIn.symbol} for ${tokenOut.symbol}`,
      rows: [
        ["Pool", pairLabel(selected, tokens)],
        ["Pay", formatTokenAmount(amountIn, tokenIn.symbol, tokenIn.decimals)],
        ["Estimated receive", formatTokenAmount(quote, tokenOut.symbol, tokenOut.decimals)],
        ["Minimum receive", formatTokenAmount(minimumOut, tokenOut.symbol, tokenOut.decimals)],
        ["Slippage", `${slippageBps / 100}%`],
        ["Fee", formatTokenAmount(fee, "ZQWX")],
        ["Nonce", formatInteger(nonce)],
      ],
    });
  };


  return (
    <Section title="Swap" subtitle={`${pairLabel(selected, tokens)} · ${selected.fee_bps / 100}% LP fee`}>
      <PoolPicker pools={pools} value={selected.pool_id} onChange={setSelectedId} tokens={tokens} />
      <SegmentedButtons
        value={direction}
        onValueChange={(value) => setDirection(value as "a-b" | "b-a")}
        buttons={[
          { value: "a-b", label: `${tokenA.symbol} → ${tokenB.symbol}` },
          { value: "b-a", label: `${tokenB.symbol} → ${tokenA.symbol}` },
        ]}
      />
      <TextInput
        label={`Amount in ${tokenIn.symbol}`}
        mode="outlined"
        value={amountText}
        onChangeText={setAmountText}
        keyboardType="decimal-pad"
        placeholder="0.0"
        right={<TextInput.Affix text={tokenIn.symbol} />}
      />
      <HelperText type="info" visible>
        Spendable {formatTokenAmount(spendable, tokenIn.symbol, tokenIn.decimals)} · pool reserves{" "}
        {formatTokenAmount(reserveIn, tokenIn.symbol, tokenIn.decimals)} /{" "}
        {formatTokenAmount(reserveOut, tokenOut.symbol, tokenOut.decimals)}
      </HelperText>
      <KeyValue label="Estimated receive" mono>
        {formatTokenAmount(quote, tokenOut.symbol, tokenOut.decimals)}
      </KeyValue>
      <KeyValue label="Minimum receive" mono>
        {formatTokenAmount(minimumOut, tokenOut.symbol, tokenOut.decimals)}
      </KeyValue>
      <Text variant="bodySmall" style={styles.muted}>
        Slippage tolerance
      </Text>
      <SegmentedButtons
        value={String(slippageBps)}
        onValueChange={(value) => setSlippageBps(Number(value))}
        buttons={[
          { value: "10", label: "0.1%" },
          { value: "50", label: "0.5%" },
          { value: "100", label: "1%" },
        ]}
      />
      {problem !== null && (
        <HelperText type="error" visible>
          {problem}
        </HelperText>
      )}
      <Button mode="contained" disabled={problem !== null} onPress={review}>
        Review swap
      </Button>
      <HelperText type="info" visible>
        The network fee of {formatTokenAmount(fee, "ZQWX")} always comes from the ZQWX balance, even
        when the input is {tokenIn.symbol}.
      </HelperText>
    </Section>
  );
}
