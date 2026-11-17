import type { HybridKeypair } from "../../lib/keys";
import type {
  AccountView,
  BridgeView,
  NetworkParams,
  NodeStatus,
  OrderView,
} from "../../types";

/** Everything every bridge panel needs from the page's one wallet and node. */
export interface BridgeContext {
  network: string;
  address: string;
  keypair: HybridKeypair;
  account: AccountView;
  params: NetworkParams;
  status: NodeStatus;
  bridge: BridgeView;
  orders: OrderView[];
  nonce: number;
  reload: () => void;
  setPendingNonce: (nonce: number | null) => void;
  notify: (message: string) => void;
  ethAccount: string | null;
  connectEth: () => Promise<string | null>;
}
