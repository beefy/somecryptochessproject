/**
 * How the relay is addressed and asked for sessions.
 *
 * A build can name the relay absolutely (`http://127.0.0.1:8646`) or relatively
 * (`/relay`, when a proxy on the page's own origin forwards it). The second form
 * is what a hosted explorer uses, and `#/play` must still be able to show a URL
 * someone can open -- including in the "cannot reach the relay" error, which is
 * the message a reader is most likely to act on.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { RELAY_BASE, relayMatches, relayPublicUrl } from "./relay";

const PAGE = "https://proofofchess.com/";

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

describe("relayMatches", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function stubRelay(): ReturnType<typeof vi.fn> {
    const fetchMock = vi.fn(
      async () =>
        new Response("[]", {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("asks for every session when no status is given", async () => {
    const fetchMock = stubRelay();
    await relayMatches();
    expect(fetchMock).toHaveBeenCalledWith(`${RELAY_BASE}/v1/matches`, {
      method: "GET",
      signal: undefined,
      headers: undefined,
      body: undefined,
    });
  });

  it("repeats status so several open states arrive in one request", async () => {
    const fetchMock = stubRelay();
    await relayMatches({ statuses: ["pending", "active"] });
    // The repeated form is what FastAPI's `list[SessionStatus]` accepts; a
    // comma-joined value would be read as one unknown status and rejected.
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      `${RELAY_BASE}/v1/matches?status=pending&status=active`,
    );
  });
});
