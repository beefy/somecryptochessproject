/**
 * Presentation helpers.
 *
 * Times are rendered from the node's `timestamp` seconds, never from when the
 * browser fetched the block: an explorer's job is to show chain time, and the
 * reader can see the difference between a fresh tip and a stalled node.
 */

const BYTE_UNITS = ["B", "KiB", "MiB", "GiB"] as const;

/** `0x`-free short form of a hash: first 8 and last 6 hex characters. */
export function shortHash(value: string | null | undefined, size = 8): string {
  if (value === null || value === undefined || value === "") return "-";
  if (value.length <= size * 2 + 1) return value;
  return `${value.slice(0, size)}…${value.slice(-6)}`;
}

/** Shorten an address but keep the bech32 prefix, so the network stays visible. */
export function shortAddress(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "-";
  const separator = value.indexOf("1");
  if (separator < 0 || value.length <= 20) return value;
  const prefix = value.slice(0, separator + 1);
  return `${prefix}${shortHash(value.slice(separator + 1), 6)}`;
}

/** Group an integer with thin-ish separators, e.g. `1 234 567`. */
export function formatInteger(value: number | null | undefined, locale = "en-US"): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "-";
  return new Intl.NumberFormat(locale).format(value);
}

/** Amounts are integer base units: one coin is 100 000 000 units. */
export const UNITS_PER_COIN = 100_000_000;

/** Base units as a coin amount, e.g. `50 SCCP` or `0.0005 SCCP`. */
export function formatAmount(base: number | null | undefined): string {
  if (base === null || base === undefined || !Number.isFinite(base)) return "-";
  const absolute = Math.abs(Math.trunc(base));
  const whole = Math.floor(absolute / UNITS_PER_COIN);
  const fraction = (absolute % UNITS_PER_COIN).toString().padStart(8, "0").replace(/0+$/, "");
  const text = fraction.length === 0 ? formatInteger(whole) : `${formatInteger(whole)}.${fraction}`;
  return `${base < 0 ? "-" : ""}${text} SCCP`;
}

/** Render a byte count with a binary unit. */
export function formatBytes(value: number): string {
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < BYTE_UNITS.length - 1) {
    size /= 1024;
    unit += 1;
  }
  const digits = unit === 0 ? 0 : 1;
  return `${size.toFixed(digits)} ${BYTE_UNITS[unit] as string}`;
}

/** A millisecond duration as a clock, e.g. `3:07`. */
export function formatClock(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${seconds.toString().padStart(2, "0")}`;
}

/** How long ago `seconds` (a UNIX timestamp) was, relative to `nowMs`. */
export function formatAge(seconds: number, nowMs: number): string {
  const delta = Math.round(nowMs / 1000 - seconds);
  if (!Number.isFinite(delta)) return "-";
  if (delta < -60) return `in ${formatDuration(-delta)}`;
  if (delta < 5) return "just now";
  return `${formatDuration(delta)} ago`;
}

/** A duration in seconds as `3d 4h`, `12m`, `8s`. */
export function formatDuration(totalSeconds: number): string {
  const seconds = Math.max(0, Math.round(totalSeconds));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  const days = Math.floor(hours / 24);
  return `${days}d ${hours % 24}h`;
}

/** A UNIX timestamp in seconds as an ISO-like UTC string. */
export function formatTimestamp(seconds: number): string {
  const date = new Date(seconds * 1000);
  if (Number.isNaN(date.getTime())) return "-";
  return `${date.toISOString().replace("T", " ").slice(0, 19)}Z`;
}

/** Pretty-print JSON for the "raw" drawers; never throws. */
export function prettyJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/** Copy text to the clipboard, reporting whether it worked. */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
