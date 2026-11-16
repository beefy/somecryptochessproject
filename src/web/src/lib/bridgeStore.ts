/**
 * Recoverable bridge state.
 *
 * A taker's preimage is generated before the ETH transaction and is the only
 * thing that can claim the SCCP leg. Losing it to a page reload would strand the
 * swap until the ETH refund, so the minimum needed to resume is kept per order.
 * It is not a wallet seed and it can only open this one hashlock; locking the
 * wallet still wipes the signing key. A completed or refunded swap is removed.
 */

export interface LocalSwap {
  role: "buy" | "sell";
  orderId: string;
  /** The SCCP account that took the order; guards recovery across wallets. */
  takerSccp?: string;
  hashlock: string;
  /** Only a buyer holds this before the claim; a seller learns it from the chain. */
  preimage?: string;
  ethTxHash?: string;
  ethLockId?: string;
  ethTimelock?: number;
  sccpTimelockHeight?: number;
  createdAt: number;
}

const KEY = "sccp.bridge.swaps.v1";
/** A swap older than this cannot still be open; drop its secret. */
const MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;

function read(): Record<string, LocalSwap> {
  if (typeof localStorage === "undefined") return {};
  try {
    const raw = localStorage.getItem(KEY);
    if (raw === null) return {};
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return {};
    const now = Date.now();
    const swaps: Record<string, LocalSwap> = {};
    for (const [key, value] of Object.entries(parsed as Record<string, LocalSwap>)) {
      if (typeof value?.createdAt === "number" && now - value.createdAt <= MAX_AGE_MS) {
        swaps[key] = value;
      }
    }
    return swaps;
  } catch {
    return {};
  }
}

function write(swaps: Record<string, LocalSwap>): void {
  if (typeof localStorage === "undefined") return;
  try {
    localStorage.setItem(KEY, JSON.stringify(swaps));
  } catch {
    // A private-mode tab may refuse storage; the swap still works for this page.
  }
}

export function loadSwaps(): Record<string, LocalSwap> {
  return read();
}

export function swapFor(orderId: string): LocalSwap | undefined {
  return read()[orderId];
}

export function saveSwap(swap: LocalSwap): void {
  const swaps = read();
  swaps[swap.orderId] = swap;
  write(swaps);
}

export function forgetSwap(orderId: string): void {
  const swaps = read();
  delete swaps[orderId];
  write(swaps);
}
