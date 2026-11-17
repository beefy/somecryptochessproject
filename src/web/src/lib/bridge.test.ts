import { describe, expect, it } from "vitest";

import {
  hashlockOf,
  orderId,
  parseEthToWei,
  formatWei,
  signBridgeOrder,
  signBridgeTake,
  sccpTimelockHeight,
  takeId,
} from "./bridge";
import { fromHex } from "./canonical";
import { LOCKED_TOPIC, encodeLock, lockIdFromReceipt, type EthReceipt } from "./eth";
import { hybridKeypairFromSeed, hybridVerify } from "./keys";

const PREIMAGE = "11".repeat(32);
const HASHLOCK = "02d449a31fbb267c8f352e9968a79e3e5fc95c1bbeaa502fd6454ebde5a4bedc";

describe("bridge order vectors", () => {
  it("matches the Python signer for a sell order", () => {
    const keypair = hybridKeypairFromSeed(new Uint8Array(32).fill(1));
    const order = signBridgeOrder(keypair, {
      network: "devnet",
      makerSccp: "sccpt1q9ejahp48496x4zw7d6qsj020kq3l9tly9tgt5el4km824nnjpzmwddqavq",
      makerEth: `0x${"12".repeat(20)}`,
      giveAmount: 10_000_000_000,
      wantAmount: "5000000000000000",
      minSccpTimelock: 50,
      expiry: 1_700_001_000,
      nonce: 0,
    });
    expect(orderId(order)).toBe(
      "268e40d0a6dc80539dfe6a243339aac46bb33ba981c0a229df0df694cb6b0bed",
    );
    expect(hybridVerify(order.public_key, fromHex(orderId(order)), order.signature)).toBe(true);
  });

  it("matches the Python signer for a take", () => {
    const keypair = hybridKeypairFromSeed(new Uint8Array(32).fill(2));
    const take = signBridgeTake(keypair, {
      network: "devnet",
      orderId: "268e40d0a6dc80539dfe6a243339aac46bb33ba981c0a229df0df694cb6b0bed",
      takerSccp: "sccpt1q8c97qs6ehcm6c6aw6pwlqjt9jlmqgdjxfsvq4vu7k6jsysfkwme79lm4gn",
      takerEth: `0x${"34".repeat(20)}`,
      hashlock: HASHLOCK,
      ethTimelock: 1_700_003_600,
      sccpTimelockHeight: 60,
      ethTxHash: `0x${"56".repeat(32)}`,
      nonce: 0,
    });
    expect(takeId(take)).toBe(
      "f720843c31e3d7d4d51955ea74547f8958e344a8d4fe39e0d30c575ffbc3fb67",
    );
    expect(hybridVerify(take.public_key, fromHex(takeId(take)), take.signature)).toBe(true);
  });
});

describe("ETH amounts", () => {
  it("hashes the shared 32-byte secret exactly", () => {
    expect(hashlockOf(PREIMAGE)).toBe(HASHLOCK);
  });

  it("parses and formats 18-decimal amounts without floating point", () => {
    expect(parseEthToWei("0.000005")).toBe(5_000_000_000_000n);
    expect(parseEthToWei("1.25")).toBe(1_250_000_000_000_000_000n);
    expect(parseEthToWei("0.000000000000000001")).toBe(1n);
    expect(formatWei(5_000_000_000_000n, 8)).toBe("0.000005");
  });

  it("keeps the SCCP timelock before the ETH timelock with a margin", () => {
    expect(sccpTimelockHeight(1_000, 7_200, 60, 4, 0)).toBe(1_116);
    expect(() => sccpTimelockHeight(1_000, 3_600, 60, 4, 2_000)).toThrow(
      "only safe up to",
    );
    expect(() => sccpTimelockHeight(1_000, 120, 60, 4, 0)).toThrow("too short");
  });
});

describe("EthHtlc calldata", () => {
  it("encodes lock arguments and reads the id back from its event", () => {
    const id = "ab".repeat(32);
    const contract = `0x${"12".repeat(20)}`;
    const data = encodeLock(HASHLOCK, 1_700_003_600, contract);
    expect(data.startsWith("0x")).toBe(true);
    expect(data.length).toBe(2 + 8 + 64 * 3);
    expect(data.slice(10, 74)).toBe(HASHLOCK);
    expect(data.slice(-40)).toBe("12".repeat(20));

    const receipt: EthReceipt = {
      transactionHash: `0x${"34".repeat(32)}`,
      blockNumber: "0x10",
      status: "0x1",
      logs: [
        {
          address: contract,
          topics: [LOCKED_TOPIC, `0x${id}`, `0x${"00".repeat(32)}`, `0x${"00".repeat(32)}`],
          data: "0x",
        },
      ],
    };
    expect(lockIdFromReceipt(receipt, contract)).toBe(id);
  });
});
