"""The unattended miner daemon (plan M4, sections 5.7.1 and 5.11).

``EngineMiner`` is a library, not a process: it polls a relay through
:class:`RelayClient`, plays the moves it is assigned, signs them with its own
verified clock, co-signs results it can reproduce, files bonded timeout claims
only when its evidence verifies, and announces the resulting win proof to a
node. The CLI and the devnet drive the same object, which is what makes the
whole loop testable in-process over ASGI transports.

Safety rules the code enforces, not just documents:

- a match is played only when the miner's address is one of its two seats;
- a certificate is pinned per match id; a relay that changes it is refused;
- a result is signed only when the SAN list, ply count and termination can be
  replayed locally from the certificate's opening prefix;
- a clock-derived claim is signed only when ``verify_clock_evidence`` accepts
  the bundle, which requires a witness quorum;
- duplicate matches and relay restarts are idempotent: state is keyed by match
  id and nothing is signed twice.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

import chess
import httpx

from sccp.chain.state import State
from sccp.chess.engines import Engine
from sccp.config import NetworkParams, get_network
from sccp.crypto.address import encode_address
from sccp.crypto.keys import Keypair, sign_domain
from sccp.errors import ClockError, MinerError, RelayError
from sccp.miner.matchmaker import ClockBundle, MatchView
from sccp.protocol.clocks import (
    MatchClock,
    flag_result,
    sign_move,
    sign_receipt,
    verify_clock_evidence,
)
from sccp.protocol.models import (
    ClockEvidence,
    Color,
    FlagReceipt,
    MatchCertificate,
    MoveMessage,
    MoveReceipt,
    ResultValue,
    TimeoutClaim,
    Transaction,
    Transcript,
    WinProof,
)
from sccp.protocol.transcript import (
    certificate_payload,
    proof_from_transcript,
    sign_result,
)
from sccp.serialization import Domain
from sccp.wallet.service import WalletService

RETRY_ATTEMPTS = 3
"""How often a relay or node call is retried on a transport error."""

RETRY_DELAY_SECONDS = 0.5
"""Base delay between retries; doubled each attempt."""

EMPTY_SIGNATURE = "0" * 128
"""Placeholder for a :class:`SignedMove` before it is signed."""


def monotonic_ms() -> int:
    """A monotonic millisecond clock; injected in tests."""
    return int(time.monotonic() * 1000)


def _sleep(attempt: int) -> None:
    time.sleep(RETRY_DELAY_SECONDS * (2**attempt))


@dataclass(frozen=True, slots=True)
class MinerConfig:
    """Everything a miner needs that is not the engine or the transports."""

    params: NetworkParams
    keypair: Keypair
    relay_url: str = "http://127.0.0.1:8646"
    node_url: str | None = None
    nodes_per_move: int = 200_000
    depth: int | None = None
    max_games_per_epoch: int = 1
    witness_keys: tuple[tuple[str, str], ...] = ()
    """Trusted clock witnesses as ``(address, public_key_hex)`` pairs.

    The miner uses them to verify the relay's receipts before it builds a
    timeout claim. With no ``witness_keys`` the relay's attestations cannot be
    checked locally and the claim is refused rather than guessed at.
    """

    @classmethod
    def for_network(
        cls,
        network: str | NetworkParams,
        keypair: Keypair,
        *,
        relay_url: str = "http://127.0.0.1:8646",
        node_url: str | None = None,
        nodes_per_move: int = 200_000,
        depth: int | None = None,
        max_games_per_epoch: int = 1,
        witness_keys: Sequence[tuple[str, str]] = (),
    ) -> MinerConfig:
        """Build a config from a network id and a wallet keypair."""
        return cls(
            params=get_network(network),
            keypair=keypair,
            relay_url=relay_url,
            node_url=node_url,
            nodes_per_move=nodes_per_move,
            depth=depth,
            max_games_per_epoch=max_games_per_epoch,
            witness_keys=tuple(witness_keys),
        )

    @property
    def network(self) -> str:
        """The network id this miner plays on."""
        return self.params.network_id

    @property
    def address(self) -> str:
        """The miner's address (its identity towards the relay)."""
        return encode_address(self.keypair.public_key_bytes, self.params)

    @property
    def witness_key_map(self) -> dict[str, str]:
        """Trusted witness public keys, keyed by address."""
        return dict(self.witness_keys)

    @property
    def limit(self) -> chess.engine.Limit:
        """The search limit handed to the engine."""
        if self.depth is not None:
            return chess.engine.Limit(depth=self.depth)
        return chess.engine.Limit(nodes=self.nodes_per_move)


