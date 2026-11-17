/**
 * The five public names, and how a build decides which one it is on.
 *
 * These are pure functions over a hostname, so they are the part of the
 * cross-site switch that can be tested without a browser: explorer is anything
 * that is not one of the named subdomains, and a local server switches with a
 * hash instead of sending a developer to production.
 */

import { describe, expect, it } from "vitest";

import {
  SITE_ORIGINS,
  crossSiteHref,
  hashUrl,
  isLocalDevelopment,
  siteForHostname,
  siteHash,
} from "./site";

describe("which site a hostname serves", () => {
  it("treats each named subdomain as its own site", () => {
    expect(siteForHostname("wallet.chessproofofwork.com")).toBe("wallet");
    expect(siteForHostname("wallet")).toBe("wallet");
    expect(siteForHostname("play.chessproofofwork.com")).toBe("play");
    expect(siteForHostname("dex.chessproofofwork.com")).toBe("dex");
    expect(siteForHostname("bridge.chessproofofwork.com")).toBe("bridge");
    expect(siteForHostname("explorer.chessproofofwork.com")).toBe("explorer");
    expect(siteForHostname("testnet.chessproofofwork.com")).toBe("explorer");
    expect(siteForHostname("localhost")).toBe("explorer");
    expect(siteForHostname("awallet.example.com")).toBe("explorer");
  });

  it("lands each site on its own view", () => {
    expect(siteHash("wallet")).toBe("#/wallet");
    expect(siteHash("play")).toBe("#/play");
    expect(siteHash("dex")).toBe("#/dex");
    expect(siteHash("bridge")).toBe("#/bridge");
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
    expect(
      crossSiteHref("dex", { hostname: "wallet.chessproofofwork.com", local: false }),
    ).toBe(`${SITE_ORIGINS.dex}/`);
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

  it("joins an origin and a hash without a double slash", () => {
    expect(hashUrl("https://wallet.chessproofofwork.com/", "#/wallet")).toBe(
      "https://wallet.chessproofofwork.com/#/wallet",
    );
    expect(hashUrl("https://wallet.chessproofofwork.com", "#/wallet")).toBe(
      "https://wallet.chessproofofwork.com/#/wallet",
    );
    expect(hashUrl("https://wallet.chessproofofwork.com/", "#/")).toBe(
      "https://wallet.chessproofofwork.com/#/",
    );
  });
});
