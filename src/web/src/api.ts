/**
 * The node API client.
 *
 * The explorer talks to `/v1/...` on whatever origin served it: `sccp serve --web`
 * mounts the built bundle on the node itself, and the Vite dev server proxies
 * `/v1` to `SCCP_NODE`. `VITE_SCCP_API` overrides both, which is what a hosted
 * static build uses to point at a remote node.
 *
 * Every response is typed in `types.ts`; nothing here guesses at a shape.
 */

import type {
  AccountView,
  BlockList,
  BlockView,
  ChallengeList,
  DifficultyPoint,
  GameList,
  GameView,
  MatchInfo,
  MempoolView,
  NetworkParams,
  NodeStatus,
  Transcript,
} from "./types";

/** Base URL with any trailing slash removed; empty means "same origin". */
export const API_BASE: string = (import.meta.env.VITE_SCCP_API ?? "").replace(/\/+$/, "");

/** An API call failed; `status` is the HTTP status, or 0 for a transport error. */
export class ApiError extends Error {
  override name = "ApiError";

  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

/** The node answered 404; views render this as "not found", not as a fault. */
export class NotFoundError extends ApiError {
  override name = "NotFoundError";

  constructor(message: string) {
    super(message, 404);
  }
}

interface RequestOptions {
  signal?: AbortSignal;
  method?: "GET" | "POST";
  body?: unknown;
}

/** Pull the node's error text out of a FastAPI error body. */
function describeFailure(body: unknown, status: number): string {
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      const parts = detail.map((item) => {
        if (typeof item === "object" && item !== null && "msg" in item) {
          const { msg, loc } = item as { msg: unknown; loc?: unknown };
          const where = Array.isArray(loc) ? loc.join(".") : "";
          return where ? `${where}: ${String(msg)}` : String(msg);
        }
        return String(item);
      });
      return parts.join("; ");
    }
  }
  return `request failed with HTTP ${status}`;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { signal, method = "GET", body } = options;
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      signal,
      headers: body === undefined ? undefined : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(`cannot reach the node at ${API_BASE === "" ? "this origin" : API_BASE}`, 0);
  }
  const payload = await response.json().catch(() => undefined);
  if (!response.ok) {
    const message = describeFailure(payload, response.status);
    if (response.status === 404) throw new NotFoundError(message);
    throw new ApiError(message, response.status);
  }
  return payload as T;
}

/** `GET /v1/status`: height, tip, peers, pool sizes. */
export function fetchStatus(signal?: AbortSignal): Promise<NodeStatus> {
  return request<NodeStatus>("/v1/status", { signal });
}

/** `GET /v1/params`: the network's consensus constants. */
export function fetchParams(signal?: AbortSignal): Promise<NetworkParams> {
  return request<NetworkParams>("/v1/params", { signal });
}

/** `GET /v1/blocks`: the newest blocks, tip first. */
export function fetchBlocks(
  options: { limit?: number; beforeHeight?: number; signal?: AbortSignal } = {},
): Promise<BlockList> {
  const query = new URLSearchParams();
  if (options.limit !== undefined) query.set("limit", String(options.limit));
  if (options.beforeHeight !== undefined) query.set("before_height", String(options.beforeHeight));
  const suffix = query.size === 0 ? "" : `?${query.toString()}`;
  return request<BlockList>(`/v1/blocks${suffix}`, { signal: options.signal });
}

/** `GET /v1/blocks/{height-or-hash}`: one block plus its position. */
export function fetchBlock(reference: string | number, signal?: AbortSignal): Promise<BlockView> {
  return request<BlockView>(`/v1/blocks/${encodeURIComponent(String(reference))}`, { signal });
}

/** `GET /v1/games`: pending proofs first, then newest settle first. */
export function fetchGames(signal?: AbortSignal): Promise<GameList> {
  return request<GameList>("/v1/games", { signal });
}

/** `GET /v1/games/{game_hash}`: one game's settlement state. */
export function fetchGame(gameHash: string, signal?: AbortSignal): Promise<GameView> {
  return request<GameView>(`/v1/games/${encodeURIComponent(gameHash)}`, { signal });
}

/** `GET /v1/matches/{match_id}`: seats, certificate and transcript. */
export function fetchMatch(matchIdText: string, signal?: AbortSignal): Promise<MatchInfo> {
  return request<MatchInfo>(`/v1/matches/${encodeURIComponent(matchIdText)}`, { signal });
}

/** `GET /v1/matches/{match_id}/transcript`: the signed record of one match. */
export function fetchTranscript(matchIdText: string, signal?: AbortSignal): Promise<Transcript> {
  return request<Transcript>(`/v1/matches/${encodeURIComponent(matchIdText)}/transcript`, {
    signal,
  });
}

/** `GET /v1/mempool`: ids waiting for a block. */
export function fetchMempool(signal?: AbortSignal): Promise<MempoolView> {
  return request<MempoolView>("/v1/mempool", { signal });
}

/** `GET /v1/challenges`: fraud challenges and their status. */
export function fetchChallenges(signal?: AbortSignal): Promise<ChallengeList> {
  return request<ChallengeList>("/v1/challenges", { signal });
}

/** `GET /v1/accounts/{address}`: balances, nonce and registration. */
export function fetchAccount(address: string, signal?: AbortSignal): Promise<AccountView> {
  return request<AccountView>(`/v1/accounts/${encodeURIComponent(address)}`, { signal });
}

/** `GET /v1/difficulty`: the retarget history. */
export function fetchDifficulty(signal?: AbortSignal): Promise<DifficultyPoint[]> {
  return request<DifficultyPoint[]>("/v1/difficulty", { signal });
}