class _HttpClient:
    """Shared retrying JSON transport for the relay and node clients."""

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
        retries: int = RETRY_ATTEMPTS,
        backoff: Callable[[int], None] = _sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client if client is not None else httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self._retries = retries
        self._backoff = backoff

    def get(self, path: str) -> object:
        """GET ``path`` and return the decoded JSON body."""
        return self._request("GET", path)

    def post(self, path: str, document: Mapping[str, object]) -> object:
        """POST ``document`` to ``path`` and return the decoded JSON body."""
        return self._request("POST", path, document)

    def _request(
        self,
        method: str,
        path: str,
        document: Mapping[str, object] | None = None,
    ) -> object:
        last_error: Exception | None = None
        for attempt in range(self._retries):
            if attempt:
                self._backoff(attempt)
            try:
                response = self._client.request(method, f"{self.base_url}{path}", json=document)
            except httpx.HTTPError as exc:
                last_error = exc
                continue
            if response.status_code >= 500:
                last_error = RelayError(f"{method} {path} failed with {response.status_code}")
                continue
            if response.status_code >= 400:
                raise RelayError(f"{method} {path} -> {response.status_code}: {response.text}")
            return response.json()
        raise RelayError(f"cannot reach {self.base_url}{path}: {last_error}")

    def close(self) -> None:
        """Close the underlying client when this object created it."""
        if self._owns_client:
            self._client.close()


class RelayClient:
    """The relay API a miner needs (plan sections 5.7 and 5.12)."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8646",
        *,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
        retries: int = RETRY_ATTEMPTS,
        backoff: Callable[[int], None] = _sleep,
    ) -> None:
        self._http = _HttpClient(
            base_url,
            client=client,
            timeout=timeout,
            retries=retries,
            backoff=backoff,
        )

    def matches(self) -> list[MatchView]:
        """Every session the relay knows about."""
        document = self._http.get("/v1/matches")
        if not isinstance(document, list):
            raise RelayError("relay /v1/matches did not return a list")
        return [MatchView.model_validate(item) for item in document]

    def match(self, match_id: str) -> MatchView:
        """One session by match id."""
        return MatchView.model_validate(self._http.get(f"/v1/matches/{match_id}"))

    def join(
        self,
        match_id: str,
        *,
        seat: str,
        public_key: str,
        signature: str,
    ) -> MatchView:
        """Join a seat by presenting the signed certificate."""
        document = self._http.post(
            f"/v1/matches/{match_id}/join",
            {"seat": seat, "public_key": public_key, "signature": signature},
        )
        return MatchView.model_validate(document)

    def move(
        self,
        match_id: str,
        *,
        seat: str,
        san: str,
        network: str | None = None,
        ply: int | None = None,
        clock_ms: int | None = None,
        signature: str | None = None,
    ) -> MatchView:
        """Submit one SAN move.

        Passing ``signature`` sends the clocked body of plan section 5.7.1:
        ``{network, match_id, seat, ply, san, clock_ms, signature}``. The relay
        verifies the signature against the registered seat key and recomputes
        ``clock_ms`` from its own monotonic clock.
        """
        body: dict[str, object] = {"seat": seat, "san": san}
        if signature is not None:
            body["signature"] = signature
            body["network"] = network
            body["ply"] = ply
            body["clock_ms"] = clock_ms
        document = self._http.post(f"/v1/matches/{match_id}/move", body)
        return MatchView.model_validate(document)

    def receipts(self, match_id: str) -> ClockBundle | None:
        """The relay's clock bundle for ``match_id``, or ``None`` if unsupported.

        A relay that predates timed sessions answers 404 here; the miner then
        keeps using its own local receipts instead of failing the tick.
        """
        try:
            document = self._http.get(f"/v1/matches/{match_id}/receipts")
        except RelayError as exc:
            if "404" in str(exc):
                return None
            raise
        return ClockBundle.model_validate(document)

    def result(self, match_id: str, *, seat: str, signature: str) -> MatchView:
        """Co-sign the result."""
        document = self._http.post(
            f"/v1/matches/{match_id}/result", {"seat": seat, "signature": signature}
        )
        return MatchView.model_validate(document)

    def transcript(self, match_id: str) -> Transcript:
        """Fetch the final transcript."""
        return Transcript.model_validate(self._http.get(f"/v1/matches/{match_id}/transcript"))

    def close(self) -> None:
        """Close the transport."""
        self._http.close()


class NodeClient:
    """The subset of the node API a miner uses: tx, proof and status."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8645",
        *,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
        retries: int = RETRY_ATTEMPTS,
        backoff: Callable[[int], None] = _sleep,
    ) -> None:
        self._http = _HttpClient(
            base_url,
            client=client,
            timeout=timeout,
            retries=retries,
            backoff=backoff,
        )

    def status(self) -> Mapping[str, object]:
        """The node's status document."""
        document = self._http.get("/v1/status")
        if not isinstance(document, Mapping):
            raise RelayError("node /v1/status did not return an object")
        return document

    def submit_transaction(self, tx: Transaction) -> Mapping[str, object]:
        """Announce a signed transaction."""
        document = self._http.post("/v1/tx", tx.model_dump(mode="json", by_alias=True))
        if not isinstance(document, Mapping):
            raise RelayError("node /v1/tx did not return an object")
        return document

    def announce_proof(self, proof: WinProof) -> Mapping[str, object]:
        """Announce a win proof."""
        document = self._http.post("/v1/proofs", proof.model_dump(mode="json", by_alias=True))
        if not isinstance(document, Mapping):
            raise RelayError("node /v1/proofs did not return an object")
        return document

    def close(self) -> None:
        """Close the transport."""
        self._http.close()


