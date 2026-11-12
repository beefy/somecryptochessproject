"""The public testnet faucet (plan M6).

The faucet is a convenience over the deterministic genesis allocation described
in :mod:`sccp.chain.genesis`, not a consensus feature. It builds and signs a
transfer from the network's published faucet key, admits it to the node's
mempool, and applies two independent limits before doing so:

- one grant per recipient address per cooldown window (default: one hour);
- a fixed number of grants per client per window (default: five per hour).

Limits are in memory. A restart forgets them, which is acceptable for a testnet
faucet and is called out in ``docs/RUNBOOK.md``. The CLI's offline
``sccp faucet send`` remains available to an operator who already holds the
public faucet key; the HTTP endpoint is the abuse-limited path for everyone
else.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from sccp.chain.genesis import faucet_keypair
from sccp.chain.state import State
from sccp.config import NetworkParams, coins_to_units
from sccp.crypto.address import validate_address
from sccp.crypto.keys import Keypair
from sccp.errors import FaucetError, RateLimitError
from sccp.protocol.models import Transaction
from sccp.wallet.service import WalletService


def unix_now() -> int:
    """Wall-clock seconds, injectable for deterministic limit tests."""
    return int(time.time())


@dataclass(frozen=True, slots=True)
class FaucetPolicy:
    """Limits and amount of one testnet faucet grant."""

    amount_coins: int = 1000
    max_coins: int = 1000
    cooldown_seconds: int = 3600
    client_window_seconds: int = 3600
    client_grants: int = 5

    def __post_init__(self) -> None:
        if self.amount_coins < 1 or self.max_coins < 1:
            raise ValueError("faucet amounts must be positive")
        if self.amount_coins > self.max_coins:
            raise ValueError("the default faucet amount must not exceed the maximum")
        if self.cooldown_seconds < 0 or self.client_window_seconds < 1:
            raise ValueError("faucet windows must be non-negative")
        if self.client_grants < 1:
            raise ValueError("the client grant limit must be positive")


@dataclass(slots=True)
class Faucet:
    """Signs faucet transfers and enforces per-address and per-client limits.

    ``state`` returns the projection the transfer must be built against (the
    node passes its mempool projection, so two grants in one block get different
    nonces). ``admit`` gives the signed transaction to the node's mempool.
    """

    params: NetworkParams
    state: Callable[[], State]
    admit: Callable[[Transaction], str]
    policy: FaucetPolicy = field(default_factory=FaucetPolicy)
    keypair: Keypair | None = None
    clock: Callable[[], int] = unix_now
    _address_last: dict[str, int] = field(default_factory=dict)
    _client_history: dict[str, deque[int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.keypair is None:
            self.keypair = faucet_keypair(self.params.network_id)
        if self.params.network_id not in ("testnet", "devnet"):
            raise FaucetError(f"{self.params.network_id} has no public faucet; only testnets do")

    @property
    def address(self) -> str:
        """The faucet's own address."""
        assert self.keypair is not None
        return WalletService(self.params, self.state()).address_for(self.keypair)

    def _check_limits(self, to: str, *, client: str, now: int) -> None:
        if not validate_address(to, self.params):
            raise FaucetError(f"{to!r} is not a valid {self.params.network_id} address")
        last = self._address_last.get(to)
        if last is not None and now - last < self.policy.cooldown_seconds:
            wait = self.policy.cooldown_seconds - max(0, now - last)
            raise RateLimitError(
                f"{to} already received a grant; try again in {wait} seconds",
                retry_after=wait,
            )
        history = self._client_history.setdefault(client, deque())
        cutoff = now - self.policy.client_window_seconds
        while history and history[0] <= cutoff:
            history.popleft()
        if len(history) >= self.policy.client_grants:
            wait = max(1, history[0] + self.policy.client_window_seconds - now)
            raise RateLimitError(
                f"client {client} exceeded {self.policy.client_grants} grants per "
                f"{self.policy.client_window_seconds} seconds",
                retry_after=wait,
            )

    def status(self) -> dict[str, object]:
        """The public description served by ``GET /v1/faucet``."""
        return {
            "network": self.params.network_id,
            "address": self.address,
            "amount": self.policy.amount_coins,
            "max_amount": self.policy.max_coins,
            "cooldown_seconds": self.policy.cooldown_seconds,
            "client_window_seconds": self.policy.client_window_seconds,
            "client_grants": self.policy.client_grants,
        }

    def request(self, to: str, *, client: str, amount: int | None = None) -> Transaction:
        """Build and admit one faucet transfer; return the signed transaction."""
        coins = self.policy.amount_coins if amount is None else amount
        if coins < 1 or coins > self.policy.max_coins:
            raise FaucetError(f"faucet amount must be between 1 and {self.policy.max_coins} coins")
        now = self.clock()
        self._check_limits(to, client=client, now=now)
        assert self.keypair is not None
        tx = WalletService(self.params, self.state()).build_transfer(
            self.keypair,
            to,
            coins_to_units(coins),
        )
        self.admit(tx)
        self._address_last[to] = now
        self._client_history[client].append(now)
        return tx


__all__ = ["Faucet", "FaucetPolicy", "unix_now"]
