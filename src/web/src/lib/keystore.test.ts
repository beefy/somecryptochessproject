/**
 * The browser keystore against a keyfile the Python module wrote.
 *
 * `scripts/make_web_vectors.py` pins every input that would otherwise be random
 * (seed, salt, nonce, timestamp, scrypt cost), so creating the same wallet here
 * must reproduce the CLI's document and ciphertext byte for byte. Unlocking the
 * pinned document must yield the same seed, and any edit to the authenticated
 * header must fail instead of decrypting something.
 */

import { describe, expect, it } from "vitest";

import { fromHex, toHex } from "./canonical";
import {
  KEYFILE_SCHEME,
  KeyfileError,
  SCRYPT_N,
  createKeyfile,
  keyfileText,
  parseKeyfile,
  unlockKeyfile,
} from "./keystore";
import vectors from "./vectors.json";

const pin = vectors.keyfile;
const pinned = parseKeyfile(pin.document);

describe("keyfile parsing", () => {
  it("accepts the file the CLI wrote", () => {
    expect(pinned.network).toBe("testnet");
    expect(pinned.scheme).toBe(KEYFILE_SCHEME);
    expect(pinned.kdf.salt).toBe("000102030405060708090a0b0c0d0e0f");
  });

  it("refuses a version-1 (Ed25519-only) file", () => {
    expect(() => parseKeyfile({ ...pin.document, version: 1 })).toThrow(KeyfileError);
  });

  it("refuses an unknown scheme, network or address prefix", () => {
    expect(() => parseKeyfile({ ...pin.document, scheme: "ed25519" })).toThrow(/scheme/);
    expect(() => parseKeyfile({ ...pin.document, network: "regtest" })).toThrow(/network/);
    expect(() => parseKeyfile({ ...pin.document, address: "sccp1qxyz" })).toThrow(/address/);
  });

  it("refuses a truncated ciphertext", () => {
    expect(() => parseKeyfile({ ...pin.document, ciphertext: "00ff" })).toThrow(/ciphertext/);
  });
});

describe("unlocking a CLI keyfile", () => {
  it("derives the pinned seed", async () => {
    const keypair = await unlockKeyfile(pinned, pin.passphrase);
    expect(toHex(keypair.seed)).toBe(pin.seed_hex);
  });

  it("rejects a wrong passphrase without leaking whether the file is valid", async () => {
    await expect(unlockKeyfile(pinned, "not the passphrase")).rejects.toThrow(
      /wrong passphrase or corrupted ciphertext/,
    );
  });

  it("rejects a header that was edited after encryption", async () => {
    const edited = { ...pinned, created_at: pinned.created_at + 1 };
    await expect(unlockKeyfile(edited, pin.passphrase)).rejects.toThrow(KeyfileError);
  });
});

describe("creating a keyfile", () => {
  it("reproduces the CLI's document byte for byte", async () => {
    const { document } = await createKeyfile({
      network: pinned.network,
      passphrase: pin.passphrase,
      seed: fromHex(pin.seed_hex),
      kdf: { n: pinned.kdf.n, r: pinned.kdf.r, p: pinned.kdf.p },
      createdAt: pinned.created_at,
      salt: fromHex(pinned.kdf.salt),
      nonce: fromHex(pinned.cipher.nonce),
    });
    expect(document).toEqual(pin.document);
  });

  it("writes the JSON layout `sccp wallet new` writes", () => {
    const text = keyfileText(pinned);
    expect(JSON.parse(text)).toEqual(pin.document);
    // Not merely parseable: the CLI's `json.dumps(indent=2, sort_keys=True)`
    // output, byte for byte, so a browser export diffs clean against it.
    expect(text).toBe(pin.text);
  });

  it("uses the production scrypt cost by default", () => {
    expect(SCRYPT_N).toBe(2 ** 17);
  });

  it("enforces the CLI's passphrase floor", async () => {
    await expect(
      createKeyfile({ network: "testnet", passphrase: "too short", kdf: { n: 2 ** 10 } }),
    ).rejects.toThrow(/at least 12 characters/);
  });

  it("round-trips a fresh key with cheap parameters", async () => {
    const created = await createKeyfile({
      network: "devnet",
      passphrase: "a long enough passphrase",
      kdf: { n: 2 ** 10 },
    });
    const unlocked = await unlockKeyfile(created.document, "a long enough passphrase");
    expect(toHex(unlocked.seed)).toBe(toHex(created.keypair.seed));
  });
});
