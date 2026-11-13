/**
 * The relay client: the API in `sccp/miner/matchmaker.py`.
 *
 * A seat's whole conversation with a real match happens here — join by signing
 * the certificate, publish signed moves, co-sign the result, fetch the
 * transcript. The relay is a separate service from the node (default
 * `http://127.0.0.1:8646`), so it needs either `VITE_SCCP_RELAY` at build time or
 * a reverse proxy, plus `--origin` on `sccp matchmaker run` for CORS.
 *
 * Every mutation carries a signature made with a key the relay already knows
 * from the roster; the relay never holds a session for us, and it never signs
 * anything we can forge (its receipts are its own witness identity).
 */

import type {
  ClockBundleView,
  RelayMatchView,
  TranscriptView,
} from "../types";

/** Base URL with any trailing slash removed. */
export const RELAY_BASE: string = (
  import.meta.env.VITE_SCCP_RELAY ?? "http://127.0.0.1:8646"
).replace(/\/+$/, "");

/** A relay call failed; `status` is the HTTP status, or 0 for a transport error. */
export class RelayError extends Error {
  override name = "RelayError";

  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

interface RequestOptions {
  signal?: AbortSignal;
  method?: "GET" | "POST";
  body?: unknown;
}

function describeFailure(body: unknown, status: number): string {
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((item) => {
          if (typeof item === "object" && item !== null && "msg" in item) {
            const { msg, loc } = item as { msg: unknown; loc?: unknown };
            const where = Array.isArray(loc) ? loc.join(".") : "";
            return where ? `${where}: ${String(msg)}` : String(msg);
          }
          return String(item);
        })
        .join("; ");
    }
  }
  return `the relay answered HTTP ${status}`;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { signal, method = "GET", body } = options;
  let response: Response;
  try {
    response = await fetch(`${RELAY_BASE}${path}`, {
      method,
      signal,
      headers: body === undefined ? undefined : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new RelayError(`cannot reach the relay at ${RELAY_BASE}`, 0);
  }
  const payload = await response.json().catch(() => undefined);
  if (!response.ok) throw new RelayError(describeFailure(payload, response.status), response.status);
  return payload as T;
}

export interface RelayStatus {
  network: string;
  epoch: number;
  matches: number;
  open: number;
  round: number | null;
  round_seconds: number;
  timed: boolean;
  time_control: { initial_ms: number; increment_ms: number };
  witness: string | null;
}

/** `GET /v1/status`: the epoch, the beacon and the clock policy. */
export function relayStatus(signal?: AbortSignal): Promise<RelayStatus> {
  return request<RelayStatus>("/v1/status", { signal });
}

/** `GET /v1/matches`: every session, pending first by creation time. */
export function relayMatches(signal?: AbortSignal): Promise<RelayMatchView[]> {
  return request<RelayMatchView[]>("/v1/matches", { signal });
}

/** `GET /v1/matches/{match_id}`: one session with its live clocks. */
export function relayMatch(matchId: string, signal?: AbortSignal): Promise<RelayMatchView> {
  return request<RelayMatchView>(`/v1/matches/${encodeURIComponent(matchId)}`, { signal });
}

/** `GET /v1/matches/{match_id}/receipts`: the witness-signed clock bundle. */
export function relayReceipts(matchId: string, signal?: AbortSignal): Promise<ClockBundleView> {
  return request<ClockBundleView>(`/v1/matches/${encodeURIComponent(matchId)}/receipts`, {
    signal,
  });
}

/** `GET /v1/matches/{match_id}/transcript`: the co-signed record of the game. */
export function relayTranscript(matchId: string, signal?: AbortSignal): Promise<TranscriptView> {
  return request<TranscriptView>(`/v1/matches/${encodeURIComponent(matchId)}/transcript`, {
    signal,
  });
}

/** `POST /v1/matches/{match_id}/join`: present this seat's start signature. */
export function relayJoin(
  matchId: string,
  fields: { seat: string; publicKey: string; signature: string },
  signal?: AbortSignal,
): Promise<RelayMatchView> {
  return request<RelayMatchView>(`/v1/matches/${encodeURIComponent(matchId)}/join`, {
    method: "POST",
    body: { seat: fields.seat, public_key: fields.publicKey, signature: fields.signature },
    signal,
  });
}

/** `POST /v1/matches/{match_id}/move`: one signed, clocked move. */
export function relayMove(
  matchId: string,
  fields: {
    seat: string;
    san: string;
    network: string;
    ply: number;
    clockMs: number;
    signature: string;
  },
  signal?: AbortSignal,
): Promise<RelayMatchView> {
  return request<RelayMatchView>(`/v1/matches/${encodeURIComponent(matchId)}/move`, {
    method: "POST",
    body: {
      seat: fields.seat,
      san: fields.san,
      network: fields.network,
      ply: fields.ply,
      clock_ms: fields.clockMs,
      signature: fields.signature,
    },
    signal,
  });
}

/** `POST /v1/matches/{match_id}/resign`: only the side to move may resign. */
export function relayResign(
  matchId: string,
  seat: string,
  signal?: AbortSignal,
): Promise<RelayMatchView> {
  return request<RelayMatchView>(`/v1/matches/${encodeURIComponent(matchId)}/resign`, {
    method: "POST",
    body: { seat },
    signal,
  });
}

/** `POST /v1/matches/{match_id}/result`: co-sign the result the relay recorded. */
export function relaySignResult(
  matchId: string,
  fields: { seat: string; signature: string },
  signal?: AbortSignal,
): Promise<RelayMatchView> {
  return request<RelayMatchView>(`/v1/matches/${encodeURIComponent(matchId)}/result`, {
    method: "POST",
    body: { seat: fields.seat, signature: fields.signature },
    signal,
  });
}
