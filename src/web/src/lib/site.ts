/**
 * Which named site this build is showing, and how to reach the others.
 *
 * `explorer`, `wallet`, `play`, `dex` and `bridge` subdomains serve the same
 * bundle from the same origin: Caddy site blocks, one nginx root and one release
 * directory. The hostname decides what `#/` means. The hamburger switches with a
 * hash rather than a cross-origin navigation so the unlocked in-memory key stays
 * in this tab; each name is still a direct, bookmarkable entry point.
 *
 * On `localhost` the same hash switch is the only sensible option. Point a
 * `VITE_*_ORIGIN` variable at something else to test a staging group.
 */

export type Site = "explorer" | "wallet" | "play" | "dex" | "bridge";

export const SITE_LABELS: Readonly<Record<Site, string>> = {
  explorer: "Explorer",
  wallet: "Wallet",
  play: "Play",
  dex: "DEX",
  bridge: "Bridge",
};

/** Where each site lives in production, unless the build overrides it. */
export const SITE_ORIGINS: Readonly<Record<Site, string>> = {
  explorer: (import.meta.env.VITE_EXPLORER_ORIGIN ?? "https://explorer.chessproofofwork.com").replace(
    /\/+$/,
    "",
  ),
  wallet: (import.meta.env.VITE_WALLET_ORIGIN ?? "https://wallet.chessproofofwork.com").replace(
    /\/+$/,
    "",
  ),
  play: (import.meta.env.VITE_PLAY_ORIGIN ?? "https://play.chessproofofwork.com").replace(
    /\/+$/,
    "",
  ),
  dex: (import.meta.env.VITE_DEX_ORIGIN ?? "https://dex.chessproofofwork.com").replace(
    /\/+$/,
    "",
  ),
  bridge: (import.meta.env.VITE_BRIDGE_ORIGIN ?? "https://bridge.chessproofofwork.com").replace(
    /\/+$/,
    "",
  ),
};

/** Hostnames that mean "this is a development server". */
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "::1", "0.0.0.0"]);

/** The hash that opens `site`'s landing page. */
export function siteHash(site: Site): string {
  switch (site) {
    case "wallet":
      return "#/wallet";
    case "play":
      return "#/play";
    case "dex":
      return "#/dex";
    case "bridge":
      return "#/bridge";
    case "explorer":
      return "#/";
  }
}

/** Which site a hostname serves; anything unrecognised is the explorer. */
export function siteForHostname(hostname: string): Site {
  const host = hostname.toLowerCase();
  for (const site of ["wallet", "play", "dex", "bridge"] as const) {
    if (host === site || host.startsWith(`${site}.`)) return site;
  }
  return "explorer";
}

/** The site this tab is on. */
export function currentSite(hostname: string | undefined = globalThis.location?.hostname): Site {
  return siteForHostname(hostname ?? "");
}

export function isLocalDevelopment(hostname: string | undefined = globalThis.location?.hostname): boolean {
  const host = (hostname ?? "").toLowerCase();
  return LOCAL_HOSTS.has(host) || host.endsWith(".local") || host.endsWith(".localhost");
}

/**
 * Where a direct link to `site` points.
 *
 * The app menu does not use this: leaving the origin would drop the unlocked
 * keypair, which is deliberately memory-only. It stays for documentation links
 * and for anyone who wants a real subdomain URL.
 */
export function crossSiteHref(
  site: Site,
  options: { hostname?: string; local?: boolean } = {},
): string {
  const hostname = options.hostname ?? globalThis.location?.hostname;
  const local = options.local ?? isLocalDevelopment(hostname);
  if (local) return siteHash(site);
  return `${SITE_ORIGINS[site]}/`;
}

/**
 * A site origin plus a hash, with exactly one slash between them.
 *
 * `crossSiteHref` returns an origin with a trailing slash (`https://host/`) and a
 * hash starts with `#`, so naive concatenation leaves `https://host//#/wallet`
 * in the address bar. This is the one place that joins the two.
 */
export function hashUrl(base: string, hash: string): string {
  return `${base.replace(/\/+$/, "")}/${hash.replace(/^\/+/, "")}`;
}
