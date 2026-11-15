/**
 * The table: one seat in a real match.
 *
 * This page is a miner client, not a sandbox. The key it holds is registered on
 * chain (`register`), commits availability two epochs ahead (`availability`)
 * and reveals the nonce in the epoch before it opens (`reveal`). The chain then
 * puts that seat on the epoch's roster, the relay pairs it from the beacon, and
 * the opponent across the board is whatever engine the other seat registered.
 *
 * What the page signs is exactly what a Stockfish miner signs: the certificate
 * at match start, every move with its ply, and the result at the end. The key is
 * the wallet's unlocked hybrid Ed25519 + ML-DSA-65 account, so the transactions
 * it posts are accepted by the chain and its starts and results verify at the
 * relay. What it announces is the same `WinProof` a miner builds from the same
 * transcript, and the checks listed here are the ones
 * `sccp/protocol/verification.py` applies that need nothing but the proof and the
 * epoch beacon — including both hybrid signature rules, verified against the
 * roster's keys.
 *
 * The page lives under the wallet's Play tab, not in the explorer: a seat is a
 * funded account, and the wallet is where the key and the funds already are.
 * `#/play` still resolves here so an old link does not break.
 *
 * Two honest limits are shown, not hidden:
 *
 * - the browser cannot open a trusted clock, so it never *wins on time* — the
 *   relay flags it instead, and the opponent settles that with its own claim;
 * - the page has no analysis engine, which does not matter: how well the chess
 *   was played is not part of a proof, and the protocol does not arbitrate it.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { type PieceSymbol, type Square } from "chess.js";
import { Pressable, StyleSheet, useWindowDimensions, View } from "react-native";
import { Button, Divider, HelperText, ProgressBar, SegmentedButtons, Text } from "react-native-paper";

import {
  fetchAccount,
  fetchEpoch,
  fetchGame,
  fetchMempool,
  requestFaucet,
  submitProof,
  submitTransaction,
} from "../api";
import {
  CodeBlock,
  FailureNotice,
  KeyValue,
  Loading,
  Mono,
  Section,
  StatusPill,
} from "../components/Chrome";
import { useNotice } from "../components/Notices";
import { tokenBalance, tokenForVariant, tokensFor, variantLabel } from "../lib/assets";
import { signCertificate } from "../lib/certificate";
import { DOMAINS } from "../lib/canonical";
import {
  formatAmount,
  formatClock,
  formatCoins,
  formatInteger,
  formatTokenAmount,
  shortAddress,
} from "../lib/format";
import {
  addressFor,
  hybridPublicKeyHex,
  hybridSignDomain,
  hybridVerifyDomain,
  signDomain,
  type HybridKeypair,
} from "../lib/keys";
import { movePayload, resultPayload } from "../lib/protocol";
import { STARTING_FEN } from "../lib/protocol";
import { buildWinProof } from "../lib/proof";
import {
  relayJoin,
  relayMatches,
  relayMove,
  relayPublicUrl,
  relayResign,
  relaySignResult,
  relayStatus,
  relayTranscript,
} from "../lib/relay";
import {
  availabilityCommitment,
  buildAvailability,
  buildRegister,
  buildReveal,
  signTransaction,
  toWireTransaction,
} from "../lib/tx";
import { type CheckResult, verifyProof } from "../lib/verify";
import { VariantGame } from "../lib/variantGame";
import { useNode } from "../node";
import type {
  AccountView,
  Color,
  EpochView,
  GameView,
  NetworkParams,
  RelayMatchView,
  WinProof,
} from "../types";
import { useNow, useResource } from "../useResource";

/** The engine string this seat registers. It is what the certificate commits to. */
const ENGINE = "sccp-web/hand-play-1";

/** How many games this seat asks for per epoch. */
const MAX_GAMES = 1;

const GLYPHS: Record<PieceSymbol, string> = {
  k: "♚",
  q: "♛",
  r: "♜",
  b: "♝",
  n: "♞",
  p: "♟",
};

/** The epoch a transaction lands in, exactly as `sccp.chain.state.epoch_of` computes it. */
function epochOf(height: number, params: NetworkParams): number {
  return Math.floor(height / params.epoch_length);
}

