/**
 * Wire types for the node API (PROTOCOL.md section 14).
 *
 * These mirror `sccp/node/models.py` field for field, but only where the UI reads
 * them; the node is the authority and the explorer never invents a field. Every
 * response carries `network`, which the shell compares against `/v1/status` so a
 * tab left open across a network switch says so instead of mixing chains.
 */

export type Color = "white" | "black";
export type ResultValue = "1-0" | "0-1" | "1/2-1/2";
export type TerminationValue = "checkmate" | "draw" | "resignation" | "time_forfeit";
export type ChallengeStatus = "none" | "open" | "upheld" | "rejected";
export type ChallengeState = "open" | "upheld" | "rejected";

export interface MempoolStatus {
  transactions: number;
  proofs: number;
}

export interface NodeStatus {
  network: string;
  protocol_version: number;
  height: number;
  tip_hash: string;
  genesis_hash: string;
  peers: number;
  syncing: boolean;
  mempool: MempoolStatus;
}

export interface NetworkParams {
  network_id: string;
  address_hrp: string;
  genesis_timestamp: number | null;
  epoch_length: number;
  rounds_per_epoch: number;
  availability_lead: number;
  registration_epochs: number;
  lag_grace_ms: number;
  clock_witness_quorum: number;
  wins_required: number;
  k_min: number;
  k_max: number;
  claim_window: number;
  seal_bits: number;
  target_block_seconds: number;
  retarget_interval: number;
  subsidy: number;
  halving_interval: number | null;
  max_supply: number | null;
  coinbase_maturity: number;
  min_bond: number;
  min_fee: number;
  challenge_window: number;
  min_plies: number;
  max_plies: number;
  nodes_per_move: number;
  book_version: string;
  quality_mode: "off" | "sampled";
  quality_sample_rate: number;
  max_block_bytes: number;
  max_pgn_bytes: number;
  max_tx_bytes: number;
  genesis_hash: string | null;
  mempool_ttl_blocks: number;
  snapshot_interval: number;
  reorg_depth: number;
}

export interface BlockSummary {
  hash: string;
  parent_hash: string;
  height: number;
  timestamp: number;
  epoch: number;
  miner: string;
  transactions: number;
  proofs: number;
  wins_required: number;
  seal_bits: number;
}

export interface BlockList {
  network: string;
  tip_height: number;
  blocks: BlockSummary[];
}

export interface BlockHeader {
  version: number;
  network: string;
  height: number;
  prev_hash: string;
  merkle_root: string;
  state_root: string;
  roster_root: string;
  timestamp: number;
  epoch: number;
  beacon_hash: string;
  wins_required: number;
  seal_bits: number;
  seal_nonce: number;
  miner: string;
}

export interface Transaction {
  network: string;
  type: string;
  from: string;
  nonce: number;
  fee: number;
  payload: Record<string, unknown>;
  signature: string | null;
}

export interface SignedMove {
  network: string;
  match_id: string;
  seat: string;
  ply: number;
  san: string;
  signature: string;
}

export interface OpeningRef {
  book_version: string;
  line_id: number;
  hash: string;
  moves: string[];
}

export interface TimeControl {
  initial_ms: number;
  increment_ms: number;
}

/** A printable engine fingerprint: the registration's `engine` string, as-is. */
export type EngineFingerprint = string;

export interface MatchCertificate {
  network: string;
  match_id: string;
  epoch: number;
  round: number;
  white: string;
  black: string;
  opening: OpeningRef;
  time_control: TimeControl;
  engine_white: EngineFingerprint;
  engine_black: EngineFingerprint;
  start_signatures: Record<string, string>;
}

export interface GameResult {
  network: string;
  match_id: string;
  game_hash: string;
  result: ResultValue;
  termination: TerminationValue;
  plies: number;
  result_signatures: Record<string, string>;
}

export interface Transcript {
  certificate: MatchCertificate;
  result: GameResult;
  pgn: string;
}

export interface WinProof {
  network: string;
  proof_id: string;
  game_hash: string;
  match_id: string;
  epoch: number;
  round: number;
  winner: string;
  loser: string;
  winner_seat: Color;
  result: ResultValue;
  termination: TerminationValue;
  plies: number;
  low_ply: boolean;
  opening: OpeningRef;
  pgn: string;
  certificate: MatchCertificate;
  result_signatures: Record<string, string>;
  clock_evidence?: ClockEvidence | null;
  move_evidence?: SignedMove[] | null;
  quality_summary?: QualitySummary | null;
  timeout_claim_id?: string | null;
}

export interface Block {
  header: BlockHeader;
  coinbase: Transaction | null;
  transactions: Transaction[];
  proofs: WinProof[];
}

export interface BlockView {
  network: string;
  hash: string;
  parent_hash: string;
  height: number;
  chain_work: number;
  main_chain: boolean;
  block: Block;
}


export interface GameSummary {
  game_hash: string;
  proof_id: string;
  match_id: string | null;
  epoch: number | null;
  round: number | null;
  winner: string;
  loser: string | null;
  winner_seat: Color | null;
  result: ResultValue | null;
  termination: TerminationValue | null;
  plies: number | null;
  height: number | null;
  in_mempool: boolean;
  low_ply: boolean;
  challenge_status: ChallengeStatus;
  quality_verdict: "pass" | "fail" | null;
}

export interface GameList {
  network: string;
  games: GameSummary[];
}

