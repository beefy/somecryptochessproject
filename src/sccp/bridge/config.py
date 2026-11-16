"""Per-environment Ethereum settings for the bridge.

Consensus must not depend on a third-party RPC, so none of this lives in
:class:`sccp.config.NetworkParams`. The chain id, contract address and
confirmation depth are deployment facts: an operator sets them with environment
variables, and the node exposes them as an advisory document. The only thing
consensus enforces is the SCCP HTLC itself.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from sccp.config import NetworkParams, get_network

ETH_RPC_ENV: Final = "SCCP_ETH_RPC_URL"
ETH_CONTRACT_ENV: Final = "SCCP_ETH_HTLC_CONTRACT"
ETH_CONFIRMATIONS_ENV: Final = "SCCP_ETH_CONFIRMATIONS"
SCCP_MARGIN_ENV: Final = "SCCP_ETH_SCCP_MARGIN"

ADDRESS_PATTERN: Final = re.compile(r"^0x[0-9a-fA-F]{40}$")
"""An Ethereum address. Case is display-only; the value is normalised to lower."""


@dataclass(frozen=True, slots=True)
class EthEnvironment:
    """The Ethereum chain one SCCP network bridges to."""

    network: str
    chain_id: int
    chain_name: str
    explorer_url: str | None
    rpc_url: str | None
    contract: str | None
    confirmations: int
    sccp_margin: int

    @property
    def configured(self) -> bool:
        """Whether a wallet can build an Ethereum transaction from this document."""
        return self.contract is not None

    def as_dict(self) -> dict[str, object]:
        """A JSON-friendly document for ``GET /v1/bridge``."""
        return {
            "network": self.network,
            "eth_chain_id": self.chain_id,
            "eth_chain_name": self.chain_name,
            "eth_explorer_url": self.explorer_url,
            "eth_rpc_url": self.rpc_url,
            "eth_htlc_contract": self.contract,
            "eth_confirmations": self.confirmations,
            "sccp_margin": self.sccp_margin,
            "configured": self.configured,
        }


_DEFAULTS: Final[dict[str, tuple[int, str, str | None, str | None, int, int]]] = {
    # network: (chain id, name, explorer, default rpc, confirmations, margin)
    #
    # The SCCP margin must exceed `reorg_depth` (10 on every shipped network):
    # the maker can only safely claim ETH after the taker's SCCP claim is final,
    # so a four-block margin on testnet would leave no time to act. Twenty-four
    # blocks leaves the depth-11 wait plus a buffer on both test networks.
    "devnet": (
        31337,
        "Anvil devnet",
        None,
        "http://127.0.0.1:8545",
        1,
        24,
    ),
    "testnet": (
        11155111,
        "Sepolia",
        "https://sepolia.etherscan.io",
        None,
        6,
        24,
    ),
    "mainnet": (
        1,
        "Ethereum",
        "https://etherscan.io",
        None,
        12,
        20,
    ),
}
"""The one chain each SCCP network may bridge to.

There is no fallback and no "auto" mode. A Sepolia deposit must never be credited
by a mainnet node, and a mainnet node must not be pointed at an Anvil chain by a
stray environment variable, so the chain id comes from this table and is not
overridable.
"""


def _positive_int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw, 10)
    except ValueError:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}") from None
    if value < 1:
        raise ValueError(f"{name} must be a positive integer, got {value}")
    return value


def _contract(environ: Mapping[str, str]) -> str | None:
    raw = environ.get(ETH_CONTRACT_ENV, "").strip()
    if raw == "":
        return None
    if not ADDRESS_PATTERN.fullmatch(raw):
        raise ValueError(f"{ETH_CONTRACT_ENV} must be a 0x-prefixed 20-byte address, got {raw!r}")
    return raw.lower()


def bridge_environment(
    network: str | NetworkParams,
    environ: Mapping[str, str] | None = None,
) -> EthEnvironment:
    """Resolve the Ethereum settings for ``network``.

    Only the endpoint and deployed address are deployment-specific. The chain id,
    name, confirmation depth and SCCP timelock margin are fixed per SCCP network
    so the three environments cannot be mixed by configuration.
    """
    params = get_network(network)
    try:
        chain_id, name, explorer, default_rpc, confirmations, margin = _DEFAULTS[params.network_id]
    except KeyError:
        raise ValueError(
            f"{params.network_id!r} has no Ethereum bridge environment; "
            f"known networks: {', '.join(sorted(_DEFAULTS))}"
        ) from None
    environment = os.environ if environ is None else environ
    rpc = environment.get(ETH_RPC_ENV, "").strip() or default_rpc
    resolved_margin = _positive_int(environment, SCCP_MARGIN_ENV, margin)
    if resolved_margin <= params.reorg_depth:
        raise ValueError(
            f"{SCCP_MARGIN_ENV} must exceed the SCCP reorg depth ({params.reorg_depth}); "
            f"got {resolved_margin}"
        )
    return EthEnvironment(
        network=params.network_id,
        chain_id=chain_id,
        chain_name=name,
        explorer_url=explorer,
        rpc_url=rpc,
        contract=_contract(environment),
        confirmations=_positive_int(environment, ETH_CONFIRMATIONS_ENV, confirmations),
        sccp_margin=resolved_margin,
    )


def eth_environments(environ: Mapping[str, str] | None = None) -> tuple[EthEnvironment, ...]:
    """Every SCCP network's bridge environment, in network-id order."""
    return tuple(
        bridge_environment(network, environ) for network in ("devnet", "mainnet", "testnet")
    )


__all__ = [
    "ADDRESS_PATTERN",
    "ETH_CONFIRMATIONS_ENV",
    "ETH_CONTRACT_ENV",
    "ETH_RPC_ENV",
    "SCCP_MARGIN_ENV",
    "EthEnvironment",
    "bridge_environment",
    "eth_environments",
]
