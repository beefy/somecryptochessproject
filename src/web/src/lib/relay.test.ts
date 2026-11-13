/**
 * How a relay base is presented to a person.
 *
 * A build can name the relay absolutely (`http://127.0.0.1:8646`) or relatively
 * (`/relay`, when a proxy on the page's own origin forwards it). The second form
 * is what a hosted explorer uses, and `#/play` must still be able to show a URL
 * someone can open -- including in the "cannot reach the relay" error, which is
 * the message a reader is most likely to act on.
 */

import { describe, expect, it } from "vitest";

import { relayPublicUrl } from "./relay";

const PAGE = "http://explorer.somecryptochessproject.com/";

describe("relayPublicUrl", () => {
  it("leaves an absolute base alone", () => {
    expect(relayPublicUrl("http://127.0.0.1:8646", PAGE)).toBe("http://127.0.0.1:8646");
    expect(relayPublicUrl("https://relay.example.com/base/", PAGE)).toBe(
      "https://relay.example.com/base",
    );
  });

  it("resolves the proxied path against the page", () => {
    expect(relayPublicUrl("/relay", PAGE)).toBe(`${PAGE}relay`);
    expect(relayPublicUrl("/relay/", PAGE)).toBe(`${PAGE}relay`);
    expect(relayPublicUrl("/relay", "https://explorer.example/#/play")).toBe(
      "https://explorer.example/relay",
    );
  });

  it("treats a leading slash as the origin root", () => {
    expect(relayPublicUrl("/relay", "http://example.com/explorer/")).toBe(
      "http://example.com/relay",
    );
    // Without the slash it stays inside the directory that served the page.
    expect(relayPublicUrl("relay", "http://example.com/explorer/")).toBe(
      "http://example.com/explorer/relay",
    );
  });

  it("returns the base unchanged outside a document", () => {
    // vitest runs with `environment: "node"`, so there is no window to resolve
    // against; a label must not throw for that.
    expect(relayPublicUrl("/relay")).toBe("/relay");
    expect(relayPublicUrl()).toBe("http://127.0.0.1:8646");
  });
});
