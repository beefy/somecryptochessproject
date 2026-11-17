/**
 * The five named views of the one web application.
 *
 * There is one public origin, `https://app.proofofchess.com`, and one bundle. A view
 * is selected by the hash route (`#/`, `#/wallet`, `#/play`, `#/dex`,
 * `#/bridge`), not by a hostname, so every view is a direct, bookmarkable URL on
 * the same origin and one TLS certificate covers all of them. The hamburger
 * switches by hash so the unlocked in-memory key stays in this tab.
 */

export type Site = "explorer" | "wallet" | "play" | "dex" | "bridge";

export const SITE_LABELS: Readonly<Record<Site, string>> = {
  explorer: "Explorer",
  wallet: "Wallet",
  play: "Play",
  dex: "DEX",
  bridge: "Bridge",
};

/** The browser tab title for each view. */
export const SITE_TITLES: Readonly<Record<Site, string>> = {
  explorer: "SCCP explorer",
  wallet: "SCCP wallet",
  play: "SCCP play",
  dex: "SCCP DEX",
  bridge: "SCCP bridge",
};

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
