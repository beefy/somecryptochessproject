import { useEffect, useState, type ReactNode } from "react";
import { Button, HelperText, Text, TextInput } from "react-native-paper";

import { EmptyState, KeyValue, Section } from "../../components/Chrome";
import { tokenBalance, tokenForAsset } from "../../lib/assets";
import { initialShares, poolId as derivePoolId } from "../../lib/dex";
import { formatInteger, formatTokenAmount } from "../../lib/format";
import { hybridPublicKeyHex } from "../../lib/keys";
import { buildCreatePool } from "../../lib/tx";
import { amountOrNull, dexStyles as styles, TokenPicker } from "./common";
import type { DeskProps } from "./types";

export function CreatePanel({
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
  const nonNative = tokens.find((token) => !token.native);
  const nonNativeAsset = nonNative?.asset ?? null;
  const [assetA, setAssetA] = useState<string | null>(null);
  const [assetB, setAssetB] = useState<string | null>(nonNativeAsset);
  const [amountA, setAmountA] = useState("");
  const [amountB, setAmountB] = useState("");

  useEffect(() => {
    if (nonNativeAsset === null) return;
    setAssetB((current) => (current === null ? nonNativeAsset : current));
  }, [nonNativeAsset]);

  const tokenA = tokenForAsset(tokens, assetA);
  const tokenB = tokenForAsset(tokens, assetB);

  const chooseA = (asset: string | null): void => {
    if (asset === assetB) setAssetB(assetA);
    setAssetA(asset);
  };
  const chooseB = (asset: string | null): void => {
    if (asset === assetA) setAssetA(assetB);
    setAssetB(asset);
  };

  if (tokenA === undefined || tokenB === undefined) {
    return (
      <Section title="Create pool" subtitle="a pair needs a variant token">
        <EmptyState>This network has no non-native token to pair with SCCP.</EmptyState>
      </Section>
    );
  }

  let parsedA = 0;
  let parsedB = 0;
  let parseFailure: string | null = null;
  try {
    parsedA = amountOrNull(amountA, tokenA) ?? 0;
    parsedB = amountOrNull(amountB, tokenB) ?? 0;
  } catch (failure) {
    parseFailure = (failure as Error).message;
  }
  const same = assetA === assetB;
  const existing = same
    ? undefined
    : pools.find((pool) => pool.pool_id === derivePoolId(assetA, assetB));
  const shares = !same && parsedA > 0 && parsedB > 0 ? initialShares(parsedA, parsedB) : 0;
  const fee = params.min_fee;
  const problem = ((): string | null => {
    if (parseFailure !== null) return parseFailure;
    if (same) return "choose two different tokens";
    if (existing !== undefined) return "that pool already exists; add liquidity instead";
    if (amountA.trim() === "" || amountB.trim() === "") return "enter both initial amounts";
    if (parsedA <= 0 || parsedB <= 0) return "both initial amounts must be positive";
    if (shares <= 0) return "the initial deposit is too small to mint a share";
    if (tokenA.native && parsedA + fee > tokenBalance(account, tokenA)) {
      return `the SCCP side plus fee exceeds the ${formatTokenAmount(tokenBalance(account, tokenA), "SCCP")} balance`;
    }
    if (!tokenA.native && parsedA > tokenBalance(account, tokenA)) {
      return `${tokenA.symbol} balance is too small`;
    }
    if (tokenB.native && parsedB + fee > tokenBalance(account, tokenB)) {
      return `the SCCP side plus fee exceeds the ${formatTokenAmount(tokenBalance(account, tokenB), "SCCP")} balance`;
    }
    if (!tokenB.native && parsedB > tokenBalance(account, tokenB)) {
      return `${tokenB.symbol} balance is too small`;
    }
    if (account.balance < fee) return "the wallet needs SCCP for the network fee";
    return null;
  })();

  const review = (): void => {
    onReview({
      tx: buildCreatePool({
        network,
        sender: address,
        nonce,
        fee,
        assetA,
        amountA: parsedA,
        assetB,
        amountB: parsedB,
        publicKeyHex: hybridPublicKeyHex(keypair),
      }),
      title: `Create ${tokenA.symbol}/${tokenB.symbol} pool`,
      rows: [
        ["Pair", `${tokenA.symbol} / ${tokenB.symbol}`],
        [
          "Initial reserve",
          `${formatTokenAmount(parsedA, tokenA.symbol, tokenA.decimals)} + ${formatTokenAmount(parsedB, tokenB.symbol, tokenB.decimals)}`,
        ],
        ["LP shares minted", formatInteger(shares)],
        ["Initial price", `1 ${tokenA.symbol} = ${(parsedB / parsedA).toFixed(6)} ${tokenB.symbol}`],
        ["Fee", formatTokenAmount(fee, "SCCP")],
        ["Nonce", formatInteger(nonce)],
      ],
    });
  };

  return (
    <Section
      title="Create pool"
      subtitle="the first deposit sets the price; later deposits follow its ratio"
    >
      <Text variant="bodySmall" style={styles.muted}>
        Token A
      </Text>
      <TokenPicker tokens={tokens} value={assetA} onChange={chooseA} />
      <TextInput
        label={`Amount of ${tokenA.symbol}`}
        mode="outlined"
        value={amountA}
        onChangeText={setAmountA}
        keyboardType="decimal-pad"
        right={<TextInput.Affix text={tokenA.symbol} />}
      />
      <Text variant="bodySmall" style={styles.muted}>
        Token B
      </Text>
      <TokenPicker tokens={tokens} value={assetB} onChange={chooseB} />
      <TextInput
        label={`Amount of ${tokenB.symbol}`}
        mode="outlined"
        value={amountB}
        onChangeText={setAmountB}
        keyboardType="decimal-pad"
        right={<TextInput.Affix text={tokenB.symbol} />}
      />
      <HelperText type="info" visible>
        Spendable: {formatTokenAmount(tokenBalance(account, tokenA), tokenA.symbol, tokenA.decimals)} and{" "}
        {formatTokenAmount(tokenBalance(account, tokenB), tokenB.symbol, tokenB.decimals)}. Initial
        shares are the integer square root of the product of the two deposits.
      </HelperText>
      <KeyValue label="LP shares" mono>
        {formatInteger(shares)}
      </KeyValue>
      {existing !== undefined && (
        <HelperText type="info" visible>
          This pool already exists. Use the Liquidity tab to add to it.
        </HelperText>
      )}
      {problem !== null && (
        <HelperText type="error" visible>
          {problem}
        </HelperText>
      )}
      <Button mode="contained" disabled={problem !== null} onPress={review}>
        Review pool
      </Button>
    </Section>
  );
}

