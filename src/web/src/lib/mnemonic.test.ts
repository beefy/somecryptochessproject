/**
 * BIP-39 backup phrases against the phrase the Python module prints.
 *
 * A phrase that does not decode to the same 32 bytes is worse than no backup at
 * all, so the vector in `vectors.json` is the CLI's own output for the pinned
 * seed, and the round trip has to be exact in both directions.
 */

import { describe, expect, it } from "vitest";

import { fromHex, toHex } from "./canonical";
import { MnemonicError, mnemonicToSeed, seedToMnemonic } from "./mnemonic";
import { BIP39_ENGLISH } from "./wordlist";
import vectors from "./vectors.json";

const pin = vectors.keyfile;

describe("the wordlist", () => {
  it("has 2048 distinct words", () => {
    expect(BIP39_ENGLISH).toHaveLength(2048);
    expect(new Set(BIP39_ENGLISH).size).toBe(2048);
  });

  it("starts and ends where the standard does", () => {
    expect(BIP39_ENGLISH[0]).toBe("abandon");
    expect(BIP39_ENGLISH[2047]).toBe("zoo");
  });
});

describe("seed to phrase", () => {
  it("matches the phrase the CLI prints", () => {
    expect(seedToMnemonic(fromHex(pin.seed_hex))).toBe(pin.mnemonic);
    expect(pin.mnemonic.split(" ")).toHaveLength(24);
  });

  it("refuses a seed that is not 32 bytes", () => {
    expect(() => seedToMnemonic(new Uint8Array(16))).toThrow(/32 bytes/);
  });

  it("is deterministic", () => {
    const seed = fromHex(pin.seed_hex);
    expect(seedToMnemonic(seed)).toBe(seedToMnemonic(seed));
  });
});

describe("phrase to seed", () => {
  it("decodes back to the same seed, ignoring case and extra spaces", () => {
    const messy = `  ${pin.mnemonic.toUpperCase().replace(/ /g, "   ")}  `;
    expect(toHex(mnemonicToSeed(messy))).toBe(pin.seed_hex);
  });

  it("refuses a phrase whose checksum does not match", () => {
    const words = pin.mnemonic.split(" ");
    const replacement = words[0] === "abandon" ? "ability" : "abandon";
    expect(() => mnemonicToSeed([replacement, ...words.slice(1)].join(" "))).toThrow(
      /checksum/,
    );
  });

  it("refuses a word that is not on the list", () => {
    const words = pin.mnemonic.split(" ");
    expect(() => mnemonicToSeed(["notaword", ...words.slice(1)].join(" "))).toThrow(MnemonicError);
  });

  it("refuses the wrong number of words", () => {
    expect(() => mnemonicToSeed("abandon ability")).toThrow(/24 words/);
  });
});
