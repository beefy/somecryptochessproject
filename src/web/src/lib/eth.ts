/**
 * The small Ethereum surface the bridge needs.
 *
 * A browser wallet (MetaMask or any EIP-1193 provider) signs the Ethereum leg;
 * this module never sees a private key and never submits anything on its own.
 * It checks the provider's chain id before every action, because a Sepolia lock
 * credited by a mainnet node would be a loss of funds, and it waits for the
 * confirmation depth the SCCP node advertises before telling the user a leg is
 * final.
 */

import { keccak_256 } from "@noble/hashes/sha3.js";

import { toHex } from "./canonical";
import { BridgeError, isTxHash } from "./bridge";

interface Eip1193Provider {
  request(args: { method: string; params?: unknown[] | object }): Promise<unknown>;
}

declare global {
  interface Window {
    ethereum?: Eip1193Provider;
  }
}

export interface EthReceipt {
  transactionHash: string;
  blockNumber: string;
  status: string;
  logs: Array<{ address: string; topics: string[]; data: string }>;
}

export interface EthChain {
  chainId: number;
  chainName: string;
  rpcUrl: string | null;
  explorerUrl: string | null;
}

function provider(): Eip1193Provider {
  if (typeof window === "undefined" || window.ethereum === undefined) {
    throw new BridgeError("install an Ethereum wallet (for example MetaMask) to use the bridge");
  }
  return window.ethereum;
}

function numberFromHex(value: unknown): number {
  if (typeof value !== "string" || !/^0x[0-9a-fA-F]+$/.test(value)) {
    throw new BridgeError("the Ethereum wallet returned a malformed number");
  }
  return Number.parseInt(value, 16);
}

export async function ethChainId(): Promise<number> {
  return numberFromHex(await provider().request({ method: "eth_chainId" }));
}

export async function connectEthAccount(): Promise<string> {
  const accounts = await provider().request({ method: "eth_requestAccounts" });
  if (!Array.isArray(accounts) || typeof accounts[0] !== "string") {
    throw new BridgeError("the Ethereum wallet returned no account");
  }
  return accounts[0].toLowerCase();
}

/** Make the wallet use the environment's chain, adding it when the wallet lacks it. */
export async function ensureEthChain(expected: EthChain): Promise<void> {
  const current = await ethChainId();
  if (current === expected.chainId) return;
  const chainHex = `0x${expected.chainId.toString(16)}`;
  try {
    await provider().request({
      method: "wallet_switchEthereumChain",
      params: [{ chainId: chainHex }],
    });
  } catch (failure) {
    const error = failure as { code?: number };
    if (error.code !== 4902 || expected.rpcUrl === null) {
      throw new BridgeError(
        `switch the Ethereum wallet to ${expected.chainName} (chain ${expected.chainId})`,
      );
    }
    await provider().request({
      method: "wallet_addEthereumChain",
      params: [
        {
          chainId: chainHex,
          chainName: expected.chainName,
          rpcUrls: [expected.rpcUrl],
          blockExplorerUrls: expected.explorerUrl === null ? [] : [expected.explorerUrl],
          nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
        },
      ],
    });
  }
  const after = await ethChainId();
  if (after !== expected.chainId) {
    throw new BridgeError(`the wallet is on chain ${after}, expected ${expected.chainId}`);
  }
}

function selector(signature: string): string {
  return toHex(keccak_256(new TextEncoder().encode(signature))).slice(0, 8);
}

const LOCK = selector("lock(bytes32,uint64,address)");
const CLAIM = selector("claim(bytes32,bytes32)");
const REFUND = selector("refund(bytes32)");
export const LOCKED_TOPIC = `0x${toHex(
  keccak_256(
    new TextEncoder().encode("Locked(bytes32,address,address,bytes32,uint64,uint256)"),
  ),
)}`;

function word(value: bigint): string {
  if (value < 0n || value >= 2n ** 256n) throw new BridgeError("value does not fit one ABI word");
  return value.toString(16).padStart(64, "0");
}

function bytes32(value: string): string {
  if (!/^[0-9a-fA-F]{64}$/.test(value)) throw new BridgeError("expected a 32-byte hex value");
  return value.toLowerCase();
}

function addressWord(value: string): string {
  if (!/^0x[0-9a-fA-F]{40}$/.test(value)) throw new BridgeError("expected a 20-byte address");
  return value.slice(2).toLowerCase().padStart(64, "0");
}

/** Calldata for `lock(bytes32 hashlock, uint64 timelock, address beneficiary)`. */
export function encodeLock(hashlock: string, timelock: number, beneficiary: string): string {
  if (!Number.isSafeInteger(timelock) || timelock <= 0) {
    throw new BridgeError("the Ethereum timelock must be a positive Unix time");
  }
  return `0x${LOCK}${bytes32(hashlock)}${word(BigInt(timelock))}${addressWord(beneficiary)}`;
}

/** Calldata for `claim(bytes32 id, bytes32 preimage)`. */
export function encodeClaim(id: string, preimage: string): string {
  return `0x${CLAIM}${bytes32(id)}${bytes32(preimage)}`;
}

/** Calldata for `refund(bytes32 id)`. */
export function encodeRefund(id: string): string {
  return `0x${REFUND}${bytes32(id)}`;
}

/** The lock id a `Locked` event emitted, or `null` when the receipt has none. */
export function lockIdFromReceipt(receipt: EthReceipt, contract: string): string | null {
  const target = contract.toLowerCase();
  for (const log of receipt.logs ?? []) {
    if (
      log.address.toLowerCase() === target &&
      log.topics[0]?.toLowerCase() === LOCKED_TOPIC.toLowerCase() &&
      log.topics[1] !== undefined
    ) {
      const id = log.topics[1].replace(/^0x/, "").toLowerCase();
      if (/^[0-9a-f]{64}$/.test(id)) return id;
    }
  }
  return null;
}

