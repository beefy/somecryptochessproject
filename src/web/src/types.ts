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

/** A network-defined chess variant, e.g. `standard` or `chess960`. */
export type VariantName = string;

/** One variant and the asset its settled proofs mint (`GET /v1/params`). */
export interface VariantParams {
  name: VariantName;
  rules: string;
  /** `null` for standard chess: its proofs mint native ZQWX. */
  asset: string | null;
  symbol: string;
  display_name: string;
  decimals: number;
  subsidy: number;
  halving_interval: number | null;
  max_supply: number | null;
  min_plies: number;
  max_games_per_block: number;
  opening_book: string;
}

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
  /** Native ZQWX minted so far, in base units. */
  minted: number;
  /** Native ZQWX destroyed by timeout and witness slashes, when the node reports it. */
  burned?: number;
  /** Asset id -> minted base units for every non-native variant token. */
  minted_assets: Record<string, number>;
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
  /** Constant-product swap fee in basis points, paid to liquidity providers. */
  dex_fee_bps: number;
  min_plies: number;
  max_plies: number;
  nodes_per_move: number;
  book_version: string;
  /** Every settled variant, standard first; non-native entries mint assets. */
  variants: VariantParams[];
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
  /** Proof count per variant, e.g. `{standard: 1, chess960: 0}` omitted when empty. */
  variants: Record<string, number>;
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
  start_fen: string;
  variant: VariantName;
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
  engine_white: EngineFingerprint;
  engine_black: EngineFingerprint;
  variant: VariantName;
  time_control: TimeControl;
  start_signatures: Record<string, string>;
}