@dataclass(slots=True)
class MatchState:
    """Everything the miner knows about one live match."""

    match_id: str
    seat: Color
    certificate: MatchCertificate
    board: chess.Board
    san_moves: list[str]
    clock: MatchClock | None = None
    signed_moves: list[MoveMessage] = field(default_factory=list)
    receipts: list[MoveReceipt] = field(default_factory=list)
    relay_flag: FlagReceipt | None = None
    result_signed: bool = False
    finished: bool = False
    refused: bool = False

    @property
    def opponent_seat(self) -> Color:
        """The other seat's colour."""
        return "black" if self.seat == "white" else "white"

    @property
    def opponent(self) -> str:
        """The other seat's address."""
        return self.certificate.black if self.seat == "white" else self.certificate.white

    @property
    def opening_plies(self) -> int:
        """How many SAN entries are the certified opening prefix."""
        return len(self.certificate.opening.moves)


@dataclass(frozen=True, slots=True)
class MinerTick:
    """What one polling round of the miner did."""

    observed: int = 0
    joined: tuple[str, ...] = ()
    moves: tuple[str, ...] = ()
    results: tuple[str, ...] = ()
    proofs: tuple[str, ...] = ()
    claims: tuple[str, ...] = ()
    refusals: tuple[str, ...] = ()


@dataclass(slots=True)
class ReceiptBook:
    """Witness-signed clock receipts, one per post-opening ply."""

    receipts: dict[int, MoveReceipt] = field(default_factory=dict)

    def add(self, receipt: MoveReceipt) -> None:
        """Record one receipt, rejecting a conflicting duplicate."""
        existing = self.receipts.get(receipt.ply)
        if existing is not None and existing != receipt:
            raise MinerError(f"two different receipts for ply {receipt.ply}")
        self.receipts[receipt.ply] = receipt

    def ordered(self) -> tuple[MoveReceipt, ...]:
        """Receipts in ply order."""
        return tuple(self.receipts[ply] for ply in sorted(self.receipts))


