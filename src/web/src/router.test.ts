/**
 * Hash routes for the ratings pages.
 *
 * The miner page is a real, shareable URL (`#/miners/<address>`), so parsing and
 * rendering it back must round-trip exactly and the bare parent path must stay a
 * 404 rather than an empty page.
 */

import { describe, expect, it } from "vitest";

import { activeSection, parseRoute, routeHash } from "./router";

const ADDRESS = "sccpt1qexampleaddress0000000000000000000000";

describe("ratings routes", () => {
  it("parses the leaderboard", () => {
    expect(parseRoute("#/ratings")).toEqual({ name: "ratings" });
    expect(routeHash({ name: "ratings" })).toBe("#/ratings");
  });

  it("round-trips a miner address", () => {
    const route = parseRoute(`#/miners/${ADDRESS}`);
    expect(route).toEqual({ name: "miner", address: ADDRESS });
    expect(routeHash(route)).toBe(`#/miners/${ADDRESS}`);
  });

  it("keeps a bare miners path out of the router", () => {
    expect(parseRoute("#/miners")).toEqual({ name: "notFound", path: "/miners" });
  });

  it("lights up the ratings tab for both pages", () => {
    expect(activeSection({ name: "ratings" })).toBe("ratings");
    expect(activeSection({ name: "miner", address: ADDRESS })).toBe("ratings");
  });
});

describe("retired routes", () => {
  it("keeps the old live page out of the router", () => {
    // The in-progress page is gone; an old bookmark lands on the 404 view
    // instead of rendering a page that no longer exists.
    expect(parseRoute("#/live")).toEqual({ name: "notFound", path: "/live" });
    expect(parseRoute("#/live/12")).toEqual({ name: "notFound", path: "/live/12" });
  });
});

describe("transaction routes", () => {
  const TXID = "ab".repeat(32);

  it("round-trips a transaction id", () => {
    const route = parseRoute(`#/transactions/${TXID}`);
    expect(route).toEqual({ name: "transaction", txid: TXID });
    expect(routeHash(route)).toBe(`#/transactions/${TXID}`);
  });

  it("keeps a bare transactions path out of the router", () => {
    expect(parseRoute("#/transactions")).toEqual({
      name: "notFound",
      path: "/transactions",
    });
  });

  it("lights up the Blocks tab", () => {
    expect(activeSection({ name: "transaction", txid: TXID })).toBe("blocks");
  });
});

describe("wallet routes", () => {
  it("parses the wallet landing page", () => {
    expect(parseRoute("#/wallet")).toEqual({ name: "wallet" });
    expect(routeHash({ name: "wallet" })).toBe("#/wallet");
  });

  it("round-trips each wallet section", () => {
    for (const [path, name] of [
      ["#/wallet/send", "walletSend"],
      ["#/wallet/receive", "walletReceive"],
      ["#/wallet/faucet", "walletFaucet"],
      ["#/wallet/history", "walletHistory"],
      ["#/wallet/settings", "walletSettings"],
    ] as const) {
      const route = parseRoute(path);
      expect(route).toEqual({ name });
      expect(routeHash(route)).toBe(path);
    }
  });

  it("moves the table to its own route and keeps the old URL as an alias", () => {
    expect(parseRoute("#/play")).toEqual({ name: "play" });
    expect(parseRoute("#/wallet/play")).toEqual({ name: "play" });
    expect(routeHash({ name: "play" })).toBe("#/play");
    expect(activeSection({ name: "play" })).toBe("play");
  });

  it("parses the DEX and Bridge sites", () => {
    expect(parseRoute("#/dex")).toEqual({ name: "dex" });
    expect(parseRoute("#/bridge")).toEqual({ name: "bridge" });
    expect(routeHash({ name: "dex" })).toBe("#/dex");
    expect(routeHash({ name: "bridge" })).toBe("#/bridge");
    expect(activeSection({ name: "dex" })).toBe("dex");
    expect(activeSection({ name: "bridge" })).toBe("bridge");
  });

  it("round-trips each DEX tab", () => {
    for (const [path, name] of [
      ["#/dex", "dex"],
      ["#/dex/liquidity", "dexLiquidity"],
      ["#/dex/create", "dexCreate"],
    ] as const) {
      const route = parseRoute(path);
      expect(route).toEqual({ name });
      expect(routeHash(route)).toBe(path);
      expect(activeSection(route)).toBe(name);
    }
  });

  it("round-trips each Bridge tab", () => {
    for (const [path, name] of [
      ["#/bridge", "bridge"],
      ["#/bridge/sell", "bridgeSell"],
      ["#/bridge/activity", "bridgeActivity"],
    ] as const) {
      const route = parseRoute(path);
      expect(route).toEqual({ name });
      expect(routeHash(route)).toBe(path);
      expect(activeSection(route)).toBe(name);
    }
  });

  it("keeps unknown DEX and Bridge tabs out of the router", () => {
    expect(parseRoute("#/dex/swap/extra")).toEqual({ name: "notFound", path: "/dex/swap/extra" });
    expect(parseRoute("#/dex/nope")).toEqual({ name: "notFound", path: "/dex/nope" });
    expect(parseRoute("#/bridge/buy")).toEqual({ name: "notFound", path: "/bridge/buy" });
    expect(parseRoute("#/bridge/nope")).toEqual({ name: "notFound", path: "/bridge/nope" });
  });

  it("refuses unknown and over-long wallet paths", () => {
    expect(parseRoute("#/wallet/nope")).toEqual({ name: "notFound", path: "/wallet/nope" });
    expect(parseRoute("#/wallet/send/extra")).toEqual({
      name: "notFound",
      path: "/wallet/send/extra",
    });
  });

  it("lights up the matching tab", () => {
    expect(activeSection({ name: "wallet" })).toBe("wallet");
    expect(activeSection({ name: "walletSend" })).toBe("walletSend");
    expect(activeSection({ name: "walletSettings" })).toBe("walletSettings");
  });
});