export interface GameResult {
  network: string;
  match_id: string;
  game_hash: string;
  result: ResultValue;
  termination: TerminationValue;
  plies: number;
  /** The folded root of the post-opening move stream (`PROTOCOL.md` 8.5). */
  move_commitment: string;
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
  /** The commit-to-the-move-stream root; the result signatures cover it. */
  move_commitment: string;
  low_ply: boolean;
  variant: VariantName;
  opening: OpeningRef;
  pgn: string;
  certificate: MatchCertificate;
  result_signatures: Record<string, string>;
  clock_evidence?: ClockEvidence | null;
  move_evidence?: MoveEvidence | null;
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

/** `GET /v1/transactions/{txid}`: where the node last saw a transaction. */
export interface TransactionRecord {
  txid: string;
  network: string;
  type: string;
  height: number | null;
  in_mempool: boolean;
  block_hash: string | null;
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
  variant: VariantName;
  height: number | null;
  in_mempool: boolean;
  low_ply: boolean;
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
  variant: VariantName;
  height: number | null;
  in_mempool: boolean;
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
  variant: VariantName;
  certificate: MatchCertificate | null;
  transcript: Transcript | null;
}

export interface MempoolView {
  network: string;
  transactions: string[];
  proofs: string[];
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
  /** The variant this account registered for. */
  variant: VariantName;
  /** Asset id -> spendable base units for every non-native token. */
  assets: Record<string, number>;
  /** Asset id -> immature base units, e.g. a C960 block reward. */
  immature_assets: Record<string, number>;
  public_key: string | null;
  banned_until: number;
  /** SCCP HTLCs this account sent or may claim (`GET /v1/accounts/{address}`). */
  htlcs: HtlcView[];
}

/** One SCCP-side hash time-locked contract. */
export interface HtlcView {
  htlc_id: string;
  network: string;
  sender: string;
  beneficiary: string;
  hashlock: string;
  timelock_height: number;
  amount: number;
  created_at_height: number;
  /** `null` for native ZQWX; a 64-hex asset id otherwise. */
  asset: string | null;
  status: "locked" | "claimed" | "refunded";
  closed_at_height: number | null;
  /** The secret a claim revealed; this is public and opens the Ethereum leg. */
  preimage: string | null;
}

export interface HtlcList {
  network: string;
  htlcs: HtlcView[];
}

/** `GET /v1/bridge`: the Ethereum environment paired with this SCCP network. */
export interface BridgeView {
  network: string;
  eth_chain_id: number;
  eth_chain_name: string;
  eth_explorer_url: string | null;
  eth_rpc_url: string | null;
  eth_htlc_contract: string | null;
  eth_confirmations: number;
  sccp_margin: number;
  configured: boolean;
}

/** One open sell-ZQWX order; fields are signed by the maker's hybrid key. */
export interface OrderView {
  order_id: string;
  network: string;
  maker_sccp: string;
  maker_eth: string;
  give_asset: "ZQWX";
  give_amount: number;
  want_asset: "ETH";
  /** Wei as a decimal string: 18 decimals exceed JavaScript's safe integers. */
  want_amount: string;
  min_sccp_timelock: number;
  expiry: number;
  nonce: number;
  public_key: string;
  signature: string;
}

/** A taker's signed announcement that the ETH leg is locked. */
export interface TakeView {
  take_id: string;
  network: string;
  order_id: string;
  taker_sccp: string;
  taker_eth: string;
  hashlock: string;
  eth_timelock: number;
  sccp_timelock_height: number;
  eth_tx_hash: string;
  nonce: number;
  public_key: string;
  signature: string;
}

export interface OrderList {
  network: string;
  orders: OrderView[];
}

export interface OrderDetail {
  network: string;
  order: OrderView;
  take: TakeView | null;
}

export interface OrderAccepted {
  network: string;
  order_id: string;
  accepted: boolean;
}

export interface TakeAccepted {
  network: string;
  take_id: string;
  accepted: boolean;
}

/** One constant-product liquidity pool (`GET /v1/pools`). */
export interface PoolView {
  pool_id: string;
  network: string;
  /** `null` for native ZQWX; a 64-hex asset id otherwise. */
  asset_a: string | null;
  asset_b: string | null;
  reserve_a: number;
  reserve_b: number;
  total_shares: number;
  /** Swap fee in basis points, paid to liquidity providers. */
  fee_bps: number;
  /** LP address -> pool shares. */
  shares: Record<string, number>;
}

export interface PoolList {
  network: string;
  pools: PoolView[];
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
  /** Whole coins, not base units: a grant of 1,000 is 1,000 ZQWX. */
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
  variant: VariantName;
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

/** One witness's hybrid signature over a stream root. */
export interface CheckpointSignature {
  witness: string;
  signature: string;
}

/**
 * A witness-quorum hybrid signature over a folded stream root
 * (`StreamCheckpoint` in Python).
 */
export interface StreamCheckpoint {
  network: string;
  kind: "moves" | "clock";
  match_id: string;
  ply: number;
  root: string;
  witnesses: CheckpointSignature[];
}

/** One ply inside a refused-checkmate bundle: no per-ply signature. */
export interface CommittedMove {
  ply: number;
  seat: string;
  san: string;
}

/** A refused-checkmate bundle: the plies plus the witness checkpoint. */
export interface MoveEvidence {
  moves: CommittedMove[];
  checkpoint: StreamCheckpoint;
}

/** The evidence a time-forfeit proof needs (`ClockEvidence`). */
export interface ClockEvidence {
  time_control: TimeControl;
  final: FlagReceipt;
  /** A suffix of the receipts, at most `CLOCK_EVIDENCE_PLIES` long. */
  receipts: MoveReceipt[];
  checkpoint: StreamCheckpoint;
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
  checkpoint: StreamCheckpoint | null;
}

/** One round pairing, as the relay derives it. */
export interface Matchup {
  network: string;
  match_id: string;
  epoch: number;
  round: number;
  white: string;
  black: string;
  variant: VariantName;
}

/** The relay's lifecycle states for a session. */
export type RelayMatchStatus = "pending" | "active" | "finished" | "abandoned";

/** The relay's live view of one session. */
export interface RelayMatchView {
  matchup: Matchup;
  status: RelayMatchStatus;
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

/** One period of a miner's derived rating history (`/v1/ratings/{address}`). */
export interface RatingPoint {
  epoch: number;
  height: number;
  rating: number;
  deviation: number;
  volatility: number;
  games: number;
  wins: number;
  draws: number;
  losses: number;
}

/** One row of the derived rating leaderboard (`/v1/ratings`). */
export interface MinerRating {
  address: string;
  rank: number;
  rating: number;
  deviation: number;
  volatility: number;
  games: number;
  wins: number;
  draws: number;
  losses: number;
  variant: VariantName;
  provisional: boolean;
  last_epoch: number;
  last_height: number;
}

/** One bar of the node's fixed-width rating histogram. */
export interface RatingBucket {
  label: string;
  low: number | null;
  high: number | null;
  count: number;
}

export interface RatingDistribution {
  buckets: RatingBucket[];
  rated: number;
  mean: number;
  median: number;
}

/**
 * `GET /v1/ratings`: Glicko-2 ratings replayed from settled proofs.
 *
 * The node derives these; there is no route that accepts a rating from a miner.
 */
export interface RatingsView {
  network: string;
  height: number;
  tip_hash: string;
  algorithm: "glicko-2";
  rating_period: "epoch";
  rated_epochs: number;
  games: number;
  miners: MinerRating[];
  distribution: RatingDistribution;
}

/** `GET /v1/ratings/{address}`: one miner's standing and its history. */
export interface MinerRatingDetail {
  network: string;
  address: string;
  rank: number | null;
  rating: number;
  deviation: number;
  volatility: number;
  games: number;
  wins: number;
  draws: number;
  losses: number;
  variant: VariantName;
  provisional: boolean;
  last_epoch: number | null;
  last_height: number | null;
  history: RatingPoint[];
}



