/**
 * Hash routes for the Elo pages.
 *
 * The miner page is a real, shareable URL (`#/miners/<address>`), so parsing and
 * rendering it back must round-trip exactly and the bare parent path must stay a
 * 404 rather than an empty page.
 */

import { describe, expect, it } from "vitest";

import { activeSection, parseRoute, routeHash } from "./router";

const ADDRESS = "sccpt1qexampleaddress0000000000000000000000";

describe("Elo routes", () => {
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

  it("lights up the Elo tab for both pages", () => {
    expect(activeSection({ name: "ratings" })).toBe("ratings");
    expect(activeSection({ name: "miner", address: ADDRESS })).toBe("ratings");
  });
});

describe("wallet routes", () => {
  it("parses the wallet landing page", () => {
    expect(parseRoute("#/wallet")).toEqual({ name: "wallet" });
    expect(routeHash({ name: "wallet" })).toBe("#/wallet");
  });

  it("round-trips each wallet section", () => {
    for (const [path, name] of [
      ["#/wallet/play", "walletPlay"],
      ["#/wallet/send", "walletSend"],
      ["#/wallet/receive", "walletReceive"],
      ["#/wallet/history", "walletHistory"],
      ["#/wallet/settings", "walletSettings"],
    ] as const) {
      const route = parseRoute(path);
      expect(route).toEqual({ name });
      expect(routeHash(route)).toBe(path);
    }
  });

  it("keeps the old #/play link as an alias for the wallet table", () => {
    expect(parseRoute("#/play")).toEqual({ name: "walletPlay" });
    expect(routeHash({ name: "walletPlay" })).toBe("#/wallet/play");
    expect(activeSection({ name: "walletPlay" })).toBe("walletPlay");
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
