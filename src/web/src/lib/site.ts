/**
 * Which of the two sites this build is showing, and how to reach the other one.
 *
 * `explorer.chessproofofwork.com` and `wallet.chessproofofwork.com` serve the
 * same bundle from the same origin: two Caddy site blocks, one nginx root, one
 * release directory. The hostname is therefore the only thing that decides
 * whether `#/` is the block explorer or the wallet, and the hamburger menu links
 * across by absolute URL -- a hash change cannot cross a subdomain.
 *
 * On `localhost` the absolute links would send a developer to production, so a
 * local build switches in-app with a hash instead. Point `VITE_EXPLORER_ORIGIN`
 * and `VITE_WALLET_ORIGIN` at something else to test a staging pair.
 */

export type Site = "explorer" | "wallet";

export const SITE_LABELS: Readonly<Record<Site, string>> = {
  explorer: "Explorer",
  wallet: "Wallet",
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
};

/** Hostnames that mean "this is a development server". */
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "::1", "0.0.0.0"]);

/** The hash that opens `site`'s landing page. */
export function siteHash(site: Site): string {
  return site === "wallet" ? "#/wallet" : "#/";
}

/** Which site a hostname serves; anything not `wallet.*` is the explorer. */
export function siteForHostname(hostname: string): Site {
  return hostname === "wallet" || hostname.startsWith("wallet.") ? "wallet" : "explorer";
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
 * Where the "open the other site" link points.
 *
 * In production that is the other subdomain's origin, so the wallet really is at
 * `wallet.chessproofofwork.com` and a bookmark of it stays a bookmark. In a local
 * build it is the other site's hash on the current server, which is the only way
 * to exercise the switch without publishing anything.
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
