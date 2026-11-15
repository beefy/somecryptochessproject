/**
 * BIP-39 backup phrases for a 32-byte master seed.
 *
 * The phrase is an *encoding* of the seed, not a derivation path: it always holds
 * exactly 24 words (256 bits of entropy plus an 8-bit checksum), so restoring a
 * phrase restores one key on one network and nothing else. That matches
 * `sccp.crypto.wallet.seed_to_mnemonic`, and `mnemonic.test.ts` pins a phrase the
 * Python module produced so the two lists cannot drift.
 *
 * The words themselves are in `wordlist.ts`; only the bit packing lives here.
 */

import { sha256 } from "@noble/hashes/sha2.js";

import { BIP39_ENGLISH } from "./wordlist";

export const SEED_SIZE = 32;
export const WORD_COUNT = 24;
export const BITS_PER_WORD = 11;

export class MnemonicError extends Error {
  override name = "MnemonicError";
}

const WORD_INDEX: ReadonlyMap<string, number> = new Map(
  BIP39_ENGLISH.map((word, index) => [word, index]),
);

/** NFKD, lowercase and single-spaced: the form BIP-39 defines and users type. */
export function normalizePhrase(phrase: string): string[] {
  return phrase
    .normalize("NFKD")
    .toLowerCase()
    .split(/\s+/)
    .filter((word) => word.length > 0);
}

/**
 * Encode a 32-byte seed as 24 BIP-39 words.
 *
 * 264 bits (seed + the first 8 bits of its SHA-256) divide exactly into 24 × 11,
 * so each word is one 11-bit group and no padding is involved.
 */
export function seedToMnemonic(seed: Uint8Array): string {
  if (seed.length !== SEED_SIZE) {
    throw new MnemonicError(`a phrase backs up ${SEED_SIZE} bytes, got ${seed.length}`);
  }
  const bits: number[] = [];
  let buffer = 0;
  let buffered = 0;
  const feed = (byte: number): void => {
    buffer = (buffer << 8) | byte;
    buffered += 8;
    while (buffered >= BITS_PER_WORD) {
      buffered -= BITS_PER_WORD;
      bits.push((buffer >>> buffered) & 0x7ff);
      // Keep only the bits that have not been emitted yet: JavaScript's bitwise
      // operators are 32-bit, so an unbounded buffer would silently truncate.
      buffer &= (1 << buffered) - 1;
    }
  };
  seed.forEach(feed);
  feed(sha256(seed)[0] as number);
  return bits.map((index) => BIP39_ENGLISH[index] as string).join(" ");
}

/** Decode a 24-word phrase back to the seed, checking the checksum. */
export function mnemonicToSeed(phrase: string): Uint8Array {
  const words = normalizePhrase(phrase);
  if (words.length !== WORD_COUNT) {
    throw new MnemonicError(`a phrase has ${WORD_COUNT} words, got ${words.length}`);
  }
  const bits: number[] = [];
  let buffer = 0;
  let buffered = 0;
  for (const word of words) {
    const index = WORD_INDEX.get(word);
    if (index === undefined) {
      throw new MnemonicError(`"${word}" is not a BIP-39 English word`);
    }
    buffer = (buffer << BITS_PER_WORD) | index;
    buffered += BITS_PER_WORD;
    while (buffered >= 8) {
      buffered -= 8;
      bits.push((buffer >>> buffered) & 0xff);
      buffer &= (1 << buffered) - 1;
    }
  }
  if (bits.length !== SEED_SIZE + 1) {
    throw new MnemonicError(`a phrase decodes to ${bits.length - 1} bytes, expected ${SEED_SIZE}`);
  }
  const seed = Uint8Array.from(bits.slice(0, SEED_SIZE));
  if ((bits[SEED_SIZE] as number) !== sha256(seed)[0]) {
    throw new MnemonicError("the phrase checksum does not match its words");
  }
  return seed;
}