export interface GameView {
  game_hash: string;
  network: string;
  proof_id: string | null;
  match_id: string | null;
  winner: string | null;
  height: number | null;
  in_mempool: boolean;
  challenge_status: ChallengeStatus;
  quality_verdict: "pass" | "fail" | null;
  quality_trace_hash: string | null;
}

/** `GET /v1/games/{game_hash}/pgn`: the move text the settlement proof carries. */
export interface GamePgn {
  game_hash: string;
  network: string;
  pgn: string;
}

export interface MatchInfo {
  match_id: string;
  network: string;
  epoch: number;
  round: number;
  seats: [string, string];
  certificate: MatchCertificate | null;
  transcript: Transcript | null;
}

export interface MempoolView {
  network: string;
  transactions: string[];
  proofs: string[];
}

export interface ChallengeView {
  challenge_id: string;
  network: string;
  proof_id: string;
  game_hash: string;
  challenger: string;
  miner: string;
  reason: string;
  evidence: string;
  bond: number;
  opened_height: number;
  deadline_height: number;
  status: ChallengeState;
  resolved_height: number | null;
  panel: string[];
}

export interface ChallengeList {
  network: string;
  challenges: ChallengeView[];
}

export interface AccountView {
  network: string;
  address: string;
  balance: number;
  immature: number;
  bonded: number;
  total: number;
  nonce: number;
  registered_until_epoch: number | null;
  endpoint: string | null;
  engine: string | null;
  public_key: string | null;
  banned_until: number;
}

export interface DifficultyPoint {
  height: number;
  wins_required: number;
  seal_bits: number;
}

export interface DifficultyView {
  network: string;
  height: number;
  wins_required: number;
  seal_bits: number;
  retarget_interval: number;
  retarget_enabled: boolean;
  history: DifficultyPoint[];
}

/** `POST /v1/faucet`: a dispensed grant, already in the mempool. */
export interface FaucetView {
  network: string;
  address: string;
  amount: number;
  txid: string;
}

/** `POST /v1/tx`: the node took a signed transaction into its mempool. */
export interface TxAccepted {
  network: string;
  txid: string;
}

/** `POST /v1/proofs`: the node took a win proof into its mempool. */
export interface ProofAccepted {
  network: string;
  proof_id: string;
}

/** One epoch's beacon and roster (`GET /v1/epochs/{e}`). */
export interface EpochView {
  epoch: number;
  network: string;
  beacon: Beacon | null;
  roster_root: string;
  roster: Record<string, RosterMemberView>;
  availability: Record<string, string>;
  quotas: Record<string, number>;
  matchable: boolean;
  ended_at_height: number | null;
}

/** A roster entry as the node exposes it: the key and the registered engine. */
export interface RosterMemberView {
  public_key: string;
  engine: string | null;
  max_games: number;
  funder?: string | null;
  subnet?: string | null;
  asn?: number | null;
  recent_opponents?: string[];
}

/** A derived epoch beacon with the reveals it commits to. */
export interface Beacon {
  network: string;
  epoch: number;
  hash: string;
  block_hash: string;
  reveals: { miner: string; commitment: string; nonce: string }[];
  drand_used: boolean;
  drand_round: number | null;
  drand_signature: string | null;
}

/** The relay's non-consensus quality verdict attached to a proof. */
export interface QualitySummary {
  policy: string;
  verdict: "pass" | "fail" | "unknown";
  trace_hash: string | null;
  engine_fingerprint: string | null;
  white_blunders: number;
  black_blunders: number;
  worst_cpl: number;
}

/** One witness signature on a clock receipt. */
export interface ClockWitnessSignature {
  witness: string;
  signature: string;
}

/** A relay receipt for one accepted post-opening move. */
export interface MoveReceipt {
  kind: "move";
  match_id: string;
  ply: number;
  seat: string;
  san: string;
  received_at_ms: number;
  white_ms: number;
  black_ms: number;
  witnesses: ClockWitnessSignature[];
}

/** A relay receipt for a flag fall: the losing seat ran out of time. */
export interface FlagReceipt {
  kind: "flag";
  match_id: string;
  ply: number;
  seat: string;
  received_at_ms: number;
  white_ms: number;
  black_ms: number;
  witnesses: ClockWitnessSignature[];
}

/** The evidence a time-forfeit proof needs (`ClockEvidence`). */
export interface ClockEvidence {
  time_control: TimeControl;
  final: FlagReceipt;
  receipts: MoveReceipt[];
}

/** `GET /v1/matches/{id}/receipts` on the relay: everything it saw of the clock. */
export interface ClockBundleView {
  match_id: string;
  time_control: TimeControl;
  turn: Color;
  white_ms: number;
  black_ms: number;
  last_ply: number;
  started: boolean;
  receipts: MoveReceipt[];
  flag: FlagReceipt | null;
}

/** One round pairing, as the relay derives it. */
export interface Matchup {
  network: string;
  match_id: string;
  epoch: number;
  round: number;
  white: string;
  black: string;
}

/** The relay's live view of one session. */
export interface RelayMatchView {
  matchup: Matchup;
  status: string;
  san_moves: string[];
  certificate: MatchCertificate;
  result: GameResult | null;
  pgn: string | null;
  time_control: TimeControl;
  white_ms: number;
  black_ms: number;
  turn: Color;
  last_ply: number;
  flagged: Color | null;
  receipts: number;
}

/** The relay's co-signed transcript: what a proof is built from. */
export type TranscriptView = Transcript;


