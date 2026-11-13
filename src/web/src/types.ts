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
  name: string;
  hash: string;
}

export interface TimeControl {
  initial_ms: number;
  increment_ms: number;
}

export interface EngineFingerprint {
  name: string;
  version: string;
  hash?: string;
}

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

