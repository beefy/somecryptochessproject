"""The ETH<->ZQWX bridge.

The bridge is a pair of hash time-locked contracts, not a custodian: ZQWX lives
in :class:`sccp.chain.state.HtlcRecord` entries and ETH lives in
``contracts/EthHtlc.sol``. The SCCP node only needs an Ethereum chain id and a
contract address for the environment it is serving; it never holds an Ethereum
key and never submits a transaction.

The network mapping is deliberately explicit. A devnet node quotes the local
Anvil chain, a testnet node quotes Sepolia, and a mainnet node quotes Ethereum
mainnet. Those are different assets and a swap that mixed them would be a loss
of funds, so the mapping is tested rather than inferred from an RPC's response.
"""

from sccp.bridge.config import EthEnvironment, bridge_environment, eth_environments
from sccp.bridge.orders import (
    Order,
    OrderBook,
    OrderError,
    Take,
    order_id,
    sccp_timelock_height,
    sign_order,
    sign_take,
    take_id,
    verify_order,
    verify_take,
)

__all__ = [
    "EthEnvironment",
    "Order",
    "OrderBook",
    "OrderError",
    "Take",
    "bridge_environment",
    "eth_environments",
    "order_id",
    "sccp_timelock_height",
    "sign_order",
    "sign_take",
    "take_id",
    "verify_order",
    "verify_take",
]