interface EthTransaction {
  from: string;
  to: string | null;
  value: string;
  input: string;
}

/** Verify the ETH leg really carries the order's hashlock, timelock and value. */
export async function verifyEthLock(
  txHash: string,
  contract: string,
  expected: {
    depositor: string;
    hashlock: string;
    timelock: number;
    beneficiary: string;
    value: bigint;
    confirmations: number;
  },
): Promise<string> {
  const mined = await waitForReceipt(txHash, expected.confirmations);
  const raw = await provider().request({
    method: "eth_getTransactionByHash",
    params: [txHash],
  });
  if (raw === null || typeof raw !== "object") throw new BridgeError("the ETH lock is unknown");
  const tx = raw as EthTransaction;
  if ((tx.from ?? "").toLowerCase() !== expected.depositor.toLowerCase()) {
    throw new BridgeError("the ETH lock was not sent by the taker");
  }
  if ((tx.to ?? "").toLowerCase() !== contract.toLowerCase()) {
    throw new BridgeError("the ETH transaction did not call the bridge contract");
  }
  if (BigInt(tx.value) !== expected.value) {
    throw new BridgeError("the ETH transaction locked a different amount");
  }
  const data = tx.input.toLowerCase().replace(/^0x/, "");
  if (!data.startsWith(LOCK)) throw new BridgeError("the ETH transaction did not call lock()");
  const args = data.slice(LOCK.length);
  if (args.length < 192) throw new BridgeError("the ETH lock calldata is truncated");
  const hashlock = args.slice(0, 64);
  const timelock = Number.parseInt(args.slice(64, 128), 16);
  const beneficiary = `0x${args.slice(128 + 24, 192)}`;
  if (hashlock !== expected.hashlock || timelock !== expected.timelock) {
    throw new BridgeError("the ETH lock carries a different hashlock or timelock");
  }
  if (beneficiary !== expected.beneficiary.toLowerCase()) {
    throw new BridgeError("the ETH lock pays a different beneficiary");
  }
  const id = lockIdFromReceipt(mined, contract);
  if (id === null) throw new BridgeError("the ETH lock emitted no Locked event");
  return id;
}

async function receipt(txHash: string): Promise<EthReceipt | null> {
  const value = await provider().request({
    method: "eth_getTransactionReceipt",
    params: [txHash],
  });
  if (value === null || typeof value !== "object") return null;
  const receiptValue = value as EthReceipt;
  return typeof receiptValue.transactionHash === "string" ? receiptValue : null;
}

/** Wait for `confirmations` blocks, then return the successful receipt. */
export async function waitForReceipt(
  txHash: string,
  confirmations: number,
  timeoutMs = 10 * 60_000,
): Promise<EthReceipt> {
  if (!isTxHash(txHash)) throw new BridgeError("the transaction hash is malformed");
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const found = await receipt(txHash);
    if (found !== null) {
      const mined = numberFromHex(found.blockNumber);
      const tip = numberFromHex(await provider().request({ method: "eth_blockNumber" }));
      if (tip - mined + 1 >= Math.max(1, confirmations)) {
        if (typeof found.status !== "string" || BigInt(found.status) !== 1n) {
          throw new BridgeError("the Ethereum transaction reverted");
        }
        return found;
      }
    }
    if (Date.now() >= deadline) throw new BridgeError("timed out waiting for Ethereum confirmations");
    await new Promise((resolve) => setTimeout(resolve, 2_000));
  }
}

async function send(to: string, data: string, value: bigint): Promise<string> {
  const from = await connectEthAccount();
  const txHash = await provider().request({
    method: "eth_sendTransaction",
    params: [{ from, to, data, value: `0x${value.toString(16)}` }],
  });
  if (typeof txHash !== "string" || !isTxHash(txHash)) {
    throw new BridgeError("the Ethereum wallet did not return a transaction hash");
  }
  return txHash.toLowerCase();
}

export async function lockEth(
  contract: string,
  hashlock: string,
  timelock: number,
  beneficiary: string,
  value: bigint,
  confirmations: number,
): Promise<{ txHash: string; lockId: string | null }> {
  const txHash = await send(contract, encodeLock(hashlock, timelock, beneficiary), value);
  const mined = await waitForReceipt(txHash, confirmations);
  return { txHash, lockId: lockIdFromReceipt(mined, contract) };
}

export async function claimEth(
  contract: string,
  id: string,
  preimage: string,
  confirmations: number,
): Promise<string> {
  const txHash = await send(contract, encodeClaim(id, preimage), 0n);
  await waitForReceipt(txHash, confirmations);
  return txHash;
}

export async function refundEth(
  contract: string,
  id: string,
  confirmations: number,
): Promise<string> {
  const txHash = await send(contract, encodeRefund(id), 0n);
  await waitForReceipt(txHash, confirmations);
  return txHash;
}

/** The provider's current account, without prompting, or `null`. */
export async function currentEthAccount(): Promise<string | null> {
  try {
    const accounts = await provider().request({ method: "eth_accounts" });
    if (Array.isArray(accounts) && typeof accounts[0] === "string") {
      return accounts[0].toLowerCase();
    }
    return null;
  } catch {
    return null;
  }
}

/** Add a `0x` prefix if the caller held a bare digest. */
export function asTxHash(value: string): string {
  return value.startsWith("0x") ? value : `0x${value}`;
}

