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
