import { describe, expect, it } from "vitest";

import {
  Bech32Error,
  HYBRID_ADDRESS_VERSION,
  addressHrp,
  convertBits,
  encodeProgram,
} from "./bech32";
import { fromHex } from "./canonical";
import { addressFor, addressProgram, addressHash, keypairFromSeed, publicKeyHex } from "./keys";
import vectors from "./vectors.json";

const keys = vectors.keys;

describe("bech32", () => {
  it("encodes the programs Python encodes", () => {
    const keypair = keypairFromSeed(fromHex(keys.seed_hex));
    expect(encodeProgram("sccpt", addressProgram(keypair.publicKey, "testnet"))).toBe(
      keys.address_testnet,
    );
    expect(encodeProgram("sccp", addressProgram(keypair.publicKey, "mainnet"))).toBe(
      keys.address_mainnet,
    );
  });

  it("refuses programs it cannot represent", () => {
    expect(() => encodeProgram("sccpt", new Uint8Array(20))).toThrow(Bech32Error);
    const wrongVersion = new Uint8Array(21);
    wrongVersion[0] = HYBRID_ADDRESS_VERSION;
    expect(() => encodeProgram("sccpt", wrongVersion)).toThrow(Bech32Error);
    expect(() => encodeProgram("sccpt", wrongVersion)).toThrow(/hybrid address version 1/);
    const nonsense = new Uint8Array(21);
    nonsense[0] = 7;
    expect(() => encodeProgram("sccpt", nonsense)).toThrow(/unsupported address version 7/);
  });

  it("regroups bits with BIP-173 convertbits semantics", () => {
    expect(convertBits(new Uint8Array([0xff]), 8, 5, true)).toStrictEqual([31, 28]);
    expect(convertBits([31, 28], 5, 8, false)).toStrictEqual([0xff]);
    expect(convertBits([31, 28], 5, 8, true)).toStrictEqual([0xff, 0]); // zero padding
    expect(convertBits([31, 31], 5, 8, false)).toBeNull(); // non-zero padding
    expect(convertBits([32], 5, 8, false)).toBeNull(); // value out of range
  });

  it("keeps the address HRPs of sccp/config.py", () => {
    expect(addressHrp("mainnet")).toBe("sccp");
    expect(addressHrp("testnet")).toBe("sccpt");
    expect(addressHrp("devnet")).toBe("sccpt");
    expect(addressHrp("unknown-net")).toBe("sccpt");
  });

  it("puts the network inside the address hash", () => {
    const keypair = keypairFromSeed(fromHex(keys.seed_hex));
    const hex = publicKeyHex(keypair);
    expect(addressHash("testnet", hex)).not.toStrictEqual(addressHash("mainnet", hex));
    expect(addressFor(keypair.publicKey, "testnet")).toHaveLength(keys.address_testnet.length);
  });
});