function randomNonce(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function squareName(column: number, row: number): Square {
  const files = "abcdefgh";
  return `${files[column]}${8 - row}` as Square;
}

/** What the page is waiting for, derived from chain and relay state. */
type Stage =
  | "fund"
  | "register"
  | "commit"
  | "wait-for-reveal"
  | "reveal"
  | "wait-for-pairing"
  | "join"
  | "play"
  | "settle";

function stageOf(
  account: AccountView,
  params: NetworkParams,
  epoch: EpochView | undefined,
  match: RelayMatchView | null,
  address: string,
  commitment: StoredCommitment | null,
): Stage {
  if (match !== null) {
    if (match.status === "pending") return "join";
    if (match.result !== null) return "settle";
    return "play";
  }
  if (account.registered_until_epoch === null) {
    return account.balance + account.bonded >= params.min_bond + params.min_fee
      ? "register"
      : "fund";
  }
  if (account.bonded < params.min_bond) return "fund";
  if (epoch !== undefined && epoch.roster[address] !== undefined) return "wait-for-pairing";
  if (epoch?.availability[address] === undefined) return "commit";
  // `_validate_reveal` only accepts a reference to `epoch + 1`, so a commitment
  // made two epochs ahead cannot be revealed until the chain reaches `E - 1`.
  const target = commitment === null ? null : commitment.epoch;
  if (target !== null && epoch !== undefined && epoch.epoch === target - 1) return "reveal";
  return "wait-for-reveal";
}

interface StoredCommitment {
  epoch: number;
  nonce: string;
}

/** A transaction the node accepted, and the account nonce it spends. */
interface PendingTx {
  label: "register" | "commit" | "reveal";
  txid: string;
  nonce: number;
  submittedAt: number;
}

function commitmentKey(address: string): string {
  return `sccp-web/availability/${address}`;
}

function loadCommitment(address: string): StoredCommitment | null {
  try {
    const raw = window.sessionStorage.getItem(commitmentKey(address));
    if (raw === null) return null;
    const parsed: unknown = JSON.parse(raw);
    if (
      typeof parsed === "object" &&
      parsed !== null &&
      "epoch" in parsed &&
      "nonce" in parsed &&
      typeof (parsed as StoredCommitment).epoch === "number" &&
      typeof (parsed as StoredCommitment).nonce === "string"
    ) {
      return parsed as StoredCommitment;
    }
    return null;
  } catch {
    return null;
  }
}

function saveCommitment(address: string, value: StoredCommitment): void {
  try {
    window.sessionStorage.setItem(commitmentKey(address), JSON.stringify(value));
  } catch {
    // A browser with storage disabled still reveals; it just cannot survive a reload.
  }
}

function dropCommitment(address: string): void {
  try {
    window.sessionStorage.removeItem(commitmentKey(address));
  } catch {
    // Nothing to clean up if storage was never available.
  }
}

/** Pick the session this seat is playing in this epoch. */
function pickMatch(
  matches: readonly RelayMatchView[],
  address: string,
): RelayMatchView | null {
  const mine = matches.filter(
    (view) => view.certificate.white === address || view.certificate.black === address,
  );
  return (
    mine.find((view) => view.status === "active") ??
    mine.find((view) => view.status === "pending") ??
    mine[mine.length - 1] ??
    null
  );
}

/**
 * The table, played with the account the wallet unlocked.
 *
 * There is no seed field and no separate "sit down" key: the key that registers,
 * commits availability and signs every move is the wallet's, so a seat is the
 * same account that holds the funds. The wallet renders this under its Play tab;
 * `#/play` is an alias for that page.
 */
export function Play({
  keypair,
  network,
}: {
  keypair: HybridKeypair;
  network: string;
}): ReactNode {
  return <Table key={network} network={network} keypair={keypair} />;
}

function Table({
  network,
  keypair,
}: {
  network: string;
  keypair: HybridKeypair;
}): ReactNode {
  const { status, params: paramsResource } = useNode();
  const params = paramsResource.data;
  const address = addressFor(keypair.publicKey, network);
  const height = status.data?.height ?? 0;

  const account = useResource((signal) => fetchAccount(address, signal), [address], {
    pollMs: 3000,
  });
  const tipEpoch = params === undefined ? 0 : epochOf(height, params);
  const epochView = useResource((signal) => fetchEpoch(tipEpoch, signal), [tipEpoch], {
    pollMs: 3000,
  });
  const relay = useResource(
    async (signal) => {
      const [head, matches] = await Promise.all([
        relayStatus(signal),
        relayMatches({ signal }),
      ]);
      return { head, matches };
    },
    [address],
    { pollMs: 2000 },
  );

  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<Error | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const notify = useNotice();
  // A chain action returns a txid before the block that executes it. Remembering
  // the nonce it spent is what stops a second click from resending the same
  // nonce and reading the node's rejection as if the first click never worked.
  const [pendingTx, setPendingTx] = useState<PendingTx | null>(null);
  // While a transaction is pending, the node's own pool is the authority on
  // whether it is still there; the status counters cannot identify a txid.
  const mempool = useResource(
    (signal) => (pendingTx === null ? Promise.resolve(null) : fetchMempool(signal)),
    [pendingTx?.txid],
    pendingTx === null ? {} : { pollMs: 2000 },
  );
  const [commitment, setCommitment] = useState<StoredCommitment | null>(() =>
    loadCommitment(address),
  );
  const [selected, setSelected] = useState<string | null>(null);
  const [selectedVariant, setSelectedVariant] = useState<string | null>(null);
  const [proof, setProof] = useState<WinProof | null>(null);
  const [checks, setChecks] = useState<CheckResult[] | null>(null);
  const signedResults = useRef<Set<string>>(new Set());
  const announced = useRef<Set<string>>(new Set());

  const { width } = useWindowDimensions();
  const boardSize = width < 720 ? Math.min(width - 56, 416) : 448;
  const now = useNow(500);

  const relayNetwork = relay.data?.head.network ?? null;
  // The node and the relay have to be on one chain: a certificate, a move and a
  // proof each name a network, so nothing can be signed across a disagreement.
  const networkMismatch = relayNetwork !== null && relayNetwork !== network;

  const match = pickMatch(relay.data?.matches ?? [], address);
  const colour: Color | null =
    match === null ? null : match.certificate.white === address ? "white" : "black";
  const opponent =
    match === null ? null : colour === "white" ? match.certificate.black : match.certificate.white;

  const board = useMemo(() => {
    const game = new VariantGame(
      match?.certificate.variant ?? match?.matchup.variant ?? "standard",
      match?.certificate.opening.start_fen ?? STARTING_FEN,
    );
    for (const san of match?.san_moves ?? []) {
      try {
        if (!game.move(san)) break;
      } catch {
        // A relay that published an unparseable SAN leaves the board at the last
        // move it could replay; the verifier later refuses the proof outright.
        break;
      }
    }
    return game;
  }, [
    match?.san_moves,
    match?.certificate.variant,
    match?.certificate.opening.start_fen,
    match?.matchup.variant,
  ]);

  const live = match !== null && match.status === "active" && match.result === null;
  const ourTurn = live && match.turn === colour;
  const elapsed = relay.updatedAt === undefined ? 0 : Math.max(0, now - relay.updatedAt);
  const whiteMs =
    match === null
      ? 0
      : Math.max(0, match.white_ms - (match.turn === "white" && live ? elapsed : 0));
  const blackMs =
    match === null
      ? 0
      : Math.max(0, match.black_ms - (match.turn === "black" && live ? elapsed : 0));
  const ourMs = colour === "white" ? whiteMs : blackMs;

  const reloadAccount = account.reload;
  const accountData = account.data;
  const epochData = epochView.data;
  const tokens = tokensFor(params);
  const registeredVariant =
    accountData !== undefined && accountData.registered_until_epoch !== null
      ? (accountData.variant ?? "standard")
      : null;
  const variant = registeredVariant ?? selectedVariant ?? tokens[0]?.variant ?? "standard";
  const variantParams = params?.variants?.find((candidate) => candidate.name === variant);
  const minPlies = variantParams?.min_plies ?? params?.min_plies ?? 1;
  const prizeToken = tokenForVariant(tokens, variant);

  const reloadStatus = status.reload;
  const act = useCallback(
    async (label: string, body: () => Promise<string>) => {
      setBusy(label);
      setFailure(null);
      setNotice(null);
      try {
        const message = await body();
        setNotice(message);
        notify(message);
        reloadAccount();
        // The submission just changed the mempool, so ask the node again rather
        // than waiting for the next poll to show the pending transaction.
        reloadStatus();
      } catch (error) {
        const described = error instanceof Error ? error : new Error(String(error));
        setFailure(described);
        notify(described.message);
      } finally {
        setBusy(null);
      }
    },
    [notify, reloadAccount, reloadStatus],
  );

  // Clear the pending marker once the chain has moved past its nonce, or once a
  // later status poll no longer lists the transaction and a block has had time
  // to include it. Until then a click would rebuild the same nonce.
  useEffect(() => {
    if (pendingTx === null || accountData === undefined) return;
    if (accountData.nonce > pendingTx.nonce) {
      setPendingTx(null);
      return;
    }
    if (mempool.updatedAt === undefined || mempool.updatedAt <= pendingTx.submittedAt) return;
    if (now - pendingTx.submittedAt < 5000) return;
    const queued = mempool.data?.transactions;
    if (queued !== undefined && !queued.includes(pendingTx.txid)) {
      setPendingTx(null);
    }
  }, [accountData, mempool.data, mempool.updatedAt, now, pendingTx]);

  const fund = (): void => {
    void act("fund", async () => {
      const grant = await requestFaucet(address);
      return `the faucet sent ${formatCoins(grant.amount)} to ${shortAddress(address)} (${grant.txid.slice(0, 12)}…)`;
    });
  };

  const register = (): void => {
    void act("register", async () => {
      if (params === undefined || accountData === undefined) {
        throw new Error("the node has not answered yet");
      }
      if (accountData.balance + accountData.bonded < params.min_bond + params.min_fee) {
        throw new Error(
          `a bond of ${params.min_bond} plus a fee of ${params.min_fee} base units is not funded yet`,
        );
      }
      const tx = signTransaction(
        keypair,
        buildRegister({
          network,
          sender: address,
          nonce: accountData.nonce,
          fee: params.min_fee,
          bond: params.min_bond,
          endpoint: window.location.origin,
          engine: ENGINE,
          variant,
          publicKeyHex: hybridPublicKeyHex(keypair),
        }),
      );
      const accepted = await submitTransaction(toWireTransaction(tx));
      setPendingTx({
        label: "register",
        txid: accepted.txid,
        nonce: accountData.nonce,
        submittedAt: Date.now(),
      });
      return `registration ${accepted.txid.slice(0, 12)}… announced`;
    });
  };

  const commit = (): void => {
    void act("commit", async () => {
      if (params === undefined || accountData === undefined) {
        throw new Error("the node has not answered yet");
      }
      const target = epochOf(height + 1, params) + params.availability_lead;
      const stored: StoredCommitment = { epoch: target, nonce: randomNonce() };
      const tx = signTransaction(
        keypair,
        buildAvailability({
          network,
          sender: address,
          nonce: accountData.nonce,
          fee: params.min_fee,
          epoch: stored.epoch,
          maxGames: MAX_GAMES,
          commitment: availabilityCommitment(address, stored.epoch, stored.nonce),
          publicKeyHex: hybridPublicKeyHex(keypair),
        }),
      );
      const accepted = await submitTransaction(toWireTransaction(tx));
      saveCommitment(address, stored);
      setCommitment(stored);
      setPendingTx({
        label: "commit",
        txid: accepted.txid,
        nonce: accountData.nonce,
        submittedAt: Date.now(),
      });
      return `availability for epoch ${stored.epoch} committed in ${accepted.txid.slice(0, 12)}…`;
    });
  };

  const reveal = (): void => {
    void act("reveal", async () => {
      if (params === undefined || accountData === undefined) {
        throw new Error("the node has not answered yet");
      }
      const stored = commitment ?? loadCommitment(address);
      if (stored === null) {
        throw new Error("this tab has no availability nonce: the commitment was made elsewhere");
      }
      const tx = signTransaction(
        keypair,
        buildReveal({
          network,
          sender: address,
          nonce: accountData.nonce,
          fee: params.min_fee,
          epoch: stored.epoch,
          nonceSecret: stored.nonce,
          publicKeyHex: hybridPublicKeyHex(keypair),
        }),
      );
      const accepted = await submitTransaction(toWireTransaction(tx));
      setPendingTx({
        label: "reveal",
        txid: accepted.txid,
        nonce: accountData.nonce,
        submittedAt: Date.now(),
      });
      return `nonce for epoch ${stored.epoch} revealed in ${accepted.txid.slice(0, 12)}…`;
    });
  };

  const join = (): void => {
    void act("join", async () => {
      if (match === null) throw new Error("the relay has not paired this seat");
      const signature = signCertificate(keypair, match.certificate);
      await relayJoin(match.matchup.match_id, {
        seat: address,
        publicKey: hybridPublicKeyHex(keypair),
        signature,
      });
      relay.reload();
      return "start signature delivered; the match activates when both seats have joined";
    });
  };

  const resign = (): void => {
    void act("resign", async () => {
      if (match === null) throw new Error("there is no match to resign");
      await relayResign(match.matchup.match_id, address);
      relay.reload();
      return "resigned";
    });
  };

  const sendSan = (san: string): void => {
    if (match === null || colour === null) return;
    const ply = match.san_moves.length;
    // Moves are Ed25519: `docs/PROTOCOL.md` section 8.5 keeps the high-volume
    // streams classical and anchors them with a hybrid signature over the move
    // root inside the result. The relay verifies this with the registered key's
    // Ed25519 half.
    const signature = signDomain(
      keypair.ed25519,
      DOMAINS.MOVE,
      movePayload({ network, match_id: match.matchup.match_id, seat: address, ply, san }),
    );
    setSelected(null);
    void act("move", async () => {
      await relayMove(match.matchup.match_id, {
        seat: address,
        san,
        network,
        ply,
        clockMs: ourMs,
        signature,
      });
      relay.reload();
      return `${san} at ply ${ply}`;
    });
  };

  const playMove = (from: string, to: string): void => {
    if (match === null || colour === null) return;
    const san = board.sanFor(from as Square, to as Square);
    if (san === null) return;
    sendSan(san);
  };

  const coSign = useCallback(
    async (view: RelayMatchView): Promise<string> => {
      const result = view.result;
      if (result === null) throw new Error("the match has no result yet");
      const seat: Color = view.certificate.white === address ? "white" : "black";
      const payload = resultPayload({
        network: result.network,
        match_id: result.match_id,
        game_hash: result.game_hash,
        result: result.result,
        termination: result.termination,
        plies: result.plies,
        move_commitment: result.move_commitment,
      });
      // A result signature is hybrid: the seat commits to the game hash, the
      // termination and the move root with both halves, which is what makes a
      // forged settled result impossible for a quantum attacker holding only the
      // Ed25519 half. `sccp wallet sign-result` produces the same bytes from the
      // same key, so the page and the CLI are interchangeable.
      return hybridSignDomain(keypair, DOMAINS.RESULT, payload);
    },
    [address, keypair],
  );

  const announce = (): void => {
    void act("proof", async () => {
      if (match === null || match.result === null) throw new Error("the match is not finished");
      if (params === undefined) throw new Error("network parameters are not loaded");
      const transcript = await relayTranscript(match.matchup.match_id);
      if (transcript.result.game_hash !== match.result.game_hash) {
        throw new Error("the relay transcript disagrees with the live result");
      }
      const proofParams = {
        network_id: network,
        rounds_per_epoch: params.rounds_per_epoch,
        min_plies: minPlies,
      };
      const built = buildWinProof({
        certificate: transcript.certificate,
        pgn: transcript.pgn,
        sanMoves: match.san_moves,
        result: transcript.result,
        resultSignatures: transcript.result.result_signatures,
        params: proofParams,
      });
      setProof(built);
      const settledEpoch = await fetchEpoch(built.epoch);
      if (settledEpoch.beacon === null) {
        throw new Error(`the node has no beacon for epoch ${built.epoch}`);
      }
      const verdict = verifyProof(built, {
        params: proofParams,
        beacon: {
          network: settledEpoch.beacon.network,
          epoch: settledEpoch.beacon.epoch,
          hash: settledEpoch.beacon.hash,
        },
        publicKeys: Object.fromEntries(
          Object.entries(settledEpoch.roster).map(([seatAddress, member]) => [
            seatAddress,
            member.public_key,
          ]),
        ),
        // The page holds ML-DSA-65, so the two hybrid signature rules are real
        // checks here rather than reported skips: a wrong key, a signature over
        // another payload, or a half-signed result fails before submission.
        verifyLedgerSignature: hybridVerifyDomain,
      });
      setChecks(verdict.checks);
      if (!verdict.ok) return "the proof is built but fails local verification";
      if (built.winner !== address) return "the proof verifies; the winner announces its own";
      if (announced.current.has(built.game_hash)) return "this proof was already announced";
      const accepted = await submitProof(built);
      announced.current.add(built.game_hash);
      return `proof ${accepted.proof_id.slice(0, 12)}… announced to the node`;
    });
  };

  const settlement = useResource(
    (signal) => (proof === null ? Promise.resolve(null) : fetchGame(proof.game_hash, signal)),
    [proof === null ? "" : proof.game_hash],
    { pollMs: 3000 },
  );

  const matchId = match === null ? null : match.matchup.match_id;
  useEffect(() => {
    setProof(null);
    setChecks(null);
    setSelected(null);
  }, [matchId]);

  useEffect(() => {
    if (match === null || match.result === null || colour === null) return;
    if (match.result.result_signatures[colour] !== undefined) return;
    const key = `${match.matchup.match_id}:${match.result.game_hash}`;
    if (signedResults.current.has(key)) return;
    signedResults.current.add(key);
    void act("cosign", async () => coSign(match));
  }, [match, colour, act, coSign]);

  const stage: Stage | null =
    accountData === undefined || params === undefined
      ? null
      : stageOf(accountData, params, epochData, match, address, commitment);

  const legal =
    selected === null ? [] : board.targets(selected as Square);

  const press = (square: Square): void => {
    if (!ourTurn) return;
    if (selected !== null && legal.includes(square)) {
      playMove(selected, square);
      return;
    }
    const piece = board.get(square);
    setSelected(piece !== undefined && piece.color === board.turn ? square : null);
  };

  if (status.data === undefined && status.error !== undefined) {
    return <FailureNotice error={status.error} />;
  }
  if (accountData === undefined || params === undefined) {
    if (account.error !== undefined) return <FailureNotice error={account.error} />;
    return <Loading />;
  }

  const tip = epochData;
  const inRoster = tip !== undefined && tip.roster[address] !== undefined;
  const committed = tip !== undefined && tip.availability[address] !== undefined;
  const nextTarget = epochOf(height + 1, params) + params.availability_lead;
  const settlementData = settlement.data;

  return (
    <>
      <View style={styles.hero}>
        <Text variant="headlineSmall" style={styles.heroTitle}>
          One seat, one real match
        </Text>
        <Text variant="bodyMedium" style={styles.heroBody}>
          {stageText(stage)}
        </Text>
      </View>

      {failure !== null && <FailureNotice error={failure} />}
      {notice !== null && (
        <HelperText type="info" visible>
          {notice}
        </HelperText>
      )}

      {networkMismatch && (
        <>
          <Section title="Two networks" subtitle={`node ${network ?? "?"} · relay ${relayNetwork}`}>
            <Text variant="bodyMedium" style={styles.muted}>
              {`The node serving this page is on ${network}, but the relay at ${relayPublicUrl()} is on ${relayNetwork}. Every payload this page signs names one network, so a seat on ${network} cannot be paired or certified by a ${relayNetwork} relay. Point both at one chain: restart the relay with --network ${network} and --node ${window.location.origin}, or rebuild the page with VITE_SCCP_RELAY pointing at a relay that follows this node.`}
            </Text>
          </Section>
          <Divider />
        </>
      )}

      <Section
        title="Seat"
        subtitle={`${network} · relay ${relayNetwork ?? "—"} · epoch ${tipEpoch} · round ${relay.data?.head.round ?? "—"}`}
        action={
          <StatusPill
            label={stage ?? "reading"}
            tone={stage === "play" ? "good" : stage === "settle" ? "info" : "neutral"}
          />
        }
      >
        <KeyValue label="Address">
          <Mono copy>{address}</Mono>
        </KeyValue>
        <KeyValue label="Variant">
          {`${variantLabel(variant)} · ${variant}`}
          {registeredVariant !== null ? " (locked by registration)" : ""}
        </KeyValue>
        {registeredVariant === null && tokens.length > 1 && (
          <SegmentedButtons
            value={variant}
            onValueChange={setSelectedVariant}
            buttons={tokens.map((token) => ({
              value: token.variant,
              label: token.symbol,
            }))}
          />
        )}
        <KeyValue label="SCCP balance">
          {`${formatAmount(accountData.balance)} spendable · ${formatAmount(accountData.bonded)} bonded`}
        </KeyValue>
        {tokens
          .filter((token) => !token.native)
          .map((token) => (
            <KeyValue key={token.asset ?? token.symbol} label={`${token.symbol} balance`} mono>
              {formatTokenAmount(
                tokenBalance(accountData, token),
                token.symbol,
                token.decimals,
              )}
            </KeyValue>
          ))}
        <KeyValue label="Nonce">{formatInteger(accountData.nonce)}</KeyValue>
        <KeyValue label="Registration">
          {accountData.registered_until_epoch === null
            ? "not registered"
            : `through epoch ${accountData.registered_until_epoch}`}
        </KeyValue>
        <KeyValue label="Engine fingerprint">{accountData.engine ?? ENGINE}</KeyValue>
      </Section>

      <Divider />

      <Section title="Registration" subtitle="signed transactions, then two epochs of patience">
        <Text variant="bodyMedium" style={styles.muted}>
          {`Register for ${variantLabel(variant)} with a bond, commit availability for epoch ${nextTarget} during this epoch, reveal the nonce in epoch ${nextTarget - 1}. The chain then opens epoch ${nextTarget}'s roster with this address on it, and the relay pairs that roster from the epoch's beacon.`}
        </Text>
        <HelperText type="info" visible>
          {prizeToken === undefined
            ? "This network has no token metadata loaded yet."
            : prizeToken.native
              ? "Winning standard proofs mint native SCCP; the bond and every fee are SCCP too."
              : `Winning ${variantLabel(variant)} proofs mint ${prizeToken.symbol}; the bond and every fee are still paid in SCCP.`}
        </HelperText>
        <View style={styles.actions}>
          <Button
            mode="outlined"
            onPress={fund}
            loading={busy === "fund"}
            disabled={busy !== null || pendingTx !== null}
          >
            Faucet
          </Button>
          <Button
            mode="outlined"
            onPress={register}
            loading={busy === "register"}
            disabled={
              busy !== null ||
              pendingTx !== null ||
              accountData.registered_until_epoch !== null
            }
          >
            {pendingTx?.label === "register" ? "Register pending" : "Register"}
          </Button>
          <Button
            mode="outlined"
            onPress={commit}
            loading={busy === "commit"}
            disabled={busy !== null || pendingTx !== null || stage !== "commit"}
          >
            {pendingTx?.label === "commit" ? "Commit pending" : "Commit availability"}
          </Button>
          <Button
            mode="outlined"
            onPress={reveal}
            loading={busy === "reveal"}
            disabled={
              busy !== null ||
              pendingTx !== null ||
              stage !== "reveal" ||
              (commitment === null && loadCommitment(address) === null)
            }
          >
            {pendingTx?.label === "reveal" ? "Reveal pending" : "Reveal nonce"}
          </Button>
        </View>
        {pendingTx !== null && (
          <HelperText type="info" visible>
            {`${pendingTx.label} transaction ${pendingTx.txid.slice(0, 12)}… is in the mempool. The chain action buttons stay disabled until a block spends nonce ${formatInteger(pendingTx.nonce)}; this page cannot safely sign another transaction for this account before that.`}
          </HelperText>
        )}
        {busy === "fund" && (
          <>
            <ProgressBar indeterminate />
            <Text variant="bodySmall" style={styles.muted}>
              Asking the faucet for funds; the node answers with a transaction id once it accepts
              the grant.
            </Text>
          </>
        )}
        <KeyValue label="Commitment in this tab">
          {commitment === null ? "none" : `epoch ${commitment.epoch}`}
        </KeyValue>
        <KeyValue label={`Epoch ${tipEpoch} availability`}>
          {tip === undefined ? "…" : committed ? "committed" : "none"}
        </KeyValue>
        <KeyValue label={`Epoch ${tipEpoch} roster`}>
          {tip === undefined
            ? "…"
            : inRoster
              ? "this seat is on it"
              : `not on it (${Object.keys(tip.roster).length} seats)`}
        </KeyValue>
      </Section>

      {relay.error !== undefined && (
        <>
          <Divider />
          <Section title="Relay" subtitle={relayPublicUrl()}>
            <FailureNotice error={relay.error} />
            <Text variant="bodySmall" style={styles.muted}>
              {`The relay is a separate service. A deployment starts it next to the node with \`sccp matchmaker run --config relay.json --node ${window.location.origin} --origin ${window.location.origin}\`, and a static build points at it with VITE_SCCP_RELAY.`}
            </Text>
          </Section>
        </>
      )}

      <Divider />

      <Section
        title="Match"
        subtitle={
          match === null
            ? "no session lists this seat yet"
            : `${match.status} · round ${match.matchup.round} · ${match.san_moves.length} plies`
        }
      >
        {match === null ? (
          <Text variant="bodyMedium" style={styles.muted}>
            {"The relay pairs the epoch roster from its beacon. A seat that is not on the roster, or is the odd one out of a round, sits this one out — the pairing is not something a client can choose."}
          </Text>
        ) : (
          <>
            <KeyValue label="Match id">
              <Mono copy>{match.matchup.match_id}</Mono>
            </KeyValue>
            <KeyValue label="Opponent">
              <Mono copy>{opponent ?? ""}</Mono>
            </KeyValue>
            <KeyValue label="Your colour">{colour ?? ""}</KeyValue>
            <KeyValue label="Variant" mono>
              {`${variantLabel(match.certificate.variant)} · ${match.certificate.variant}`}
            </KeyValue>
            <KeyValue label="Start position">
              <Mono copy>{match.certificate.opening.start_fen}</Mono>
            </KeyValue>
            <KeyValue label="Opening">
              {`${match.certificate.opening.book_version} #${match.certificate.opening.line_id}: ${match.certificate.opening.moves.join(" ")}`}
            </KeyValue>
            <KeyValue label="Engines">
              {`${match.certificate.engine_white} vs ${match.certificate.engine_black}`}
            </KeyValue>
            <KeyValue label="Clock">
              {`white ${formatClock(whiteMs)} · black ${formatClock(blackMs)}`}
            </KeyValue>
            <KeyValue label="Start signatures">
              {Object.keys(match.certificate.start_signatures).join(", ") || "none yet"}
            </KeyValue>
            <View style={styles.actions}>
              {match.status === "pending" && (
                <Button
                  mode="contained"
                  onPress={join}
                  loading={busy === "join"}
                  disabled={
                    busy !== null ||
                    (colour !== null && match.certificate.start_signatures[colour] !== undefined)
                  }
                >
                  Sign the certificate
                </Button>
              )}
              {live && ourTurn && (
                <Button
                  mode="outlined"
                  onPress={resign}
                  loading={busy === "resign"}
                  disabled={busy !== null}
                >
                  Resign
                </Button>
              )}
            </View>
            {match.status === "pending" && (
              <HelperText type="info" visible>
                Both start signatures activate the match and start the clocks; the second one is what
                makes the certificate final.
              </HelperText>
            )}
          </>
        )}
      </Section>


      <Divider />

      <Section
        title="Board"
        subtitle={
          match === null
            ? "no board until the relay pairs this seat"
            : live
              ? `${ourTurn ? "your move" : "their move"}${board.isCheck() ? ", in check" : ""}`
              : match.result === null
                ? match.status
                : `game over: ${match.result.result} by ${match.result.termination}`
        }
      >
        <View style={styles.table}>
          <View style={[styles.board, { width: boardSize, height: boardSize }]}>
            {board.board().map((row, rowIndex) => (
              <View key={rowIndex} style={styles.boardRow}>
                {row.map((cell, columnIndex) => {
                  const square = cell?.square ?? squareName(columnIndex, rowIndex);
                  const light = (rowIndex + columnIndex) % 2 === 0;
                  const isSelected = selected === square;
                  const isTarget = legal.includes(square);
                  return (
                    <Pressable
                      key={square}
                      accessibilityRole="button"
                      accessibilityLabel={square}
                      onPress={() => {
                        press(square);
                      }}
                      style={[
                        styles.square,
                        light ? styles.squareLight : styles.squareDark,
                        isSelected ? styles.squareSelected : null,
                        isTarget ? styles.squareTarget : null,
                      ]}
                    >
                      {cell !== null && cell !== undefined && (
                        <Text
                          style={[
                            styles.piece,
                            { fontSize: boardSize / 11 },
                            cell.color === "w" ? styles.pieceWhite : styles.pieceBlack,
                          ]}
                        >
                          {GLYPHS[cell.type]}
                        </Text>
                      )}
                    </Pressable>
                  );
                })}
              </View>
            ))}
          </View>

          <View style={styles.side}>
            <View style={styles.turnRow}>
              <StatusPill
                label={
                  match === null
                    ? "no match"
                    : live
                      ? ourTurn
                        ? "your move"
                        : "their move"
                      : match.status
                }
                tone={ourTurn ? "good" : "neutral"}
              />
              <Text variant="bodySmall" style={styles.muted}>
                {`your clock ${formatClock(ourMs)}`}
              </Text>
            </View>
            {live && ourTurn && board.castleSans().length > 0 && (
              <View style={styles.actions}>
                {board.castleSans().map((san) => (
                  <Button
                    key={san}
                    mode="outlined"
                    compact
                    onPress={() => sendSan(san)}
                    disabled={busy !== null}
                  >
                    {san === "O-O" ? "Castle kingside" : "Castle queenside"}
                  </Button>
                ))}
              </View>
            )}
            <View style={styles.log}>
              {(match?.san_moves ?? []).map((san, index) => (
                <View key={`${index}-${san}`} style={styles.logRow}>
                  <Text variant="bodySmall" style={styles.logPly}>
                    {`${Math.floor(index / 2) + 1}${index % 2 === 0 ? "." : "…"}`}
                  </Text>
                  <Text variant="bodyMedium" style={styles.logSan}>
                    {san}
                  </Text>
                </View>
              ))}
              {(match?.san_moves ?? []).length === 0 && (
                <Text variant="bodySmall" style={styles.muted}>
                  The certified opening is played by the relay before the clocks start.
                </Text>
              )}
            </View>
          </View>
        </View>
      </Section>


      {match?.result != null && (
        <>
          <Divider />
          <Section
            title="Result"
            subtitle={`${match.result.result} by ${match.result.termination} in ${match.result.plies} plies`}
          >
            <KeyValue label="Game hash">
              <Mono copy>{match.result.game_hash}</Mono>
            </KeyValue>
            <KeyValue label="Signatures">
              {Object.keys(match.result.result_signatures).join(", ") || "none yet"}
            </KeyValue>
            {match.result.termination === "time_forfeit" && (
              <HelperText type="info" visible>
                This one ended on the relay&apos;s clock. A flag settles through a bonded timeout
                claim backed by the relay&apos;s witness receipts; a browser seat cannot open that
                claim, so this page can only record the result.
              </HelperText>
            )}
            <View style={styles.actions}>
              <Button
                mode="contained"
                onPress={announce}
                loading={busy === "proof"}
                disabled={busy !== null || Object.keys(match.result.result_signatures).length < 2}
              >
                Build, verify and announce
              </Button>
            </View>
            {Object.keys(match.result.result_signatures).length < 2 && (
              <HelperText type="info" visible>
                The transcript exists once both seats have co-signed. This page co-signs
                automatically, so this clears as soon as the opponent does too.
              </HelperText>
            )}
          </Section>
        </>
      )}

      {checks !== null && (
        <>
          <Divider />
          <Section
            title="Local verification"
            subtitle="every rule that needs only the proof and the epoch beacon"
          >
            {checks.map((check) => (
              <View key={check.rule} style={styles.sigRow}>
                <View style={styles.logRow}>
                  <StatusPill label={check.ok ? "pass" : "fail"} tone={check.ok ? "good" : "bad"} />
                  <Text variant="bodyMedium" style={styles.logSan}>
                    {check.rule}
                  </Text>
                </View>
                <Text variant="bodySmall" style={styles.muted}>
                  {check.detail}
                </Text>
              </View>
            ))}
          </Section>
        </>
      )}

      {proof !== null && (
        <>
          <Divider />
          <Section title="Proof" subtitle={proof.proof_id}>
            <KeyValue label="Winner">
              {`${shortAddress(proof.winner)} · ${proof.winner_seat}`}
            </KeyValue>
            <KeyValue label="Variant" mono>
              {`${variantLabel(proof.variant)} · ${proof.variant}`}
            </KeyValue>
            <KeyValue label="Low ply">
              {proof.low_ply ? `yes (${proof.plies} < ${minPlies})` : "no"}
            </KeyValue>
            <KeyValue label="Settlement">
              {settlementData === null || settlementData === undefined
                ? "asking the node"
                : settlementData.height !== null
                  ? `settled at height ${settlementData.height}`
                  : settlementData.in_mempool
                    ? "in the mempool"
                    : "not settled yet"}
            </KeyValue>
            <KeyValue label="Proof JSON">
              <CodeBlock>{JSON.stringify(proof, null, 2)}</CodeBlock>
            </KeyValue>
          </Section>
        </>
      )}
    </>
  );
}

/** One sentence on what the page is waiting for. */
function stageText(stage: Stage | null): string {
  switch (stage) {
    case null:
      return "Reading the chain and the relay…";
    case "fund":
      return "Fund the seat. A bond and a fee have to be on chain before it can register.";
    case "register":
      return "Register for the selected variant. The bond, endpoint, engine fingerprint and variant are what the certificate will commit to.";
    case "commit":
      return "Commit availability for the epoch two ahead. The reveal comes in the epoch before it opens.";
    case "wait-for-reveal":
      return "Committed. A reveal is only valid in epoch E-1, so the chain has to reach it first.";
    case "reveal":
      return "Reveal the committed nonce. After this the chain can open a roster slot.";
    case "wait-for-pairing":
      return "On the roster. Waiting for the relay to open a round whose beacon pairs this seat.";
    case "join":
      return "Paired. Sign the certificate; the second signature starts both clocks.";
    case "play":
      return "Your move. Every move is signed with its ply and relayed to the opponent.";
    case "settle":
      return "Result recorded. Build the proof, check it locally, and announce it to the node.";
  }
}

const styles = StyleSheet.create({
  hero: { gap: 8, paddingTop: 8, maxWidth: 760 },
  heroTitle: { fontWeight: "600" },
  heroBody: { opacity: 0.8 },
  actions: { flexDirection: "row", gap: 8, flexWrap: "wrap" },
  table: {
    flexDirection: "row",
    gap: 20,
    flexWrap: "wrap",
    alignItems: "flex-start",
    paddingTop: 8,
  },
  board: {
    borderRadius: 8,
    overflow: "hidden",
    borderWidth: 1,
    borderColor: "rgba(0,0,0,0.24)",
  },
  boardRow: { flex: 1, flexDirection: "row" },
  square: { flex: 1, alignItems: "center", justifyContent: "center" },
  squareLight: { backgroundColor: "#e9e2d0" },
  squareDark: { backgroundColor: "#7d6a54" },
  squareSelected: { backgroundColor: "#c9a227" },
  squareTarget: { backgroundColor: "#94b58a" },
  piece: { textAlign: "center" },
  pieceWhite: { color: "#ffffff", textShadowColor: "#161616", textShadowRadius: 1 },
  pieceBlack: { color: "#161616", textShadowColor: "#ffffff", textShadowRadius: 1 },
  side: { flexGrow: 1, flexShrink: 1, minWidth: 220, gap: 10 },
  turnRow: { flexDirection: "row", alignItems: "center", gap: 8, flexWrap: "wrap" },
  log: { gap: 2 },
  logRow: { flexDirection: "row", alignItems: "center", gap: 8, flexWrap: "wrap" },
  logPly: { width: 42, fontVariant: ["tabular-nums"], opacity: 0.66 },
  logSan: { minWidth: 64, fontWeight: "600" },
  sigRow: {
    gap: 4,
    paddingVertical: 6,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: "rgba(128,128,128,0.32)",
  },
  muted: { opacity: 0.66 },
});
