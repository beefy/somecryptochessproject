# `EthHtlc`: the Ethereum leg

One contract, deployed once per Ethereum environment. It holds ETH only between
a `lock` and its `claim` or `refund`; there is no owner, proxy, upgrade path or
admin key. The SCCP leg is an ordinary `htlc_lock` transaction on this chain, and
the two legs share the same 32-byte secret and `sha256` hashlock.

| SCCP network | Ethereum chain | Chain id | Local default RPC |
| --- | --- | --- | --- |
| `devnet` | Anvil devnet | `31337` | `http://127.0.0.1:8545` |
| `testnet` | Sepolia | `11155111` | set `SCCP_ETH_RPC_URL` |
| `mainnet` | Ethereum | `1` | set `SCCP_ETH_RPC_URL` |

The mapping is implemented in `sccp.bridge.config`; the deployment script checks
it again with `SCCP_NETWORK` before broadcasting.

## Build and test

```console
$ forge install foundry-rs/forge-std
$ forge test
$ forge script script/Deploy.s.sol:Deploy \
    --rpc-url "$SCCP_ETH_RPC_URL" \
    --private-key "$DEPLOYER_PRIVATE_KEY" \
    --broadcast
```

`SCCP_NETWORK=devnet` against Anvil, `testnet` against Sepolia and `mainnet`
against Ethereum. A mismatch reverts before a transaction is sent. Copy the
printed address into the node's environment:

```console
$ export SCCP_ETH_HTLC_CONTRACT=0x...
$ export SCCP_ETH_RPC_URL=https://...
```

`GET /v1/bridge` on the SCCP node reports the same chain id, contract and
confirmation depth the wallet should use. The browser still asks its Ethereum
provider for `eth_chainId` and refuses to sign when the two disagree.

## Interface

```solidity
function lock(bytes32 hashlock, uint64 timelock, address beneficiary)
    external payable returns (bytes32 id);
function claim(bytes32 id, bytes32 preimage) external;
function refund(bytes32 id) external;
function getLock(bytes32 id) external view returns (Lock memory);
```

`timelock` is absolute Unix seconds. `claim` is valid only before it, `refund`
only at or after it and only from the depositor. Both paths are mutually
exclusive and close the lock before transferring ETH. The preimage check is
SHA-256, not keccak, so the same 32 bytes open the SCCP HTLC.

## Trust boundary

The SCCP side is post-quantum (hybrid Ed25519 + ML-DSA-65); this contract is
secp256k1 and keccak. A swap is as strong as its weaker leg. The UI says so, and
the contract is deliberately small enough to audit.
