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

export function NodeProvider({ children }: { children: ReactNode }): ReactNode {
  const status = useResource((signal) => fetchStatus(signal), [], { pollMs: STATUS_POLL_MS });
  const params = useResource((signal) => fetchParams(signal), []);
  const network = status.data?.network ?? null;
  const value = useMemo<NodeState>(() => ({ status, params, network }), [status, params, network]);
  return <NodeContext.Provider value={value}>{children}</NodeContext.Provider>;
}

/** The chain head, the consensus parameters and their load state. */
export function useNode(): NodeState {
  const value = useContext(NodeContext);
  if (value === null) throw new Error("useNode must be used inside <NodeProvider>");
  return value;
}
