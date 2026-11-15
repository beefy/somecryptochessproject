/**
 * The two public names, and how a build decides which one it is on.
 *
 * These are pure functions over a hostname, so they are the part of the
 * cross-subdomain switch that can be tested without a browser: the explorer is
 * anything that is not `wallet.*`, and a local server switches with a hash
 * instead of sending a developer to production.
 */

import { describe, expect, it } from "vitest";

import {
  SITE_ORIGINS,
  crossSiteHref,
  isLocalDevelopment,
  siteForHostname,
  siteHash,
} from "./site";

describe("which site a hostname serves", () => {
  it("treats the wallet name, and only that name, as the wallet", () => {
    expect(siteForHostname("wallet.chessproofofwork.com")).toBe("wallet");
    expect(siteForHostname("wallet")).toBe("wallet");
    expect(siteForHostname("explorer.chessproofofwork.com")).toBe("explorer");
    expect(siteForHostname("testnet.chessproofofwork.com")).toBe("explorer");
    expect(siteForHostname("localhost")).toBe("explorer");
    expect(siteForHostname("awallet.example.com")).toBe("explorer");
  });

  it("lands each site on its own view", () => {
    expect(siteHash("wallet")).toBe("#/wallet");
    expect(siteHash("explorer")).toBe("#/");
  });
});

describe("cross-site links", () => {
  it("uses the absolute origin on a hosted build", () => {
    expect(
      crossSiteHref("wallet", { hostname: "explorer.chessproofofwork.com", local: false }),
    ).toBe(`${SITE_ORIGINS.wallet}/`);
    expect(
      crossSiteHref("explorer", { hostname: "wallet.chessproofofwork.com", local: false }),
    ).toBe(`${SITE_ORIGINS.explorer}/`);
  });

  it("stays on the current server during development", () => {
    expect(crossSiteHref("wallet", { hostname: "localhost", local: true })).toBe("#/wallet");
    expect(crossSiteHref("explorer", { hostname: "localhost", local: true })).toBe("#/");
  });

  it("recognizes the usual local hosts", () => {
    expect(isLocalDevelopment("localhost")).toBe(true);
    expect(isLocalDevelopment("127.0.0.1")).toBe(true);
    expect(isLocalDevelopment("mac.local")).toBe(true);
    expect(isLocalDevelopment("wallet.chessproofofwork.com")).toBe(false);
  });
});
