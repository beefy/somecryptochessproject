import type { HybridKeypair } from "../../lib/keys";
import type { Token } from "../../lib/assets";
import type { Transaction } from "../../lib/tx";
import type { AccountView, NetworkParams, PoolView } from "../../types";

/** A prepared transaction plus the fields the review screen shows. */
export interface Review {
  tx: Transaction;
  title: string;
  rows: [string, string][];
}

/** Everything a DEX panel needs from the unlocked desk. */
export interface DeskProps {
  address: string;
  network: string;
  keypair: HybridKeypair;
  account: AccountView;
  params: NetworkParams;
  pools: PoolView[];
  tokens: Token[];
  nonce: number;
  reload: () => void;
  onReview: (review: Review) => void;
}
