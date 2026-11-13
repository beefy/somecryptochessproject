import { describe, expect, it } from "vitest";

import {
  CanonicalJsonError,
  canonicalJson,
  compareCodePoints,
  domainHex,
  fromHex,
  normalize,
  toHex,
} from "./canonical";
import vectors from "./vectors.json";

const sample = vectors.canonical;

describe("canonicalJson", () => {
  it("matches the Python encoder byte for byte", () => {
    expect(canonicalJson(sample.payload)).toBe(sample.json);
    expect(new TextDecoder().decode(new TextEncoder().encode(sample.json)).length).toBeGreaterThan(0);
  });

  it("matches the Python domain hash", () => {
    expect(domainHex("sccp/v1/game", sample.payload)).toBe(sample.game_digest);
  });

  it("sorts keys by code point, not UTF-16 code unit", () => {
    // U+E000 sorts before U+1F600 by code point; sorting by UTF-16 units would
    // put the surrogate pair first because 0xD83D < 0xE000.
    const json = canonicalJson({ "\u{1f600}": 1, "\ue000": 2 });
    expect(json).toBe('{"\ue000":2,"\u{1f600}":1}');
    expect(compareCodePoints("\ue000", "\u{1f600}")).toBeLessThan(0);
    expect("\ue000" < "\u{1f600}").toBe(false); // the trap being avoided
  });

  it("NFC-normalizes keys and values", () => {
    expect(canonicalJson({ "cafe\u0301": "e\u0301" })).toBe('{"café":"é"}');
    expect(normalize({ "cafe\u0301": 1 })).toStrictEqual({ café: 1 });
  });

  it("keeps the consensus integer range and accepts exact bigints", () => {
    expect(canonicalJson({ big: 9_007_199_254_740_991 })).toBe('{"big":9007199254740991}');
    expect(canonicalJson({ big: 18_446_744_073_709_551_615n, neg: -9_223_372_036_854_775_808n })).toBe(
      vectors.bigint.json,
    );
    expect(domainHex("sccp/v1/game", { big: 18_446_744_073_709_551_615n, neg: -9_223_372_036_854_775_808n })).toBe(
      vectors.bigint.game_digest,
    );
    expect(() => normalize(1.5)).toThrow(CanonicalJsonError);
    expect(() => normalize({ a: undefined })).toThrow(CanonicalJsonError);
    expect(() => normalize({ a: 2n ** 64n })).toThrow(CanonicalJsonError);
    expect(() => normalize(2 ** 60)).toThrow(CanonicalJsonError); // unsafe double
    expect(() => normalize(new Uint8Array([1]))).toThrow(CanonicalJsonError);
  });

  it("refuses unknown domains", () => {
    expect(() => domainHex("sccp/v1/nope", {})).toThrow(CanonicalJsonError);
  });

  it("round-trips hex", () => {
    expect(toHex(fromHex("00ff10"))).toBe("00ff10");
    expect(() => fromHex("abc")).toThrow(CanonicalJsonError);
  });
});
