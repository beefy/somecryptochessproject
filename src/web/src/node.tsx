/**
 * One place that knows which chain this tab is talking to.
 *
 * The header polls `/v1/status` every few seconds; views read the same object
 * instead of fetching it again, so the height in the title bar and the height on
 * the dashboard can never disagree.
 */

import { createContext, useContext, useMemo, type ReactNode } from "react";

import { fetchParams, fetchStatus } from "./api";
import { useResource, type Resource } from "./useResource";
import type { NetworkParams, NodeStatus } from "./types";

export interface NodeState {
  status: Resource<NodeStatus>;
  params: Resource<NetworkParams>;
  /** The network id from `/v1/status`, or `null` while the first call is in flight. */
  network: string | null;
}

const NodeContext = createContext<NodeState | null>(null);

/** How often the header re-reads the chain head. */
const STATUS_POLL_MS = 5000;

/**
 * The parameters for `network`, or none while a re-read is in flight.
 *
 * `/v1/status` polls and `/v1/params` does not, so a node restarted on another
 * chain would otherwise leave devnet's epoch length next to testnet's height.
 * Until the re-read lands the old document is worse than no document: every
 * epoch boundary and every verifier binding would be computed from the wrong
 * network.
 */
export function paramsForNetwork<T extends { network_id: string }>(
  params: Resource<T>,
  network: string | null,
): Resource<T> {
  if (params.data === undefined || network === null || params.data.network_id === network) {
    return params;
  }
  return { ...params, data: undefined };
}

export function NodeProvider({ children }: { children: ReactNode }): ReactNode {
  const status = useResource((signal) => fetchStatus(signal), [], { pollMs: STATUS_POLL_MS });
  const network = status.data?.network ?? null;
  const params = useResource((signal) => fetchParams(signal), [network]);
  const value = useMemo<NodeState>(
    () => ({ status, params: paramsForNetwork(params, network), network }),
    [status, params, network],
  );
  return <NodeContext.Provider value={value}>{children}</NodeContext.Provider>;
}

/** The chain head, the consensus parameters and their load state. */
export function useNode(): NodeState {
  const value = useContext(NodeContext);
  if (value === null) throw new Error("useNode must be used inside <NodeProvider>");
  return value;
}