class EngineMiner:
    """A miner that plays assigned matches, signs what it can prove, and stops.

    The object is deliberately synchronous and single-threaded: one call to
    :meth:`run_once` performs at most one relay-visible action per match, so a
    caller (the CLI loop, the devnet, or a test) controls pacing and can inject
    a fake clock.
    """

    def __init__(
        self,
        config: MinerConfig,
        *,
        engine: Engine,
        relay: RelayClient | None = None,
        node: NodeClient | None = None,
        clock_ms: Callable[[], int] = monotonic_ms,
    ) -> None:
        self.config = config
        self.engine = engine
        self.relay = relay if relay is not None else RelayClient(config.relay_url)
        if node is not None:
            self.node: NodeClient | None = node
        elif config.node_url is not None:
            self.node = NodeClient(config.node_url)
        else:
            self.node = None
        self.clock_ms = clock_ms
        self.matches: dict[str, MatchState] = {}
        self.transcripts: dict[str, Transcript] = {}
        self.proofs: dict[str, WinProof] = {}
        self.claims: dict[str, TimeoutClaim] = {}
        self.books: dict[str, ReceiptBook] = {}
        self.refusals: list[str] = []

    # -- identity and wallet plans -----------------------------------------

    @property
    def address(self) -> str:
        """The miner's address on its network."""
        return self.config.address

    def plan_register(
        self,
        state: State,
        *,
        bond: int,
        endpoint: str,
        engine: str | None = None,
    ) -> Transaction:
        """A signed registration transaction for ``state``'s next nonce."""
        service = WalletService(self.config.params, state)
        return service.build_register(
            self.config.keypair,
            endpoint=endpoint,
            engine=engine or self.engine.fingerprint,
            bond=bond,
        )

    def plan_availability(
        self,
        state: State,
        *,
        nonce_secret: str,
        max_games: int | None = None,
    ) -> Transaction:
        """A signed availability commitment for the target epoch."""
        service = WalletService(self.config.params, state)
        return service.build_availability(
            self.config.keypair,
            nonce_secret=nonce_secret,
            max_games=max_games or self.config.max_games_per_epoch,
        )

    def plan_reveal(self, state: State, *, nonce_secret: str) -> Transaction:
        """A signed reveal matching the availability commitment."""
        service = WalletService(self.config.params, state)
        return service.build_reveal(self.config.keypair, nonce_secret)

    # -- receipt bookkeeping ------------------------------------------------

    def record_receipt(self, receipt: MoveReceipt) -> None:
        """Store a receipt the miner received from a witness."""
        book = self.books.setdefault(receipt.match_id, ReceiptBook())
        book.add(receipt)

    def receipts_for(self, match_id: str) -> tuple[MoveReceipt, ...]:
        """The witness receipts stored for ``match_id``."""
        book = self.books.get(match_id)
        return () if book is None else book.ordered()

    def _refuse(self, reason: str) -> None:
        if reason not in self.refusals:
            self.refusals.append(reason)

    # -- the polling loop ---------------------------------------------------

    def run_once(self) -> MinerTick:
        """Poll the relay once and act on every match this miner owns."""
        before = len(self.refusals)
        try:
            views = self.relay.matches()
        except RelayError as exc:
            self._refuse(f"relay unavailable: {exc}")
            return MinerTick(refusals=tuple(self.refusals[before:]))
        observed = 0
        joined: list[str] = []
        moves: list[str] = []
        results: list[str] = []
        proofs: list[str] = []
        claims: list[str] = []
        for view in views:
            matchup = view.matchup
            if self.address not in (matchup.white, matchup.black):
                continue  # never play a match we were not assigned to
            if matchup.network != self.config.network:
                self._refuse(f"{matchup.match_id}: match is on {matchup.network!r}")
                continue
            observed += 1
            try:
                new_joined, new_moves, new_results, new_proofs, new_claims = self._process(view)
            except (MinerError, RelayError) as exc:
                self._refuse(f"{matchup.match_id}: {exc}")
                continue
            joined += new_joined
            moves += new_moves
            results += new_results
            proofs += new_proofs
            claims += new_claims
        return MinerTick(
            observed=observed,
            joined=tuple(joined),
            moves=tuple(moves),
            results=tuple(results),
            proofs=tuple(proofs),
            claims=tuple(claims),
            refusals=tuple(self.refusals[before:]),
        )

    def _process(
        self, view: MatchView
    ) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
        matchup = view.matchup
        seat: Color = "white" if self.address == matchup.white else "black"
        state = self.matches.get(matchup.match_id)
        if state is None:
            state = self._open_match(view, seat)
        else:
            self._check_pinned(state, view)
        joined: list[str] = []
        moves = self._sync_moves(state, view)
        if view.status in ("active", "finished"):
            self._sync_receipts(state)
        if view.status == "pending" and seat not in view.certificate.start_signatures:
            self._join(state)
            joined.append(state.match_id)
            view = self.relay.match(state.match_id)
            moves += self._sync_moves(state, view)
        if state.finished or state.refused:
            return joined, moves, [], [], []
        results: list[str] = []
        proofs: list[str] = []
        claims: list[str] = []
        if view.status == "finished":
            results, proofs, claims = self._finish(view, state)
        elif view.status == "active":
            moves += self._play(state)
        return joined, moves, results, proofs, claims

    def _open_match(self, view: MatchView, seat: Color) -> MatchState:
        certificate = view.certificate
        if certificate.network != self.config.network:
            raise MinerError(
                f"certificate is on {certificate.network!r}, "
                f"this miner is on {self.config.network!r}"
            )
        if certificate.match_id != view.matchup.match_id:
            raise MinerError("certificate names a different match")
        if (certificate.white, certificate.black) != (
            view.matchup.white,
            view.matchup.black,
        ):
            raise MinerError("certificate seats disagree with the matchup")
        if self.address not in (certificate.white, certificate.black):
            raise MinerError(f"{self.address} is not a seat of this match")
        board = chess.Board()
        opening = list(certificate.opening.moves)
        for san in opening:
            try:
                board.push_san(san)
            except ValueError as exc:
                raise MinerError(f"certified opening {san!r} is illegal: {exc}") from exc
        if list(view.san_moves[: len(opening)]) != opening:
            raise MinerError("match does not start with the certified opening")
        state = MatchState(
            match_id=certificate.match_id,
            seat=seat,
            certificate=certificate,
            board=board,
            san_moves=list(opening),
        )
        self.matches[certificate.match_id] = state
        return state

    @staticmethod
    def _certificate_body(certificate: MatchCertificate) -> dict[str, object]:
        """The immutable certificate content, without join signatures."""
        return certificate.model_dump(mode="json", exclude={"start_signatures"})

    @classmethod
    def _check_pinned(cls, state: MatchState, view: MatchView) -> None:
        if cls._certificate_body(state.certificate) != cls._certificate_body(view.certificate):
            state.refused = True
            raise MinerError("relay changed the certificate of an open match")

    def _sync_moves(self, state: MatchState, view: MatchView) -> list[str]:
        opening = list(state.certificate.opening.moves)
        moves = list(view.san_moves)
        if moves[: len(opening)] != opening:
            raise MinerError("relay changed the certified opening")
        if moves[: len(state.san_moves)] != state.san_moves:
            raise MinerError("relay rewrote moves this miner already played")
        actions: list[str] = []
        for san in moves[len(state.san_moves) :]:
            self._apply_remote(state, san)
            actions.append(f"{state.match_id}:{san}")
        return actions

    def _sync_receipts(self, state: MatchState) -> None:
        """Pull the relay's signed receipts into the local receipt book.

        A relay that does not run timed sessions has no receipts endpoint and
        returns ``None``; the miner then keeps its own local records. A relay
        that changes a receipt it already published for the same ply is
        equivocating, and the conflict is fatal for the match.
        """
        bundle = self.relay.receipts(state.match_id)
        if bundle is None:
            return
        if bundle.match_id != state.match_id:
            raise MinerError("relay returned a clock bundle for another match")
        for receipt in bundle.receipts:
            if receipt.ply < state.opening_plies:
                raise MinerError(f"relay receipt covers opening ply {receipt.ply}")
            self.record_receipt(receipt)
        if bundle.flag is not None:
            state.relay_flag = bundle.flag

    def _join(self, state: MatchState) -> None:
        signature = sign_domain(
            self.config.keypair,
            Domain.CERTIFICATE,
            certificate_payload(state.certificate),
        ).hex()
        view = self.relay.join(
            state.match_id,
            seat=self.address,
            public_key=self.config.keypair.public_key_hex,
            signature=signature,
        )
        if self._certificate_body(view.certificate) == self._certificate_body(state.certificate):
            # The relay fills in join signatures; keep the pinned copy current
            # so the immutability check keeps comparing like with like.
            state.certificate = view.certificate

    # -- clock and board bookkeeping ---------------------------------------

    @staticmethod
    def _to_move(state: MatchState) -> Color:
        return "white" if state.board.turn == chess.WHITE else "black"

    def _start_clock(self, state: MatchState) -> None:
        clock = MatchClock.start(
            state.certificate.time_control,
            now_ms=self.clock_ms(),
            lag_grace_ms=self.config.params.lag_grace_ms,
        )
        clock.turn = self._to_move(state)
        state.clock = clock

    def _apply_remote(self, state: MatchState, san: str) -> None:
        """Replay one move the relay reports as played by either seat."""
        now = self.clock_ms()
        if state.clock is None:
            self._start_clock(state)
        assert state.clock is not None
        if state.clock.turn != self._to_move(state):
            raise MinerError(
                f"clock says {state.clock.turn} is to move, the board says {self._to_move(state)}"
            )
        try:
            state.clock.apply_move(now)
        except ClockError as exc:
            raise MinerError(f"relay accepted a late move: {exc}") from exc
        try:
            state.board.push_san(san)
        except ValueError as exc:
            raise MinerError(f"relay reported an illegal SAN {san!r}: {exc}") from exc
        state.san_moves.append(san)
        state.receipts.append(self._local_receipt(state, san, now))

    def _apply_local(self, state: MatchState, san: str, now: int) -> None:
        """Record a move this miner just sent successfully."""
        assert state.clock is not None
        try:
            state.clock.apply_move(now)
        except ClockError as exc:  # pragma: no cover - guarded by _play
            raise MinerError(f"own move overstepped the clock: {exc}") from exc
        state.board.push_san(san)
        state.san_moves.append(san)
        state.receipts.append(self._local_receipt(state, san, now))

    def _local_receipt(self, state: MatchState, san: str, now: int) -> MoveReceipt:
        assert state.clock is not None
        snapshot = state.clock.snapshot(now)
        return MoveReceipt(
            match_id=state.match_id,
            ply=len(state.san_moves) - 1,
            seat=self._mover_address(state),
            san=san,
            received_at_ms=now,
            white_ms=snapshot["white"],
            black_ms=snapshot["black"],
        )

    @staticmethod
    def _mover_address(state: MatchState) -> str:
        mover: Color = "white" if state.board.turn == chess.BLACK else "black"
        return state.certificate.white if mover == "white" else state.certificate.black

    # -- playing and finishing ---------------------------------------------

    def _play(self, state: MatchState) -> list[str]:
        if state.clock is None:
            self._start_clock(state)
        assert state.clock is not None
        now = self.clock_ms()
        if state.clock.flag(now) is not None:
            self._refuse(f"{state.match_id}: clock reached zero; a clock claim is required")
            return []
        if self._to_move(state) != state.seat:
            return []
        if state.board.is_game_over():
            self._refuse(f"{state.match_id}: board is over but the relay has no result")
            return []
        move = self.engine.choose_move(state.board, self.config.limit)
        if not state.board.is_legal(move):
            raise MinerError(f"engine returned the illegal move {move.uci()}")
        san = state.board.san(move)
        remaining = state.clock.current_remaining_ms(now)
        signed = sign_move(
            MoveMessage(
                network=self.config.network,
                match_id=state.match_id,
                seat=self.address,
                ply=state.board.ply(),
                san=san,
                clock_ms=remaining,
                signature=EMPTY_SIGNATURE,
            ),
            self.config.keypair,
        )
        self.relay.move(
            state.match_id,
            seat=self.address,
            san=san,
            network=self.config.network,
            ply=signed.ply,
            clock_ms=remaining,
            signature=signed.signature,
        )
        state.signed_moves.append(signed)
        self._apply_local(state, san, now)
        return [f"{state.match_id}:{san}"]

    def _finish(self, view: MatchView, state: MatchState) -> tuple[list[str], list[str], list[str]]:
        result = view.result
        if result is None:
            state.refused = True
            raise MinerError("finished match has no result")
        if result.termination == "time_forfeit":
            return self._settle_flag(view, state)
        reproducible, reason = self._reproducible(view, state)
        if not reproducible:
            state.refused = True
            raise MinerError(f"refusing to sign an unreproducible result: {reason}")
        actions: list[str] = []
        proofs: list[str] = []
        if state.seat not in result.result_signatures:
            signed = sign_result(result, state.certificate, state.seat, self.config.keypair)
            updated = self.relay.result(
                state.match_id,
                seat=self.address,
                signature=signed.result_signatures[state.seat],
            )
            state.result_signed = True
            actions.append(f"{state.match_id}:result")
            if updated.result is None or len(updated.result.result_signatures) < 2:
                # Our co-signature is on the relay, but the transcript only
                # exists once both seats signed; finish on a later poll.
                return actions, proofs, []
        transcript = self.relay.transcript(state.match_id)
        if transcript.result.game_hash != result.game_hash:
            state.refused = True
            raise MinerError("relay transcript disagrees with the result")
        self.transcripts[state.match_id] = transcript
        state.finished = True
        if self._winner_seat(result.result) == state.seat:
            proof = proof_from_transcript(transcript, params=self.config.params)
            self.proofs[proof.proof_id] = proof
            proofs.append(proof.proof_id)
            if self.node is not None:
                self.node.announce_proof(proof)
        return actions, proofs, []

    def _settle_flag(
        self, view: MatchView, state: MatchState
    ) -> tuple[list[str], list[str], list[str]]:
        """Handle a match the relay ended on time.

        A flagged game is never co-signed: the losing seat simply stops
        answering, so the settlement is a bonded ``timeout_claim`` backed by the
        relay's receipts. The winner assembles and locally verifies that claim;
        the loser records the loss and signs nothing.
        """
        winner = self._winner_seat(view.result.result if view.result else "1/2-1/2")
        if winner != state.seat:
            state.finished = True
            if state.relay_flag is None:
                self._refuse(
                    f"{state.match_id}: lost on time without a flag receipt "
                    "(no on-chain claim is possible)"
                )
            return [], [], []
        bundle = self.relay.receipts(state.match_id)
        if bundle is None or bundle.flag is None:
            state.refused = True
            raise MinerError("relay ended the match on time without a flag receipt")
        state.relay_flag = bundle.flag
        state.finished = True
        claim = self.build_timeout_claim(view, bundle=bundle)
        if claim is None:
            return [], [], []
        return [f"{state.match_id}:claim"], [], [state.match_id]

    @staticmethod
    def _winner_seat(result: ResultValue) -> Color | None:
        if result == "1-0":
            return "white"
        if result == "0-1":
            return "black"
        return None

    def _reproducible(self, view: MatchView, state: MatchState) -> tuple[bool, str]:
        """Whether this miner can rebuild the claimed result from its own board."""
        result = view.result
        certificate = state.certificate
        if result is None:
            return False, "no result"
        if result.match_id != certificate.match_id or result.network != certificate.network:
            return False, "result belongs to another match or network"
        if result.plies != len(view.san_moves):
            return False, f"result claims {result.plies} plies, the relay has {len(view.san_moves)}"
        board = state.board.copy()
        if result.termination == "checkmate":
            if not board.is_checkmate():
                return False, "the board is not checkmate"
            expected: ResultValue = "0-1" if board.turn == chess.WHITE else "1-0"
            if result.result != expected:
                return False, "the checkmate result disagrees with the board"
        elif result.termination == "draw":
            if result.result != "1/2-1/2":
                return False, "a draw must be scored 1/2-1/2"
            if not board.is_game_over():
                return False, "the board is still in progress"
        elif result.termination == "time_forfeit":
            return False, "time forfeits need verified clock evidence"
        return True, ""

    # -- timeout claims -----------------------------------------------------

    def build_timeout_claim(
        self,
        view: MatchView,
        *,
        witnesses: Sequence[tuple[str, Keypair]] = (),
        bundle: ClockBundle | None = None,
    ) -> TimeoutClaim | None:
        """Assemble a bonded claim, or refuse when the evidence is incomplete.

        ``bundle`` is the relay's clock bundle: when present the claim carries
        the relay's witness-signed receipts and flag receipt verbatim, which is
        the path a timed relay enables. Without it the miner derives the flag
        from its own clock state and signs the receipt with ``witnesses``.
        """
        state = self.matches.get(view.matchup.match_id)
        if state is None:
            self._refuse(f"{view.matchup.match_id}: no local state for a timeout claim")
            return None
        plies = len(state.san_moves)
        if plies <= state.opening_plies:
            self._refuse(f"{state.match_id}: a flag before any post-opening move is a no-show")
            return None
        if not state.signed_moves:
            self._refuse(f"{state.match_id}: no signed move to attach to the claim")
            return None
        last = state.signed_moves[-1]
        if last.ply != plies - 1:
            self._refuse(f"{state.match_id}: the last signed move is not ply {plies - 1}")
            return None
        expected = list(range(state.opening_plies, plies))
        flagged: Color
        if bundle is not None:
            if bundle.match_id != state.match_id:
                self._refuse(f"{state.match_id}: the relay bundle is for another match")
                return None
            final = bundle.flag
            if final is None:
                self._refuse(f"{state.match_id}: the relay has no flag receipt")
                return None
            receipts = bundle.receipts
            flagged = "white" if final.seat == state.certificate.white else "black"
        else:
            if state.clock is None:
                self._refuse(f"{state.match_id}: no clock has started")
                return None
            now = self.clock_ms()
            flagged_now = state.clock.flag(now)
            if flagged_now is None:
                return None
            if flagged_now == state.seat:
                self._refuse(f"{state.match_id}: our own clock is the one that ran out")
                return None
            flagged = flagged_now
            book = self.books.get(state.match_id)
            receipts = () if book is None else book.ordered()
            snapshot = state.clock.snapshot(now)
            final = FlagReceipt(
                match_id=state.match_id,
                ply=plies,
                seat=state.opponent,
                received_at_ms=now,
                white_ms=0 if flagged == "white" else snapshot["white"],
                black_ms=0 if flagged == "black" else snapshot["black"],
            )
            for _, keypair in witnesses:
                signed = sign_receipt(final, keypair, network=self.config.network)
                if not isinstance(signed, FlagReceipt):  # pragma: no cover - inputs are flags
                    raise MinerError("sign_receipt returned the wrong receipt kind")
                final = signed
        if [receipt.ply for receipt in receipts] != expected:
            self._refuse(f"{state.match_id}: witness receipts do not cover every post-opening move")
            return None
        evidence = ClockEvidence(
            time_control=state.certificate.time_control,
            final=final,
            receipts=tuple(receipts),
        )
        result = flag_result(state.board, flagged=flagged)
        if result == "1/2-1/2":
            self._refuse(f"{state.match_id}: a flag with insufficient material is a draw")
            return None
        if self._winner_seat(result) != state.seat:
            self._refuse(f"{state.match_id}: the flag does not win the game for us")
            return None
        claim = TimeoutClaim(
            network=self.config.network,
            match_id=state.match_id,
            certificate=state.certificate,
            last_move=last,
            clock_evidence=evidence,
            bond=max(self.config.params.min_timeout_bond, 1),
        )
        public_keys = self.config.witness_key_map
        public_keys.update({address: keypair.public_key_hex for address, keypair in witnesses})
        try:
            verify_clock_evidence(
                evidence,
                certificate=state.certificate,
                san_moves=view.san_moves,
                result=result,
                winner_seat=state.seat,
                public_keys=public_keys,
                quorum=self.config.params.clock_witness_quorum,
                max_plies=self.config.params.max_plies,
                lag_grace_ms=self.config.params.lag_grace_ms,
            )
        except ClockError as exc:
            self._refuse(f"{state.match_id}: clock evidence does not verify: {exc}")
            return None
        self.claims[state.match_id] = claim
        return claim

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """Close the engine and both transports."""
        self.engine.close()
        self.relay.close()
        if self.node is not None:
            self.node.close()


__all__ = [
    "EMPTY_SIGNATURE",
    "RETRY_ATTEMPTS",
    "RETRY_DELAY_SECONDS",
    "EngineMiner",
    "MatchState",
    "MinerConfig",
    "MinerTick",
    "NodeClient",
    "ReceiptBook",
    "RelayClient",
    "monotonic_ms",
]
