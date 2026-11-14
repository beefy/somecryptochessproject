import { describe, expect, it } from "vitest";

import {
  ADDRESS_VERSION,
  Bech32Error,
  PROGRAM_SIZE,
  addressHrp,
  convertBits,
  encodeProgram,
} from "./bech32";
import { fromHex } from "./canonical";
import { addressFor, addressHash, addressProgram } from "./keys";
import vectors from "./vectors.json";

const keys = vectors.keys;
const hybridPublicKey = fromHex(keys.hybrid_public_key_hex);

describe("bech32", () => {
  it("encodes the programs Python encodes", () => {
    expect(encodeProgram("sccpt", addressProgram(hybridPublicKey, "testnet"))).toBe(
      keys.address_testnet,
    );
    expect(encodeProgram("sccp", addressProgram(hybridPublicKey, "mainnet"))).toBe(
      keys.address_mainnet,
    );
  });

  it("refuses programs it cannot represent", () => {
    expect(() => encodeProgram("sccpt", new Uint8Array(20))).toThrow(Bech32Error);
    expect(() => encodeProgram("sccpt", new Uint8Array(21))).toThrow(/33 bytes/);
    const wrongVersion = new Uint8Array(PROGRAM_SIZE);
    wrongVersion[0] = 0;
    expect(() => encodeProgram("sccpt", wrongVersion)).toThrow(/unsupported address version 0/);
    const nonsense = new Uint8Array(PROGRAM_SIZE);
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

  it("puts the network and the algorithm inside the address hash", () => {
    const hex = keys.hybrid_public_key_hex;
    expect(addressHash("testnet", hex)).not.toStrictEqual(addressHash("mainnet", hex));
    expect(addressHash("testnet", hex)).toHaveLength(32);
    expect(addressFor(hybridPublicKey, "testnet")).toHaveLength(keys.address_testnet.length);
  });

  it("uses version 1 over the full digest", () => {
    const program = addressProgram(hybridPublicKey, "testnet");
    expect(ADDRESS_VERSION).toBe(1);
    expect(program).toHaveLength(PROGRAM_SIZE);
    expect(program[0]).toBe(1);
  });
});

