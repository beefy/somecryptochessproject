/**
 * Hash routing, so the bundle works when the node serves it from any static
 * path without a server-side rewrite rule: `#/blocks/12` never reaches the node
 * as a path, but it is still a real, shareable URL.
 */

import { useEffect, useState } from "react";

export type Route =
  | { name: "dashboard" }
  | { name: "blocks" }
  | { name: "block"; reference: string }
  | { name: "transaction"; txid: string }
  | { name: "games" }
  | { name: "game"; gameHash: string }
  | { name: "ratings" }
  | { name: "miner"; address: string }
  | { name: "account"; address: string }
  | { name: "wallet" }
  | { name: "play" }
  | { name: "dex" }
  | { name: "dexLiquidity" }
  | { name: "dexCreate" }
  | { name: "bridge" }
  | { name: "bridgeSell" }
  | { name: "bridgeActivity" }
  | { name: "walletSend" }
  | { name: "walletReceive" }
  | { name: "walletFaucet" }
  | { name: "walletHistory" }
  | { name: "walletSettings" }
  | { name: "notFound"; path: string };


/** Parse `location.hash` (or a bare path) into a route. */
export function parseRoute(hash: string): Route {
  const path = hash.replace(/^#/, "").replace(/\/+$/, "");
  const segments = path.split("/").filter((segment) => segment.length > 0);
  const [head, second] = segments;

  if (head === undefined) return { name: "dashboard" };
  switch (head) {
    case "blocks":
      return second === undefined ? { name: "blocks" } : { name: "block", reference: decode(second) };
    case "transactions":
      return second === undefined
        ? { name: "notFound", path }
        : { name: "transaction", txid: decode(second) };
    case "games":
      return second === undefined ? { name: "games" } : { name: "game", gameHash: decode(second) };
    case "ratings":
      return { name: "ratings" };
    case "miners":
      return second === undefined ? { name: "notFound", path } : { name: "miner", address: decode(second) };
    // The table has its own page now. The old wallet tab URL is an alias, so a
    // bookmark or an old link does not break.
    case "play":
      return { name: "play" };
    case "dex":
      if (second === undefined) return { name: "dex" };
      if (segments.length > 2) return { name: "notFound", path };
      switch (second) {
        case "liquidity":
          return { name: "dexLiquidity" };
        case "create":
          return { name: "dexCreate" };
        default:
          return { name: "notFound", path };
      }
    case "bridge":
      if (second === undefined) return { name: "bridge" };
      if (segments.length > 2) return { name: "notFound", path };
      switch (second) {
        case "sell":
          return { name: "bridgeSell" };
        case "activity":
          return { name: "bridgeActivity" };
        default:
          return { name: "notFound", path };
      }
    case "accounts":
      return second === undefined ? { name: "notFound", path } : { name: "account", address: decode(second) };
    case "wallet": {
      if (second === undefined) return { name: "wallet" };
      if (segments.length > 2) return { name: "notFound", path };
      switch (second) {
        case "send":
          return { name: "walletSend" };
        case "receive":
          return { name: "walletReceive" };
        case "faucet":
          return { name: "walletFaucet" };
        case "play":
          return { name: "play" };
        case "history":
          return { name: "walletHistory" };
        case "settings":
          return { name: "walletSettings" };
        default:
          return { name: "notFound", path };
      }
    }
    default:
      return { name: "notFound", path };

  }
}

function decode(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

/** The hash for a route, e.g. `#/blocks/12`. */
export function routeHash(route: Route): string {
  switch (route.name) {
    case "dashboard":
      return "#/";
    case "blocks":
      return "#/blocks";
    case "block":
      return `#/blocks/${encodeURIComponent(route.reference)}`;
    case "transaction":
      return `#/transactions/${encodeURIComponent(route.txid)}`;
    case "games":
      return "#/games";
    case "game":
      return `#/games/${encodeURIComponent(route.gameHash)}`;
    case "ratings":
      return "#/ratings";
    case "miner":
      return `#/miners/${encodeURIComponent(route.address)}`;
    case "account":
      return `#/accounts/${encodeURIComponent(route.address)}`;
    case "wallet":
      return "#/wallet";
    case "play":
      return "#/play";
    case "dex":
      return "#/dex";
    case "dexLiquidity":
      return "#/dex/liquidity";
    case "dexCreate":
      return "#/dex/create";
    case "bridge":
      return "#/bridge";
    case "bridgeSell":
      return "#/bridge/sell";
    case "bridgeActivity":
      return "#/bridge/activity";
    case "walletSend":
      return "#/wallet/send";
    case "walletReceive":
      return "#/wallet/receive";
    case "walletFaucet":
      return "#/wallet/faucet";
    case "walletHistory":
      return "#/wallet/history";
    case "walletSettings":
      return "#/wallet/settings";
    case "notFound":
      return `#/${route.path}`;
  }
}

/** Navigate to `hash`, pushing a history entry the back button can undo. */
export function navigate(hash: string): void {
  if (window.location.hash === hash) return;
  window.location.hash = hash;
}

/** The current route, kept in sync with the address bar. */
export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));
  useEffect(() => {
    const update = (): void => {
      setRoute(parseRoute(window.location.hash));
      window.scrollTo({ top: 0 });
    };
    window.addEventListener("hashchange", update);
    return () => {
      window.removeEventListener("hashchange", update);
    };
  }, []);
  return route;
}

/** Which nav tab is active for `route`. */
export type Section =
  | "dashboard"
  | "blocks"
  | "games"
  | "ratings"
  | "account"
  | "wallet"
  | "play"
  | "dex"
  | "dexLiquidity"
  | "dexCreate"
  | "bridge"
  | "bridgeSell"
  | "bridgeActivity"
  | "walletSend"
  | "walletReceive"
  | "walletFaucet"
  | "walletHistory"
  | "walletSettings";

export function activeSection(route: Route): Section {
  switch (route.name) {
    case "blocks":
    case "block":
    case "transaction":
      return "blocks";
    case "games":
    case "game":
      return "games";
    case "ratings":
    case "miner":
      return "ratings";
    case "account":
      return "account";
    case "wallet":
      return "wallet";
    case "play":
      return "play";
    case "dex":
      return "dex";
    case "dexLiquidity":
      return "dexLiquidity";
    case "dexCreate":
      return "dexCreate";
    case "bridge":
      return "bridge";
    case "bridgeSell":
      return "bridgeSell";
    case "bridgeActivity":
      return "bridgeActivity";
    case "walletSend":
      return "walletSend";
    case "walletReceive":
      return "walletReceive";
    case "walletFaucet":
      return "walletFaucet";
    case "walletHistory":
      return "walletHistory";
    case "walletSettings":
      return "walletSettings";
    default:
      return "dashboard";
  }
}
