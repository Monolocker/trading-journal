"""Tests for the Hyperliquid read-only adapter.

No test here reaches the network. The client takes its HTTP session as a
constructor argument, so every test injects a stub that replays saved
fixture responses. The only exception is the opt-in live test at the end
of this file, which is skipped unless an environment variable is set.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from tradejournal.domain.enums import (
    CashFlowType,
    Direction,
    LiquidityRole,
    Side,
    Venue,
)
from tradejournal.exchanges.base import ReadOnlyExchangeClient
from tradejournal.exchanges.hyperliquid import (
    HyperliquidClient,
    HyperliquidRequestError,
    HyperliquidResponseError,
    redact_address,
    validate_account_address,
)
from tradejournal.exchanges.normalized import to_cash_flow, to_fill

ACCOUNT = "0x31ca8395cf837de08b24da3f660e77761dfb974b"


# ----------------------------------------------------------------------
# Test doubles
# ----------------------------------------------------------------------


class StubResponse:
    def __init__(self, status_code: int, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class StubSession:
    """Replays queued responses and records every request made."""

    def __init__(self, *responses: StubResponse) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.timeouts: list[float] = []

    def post(self, url: str, *, json: dict[str, Any], timeout: float) -> Any:
        self.requests.append(json)
        self.timeouts.append(timeout)
        if not self._responses:
            raise AssertionError("StubSession ran out of queued responses")
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


class ExplodingSession:
    """Raises a transport error on every attempt."""

    def __init__(self) -> None:
        self.attempts = 0

    def post(self, url: str, *, json: dict[str, Any], timeout: float) -> Any:
        self.attempts += 1
        raise ConnectionError(f"refused for body {json!r}")


def build_client(session: Any, **overrides: Any) -> HyperliquidClient:
    settings: dict[str, Any] = {
        "session": session,
        "max_retries": 2,
        "backoff_seconds": 0.0,
        "sleep": lambda _seconds: None,
    }
    settings.update(overrides)
    return HyperliquidClient(ACCOUNT, **settings)


@pytest.fixture
def fills_payload(load_fixture) -> list[dict[str, Any]]:
    return load_fixture("hyperliquid_fills.json")


@pytest.fixture
def funding_payload(load_fixture) -> list[dict[str, Any]]:
    return load_fixture("hyperliquid_funding.json")


@pytest.fixture
def state_payload(load_fixture) -> dict[str, Any]:
    return load_fixture("hyperliquid_clearinghouse_state.json")


# ----------------------------------------------------------------------
# Configuration and redaction
# ----------------------------------------------------------------------


def test_valid_address_is_accepted() -> None:
    assert validate_account_address(ACCOUNT) == ACCOUNT


@pytest.mark.parametrize(
    "address",
    ["", "0x123", ACCOUNT[:-1], ACCOUNT + "0", "31ca8395" * 5, None, 12345],
)
def test_invalid_address_is_rejected_at_construction(address: object) -> None:
    """A bad address otherwise looks like an account with no history."""
    with pytest.raises(ValueError):
        HyperliquidClient(address)  # type: ignore[arg-type]


def test_address_is_redacted() -> None:
    redacted = redact_address(ACCOUNT)
    assert redacted == "0x31ca...974b"
    assert ACCOUNT not in redacted


def test_repr_does_not_leak_the_full_address() -> None:
    client = build_client(StubSession(StubResponse(200, [])))
    assert ACCOUNT not in repr(client)


def test_client_satisfies_the_read_only_protocol() -> None:
    client = build_client(StubSession(StubResponse(200, [])))
    assert isinstance(client, ReadOnlyExchangeClient)
    assert client.venue is Venue.HYPERLIQUID
    assert client.supports_positions is True


def test_client_exposes_no_write_capability() -> None:
    """The read-only mandate, asserted rather than merely documented."""
    forbidden = {
        "place_order",
        "cancel_order",
        "modify_order",
        "update_leverage",
        "transfer",
        "withdraw",
        "sign",
        "exchange",
    }
    assert forbidden.isdisjoint(dir(HyperliquidClient))


# ----------------------------------------------------------------------
# Requests
# ----------------------------------------------------------------------


def test_request_body_is_shaped_as_documented(fills_payload) -> None:
    session = StubSession(StubResponse(200, fills_payload))
    build_client(session).fetch_fills(
        since=datetime(2026, 1, 15, tzinfo=UTC)
    )

    request = session.requests[0]
    assert request["type"] == "userFillsByTime"
    assert request["user"] == ACCOUNT
    assert request["startTime"] == 1768435200000


def test_every_request_carries_a_timeout(fills_payload) -> None:
    session = StubSession(StubResponse(200, fills_payload))
    build_client(session, timeout_seconds=7.5).fetch_fills()
    assert session.timeouts == [7.5]


# ----------------------------------------------------------------------
# Fill mapping
# ----------------------------------------------------------------------


def test_fills_are_mapped_and_ordered(fills_payload) -> None:
    fills = build_client(StubSession(StubResponse(200, fills_payload))).fetch_fills()

    # Three perp fills; the spot fill on '@107' is skipped.
    assert len(fills) == 3
    assert [fill.symbol for fill in fills] == [
        "BTC-PERP",
        "BTC-PERP",
        "ETH-PERP",
    ]
    assert fills == sorted(fills, key=lambda fill: fill.timestamp)


def test_buy_and_sell_sides_are_mapped(fills_payload) -> None:
    fills = build_client(StubSession(StubResponse(200, fills_payload))).fetch_fills()
    assert fills[0].side is Side.BUY
    assert fills[1].side is Side.SELL


def test_crossed_flag_becomes_liquidity_role(fills_payload) -> None:
    fills = build_client(StubSession(StubResponse(200, fills_payload))).fetch_fills()
    assert fills[0].liquidity_role is LiquidityRole.TAKER
    assert fills[1].liquidity_role is LiquidityRole.MAKER


def test_missing_crossed_flag_is_unknown_not_guessed() -> None:
    entry = {
        "coin": "BTC",
        "px": "100",
        "sz": "1",
        "side": "B",
        "time": 1768478400000,
        "fee": "0.1",
        "tid": 1,
        "feeToken": "USDC",
    }
    fills = build_client(StubSession(StubResponse(200, [entry]))).fetch_fills()
    assert fills[0].liquidity_role is LiquidityRole.UNKNOWN


def test_taker_fee_is_negated_to_a_cost(fills_payload) -> None:
    """Hyperliquid reports a paid fee as positive; we store costs negative."""
    fills = build_client(StubSession(StubResponse(200, fills_payload))).fetch_fills()
    assert fills_payload[0]["fee"] == "0.026868"
    assert fills[0].fee == Decimal("-0.026868")
    assert fills[0].fee < 0


def test_maker_rebate_becomes_positive_income(fills_payload) -> None:
    """The other half of the same rule: a negative venue fee is a rebate."""
    fills = build_client(StubSession(StubResponse(200, fills_payload))).fetch_fills()
    assert fills_payload[1]["fee"] == "-0.001439"
    assert fills[1].fee == Decimal("0.001439")
    assert fills[1].fee > 0


def test_price_and_quantity_keep_full_precision(fills_payload) -> None:
    fills = build_client(StubSession(StubResponse(200, fills_payload))).fetch_fills()
    assert fills[0].price == Decimal("93787.9606019699")
    assert fills[0].quantity == Decimal("0.0353")
    assert isinstance(fills[0].price, Decimal)


def test_identifiers_and_symbols_are_preserved(fills_payload) -> None:
    fills = build_client(StubSession(StubResponse(200, fills_payload))).fetch_fills()
    assert fills[0].venue_fill_id == "118906512037719"
    assert fills[0].venue_order_id == "90542681"
    assert fills[0].venue_symbol == "BTC"
    assert fills[0].raw_payload["dir"] == "Open Long"


def test_timestamps_are_utc(fills_payload) -> None:
    fills = build_client(StubSession(StubResponse(200, fills_payload))).fetch_fills()
    assert fills[0].timestamp == datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    assert fills[0].timestamp.tzinfo is not None


def test_unsupported_market_is_skipped_and_recorded(fills_payload) -> None:
    """A spot fill is not silently dropped; it is recorded for alerting."""
    client = build_client(StubSession(StubResponse(200, fills_payload)))
    client.fetch_fills()

    assert len(client.skipped_events) == 1
    assert client.skipped_events[0].venue_symbol == "@107"
    assert client.skipped_events[0].data_type == "fills"


def test_hip3_market_is_skipped_rather_than_confused_with_a_main_market() -> None:
    """A dex-prefixed HIP-3 asset must not normalise onto the base market."""
    entry = {
        "coin": "xyz:XYZ100",
        "px": "25372.0",
        "sz": "0.0353",
        "side": "B",
        "time": 1768478400000,
        "crossed": False,
        "fee": "0.026868",
        "tid": 164087028129848,
        "feeToken": "USDC",
    }
    client = build_client(StubSession(StubResponse(200, [entry])))
    assert client.fetch_fills() == []
    assert len(client.skipped_events) == 1


def test_mapped_fill_persists_end_to_end(repository, fills_payload) -> None:
    """The adapter, the boundary and the database agree on every type."""
    fills = build_client(StubSession(StubResponse(200, fills_payload))).fetch_fills()
    for normalized in fills:
        assert repository.insert_fill(to_fill(normalized)) is not None
    assert repository.count_fills() == 3

    stored = repository.get_fill_by_venue_id(
        Venue.HYPERLIQUID, "118906512037719"
    )
    assert stored is not None
    assert stored.fee == Decimal("-0.026868")
    assert stored.raw_payload["coin"] == "BTC"


def test_reimporting_the_same_fills_creates_no_duplicates(
    repository, fills_payload
) -> None:
    for _ in range(2):
        fills = build_client(
            StubSession(StubResponse(200, fills_payload))
        ).fetch_fills()
        for normalized in fills:
            repository.insert_fill(to_fill(normalized))
    assert repository.count_fills() == 3


# ----------------------------------------------------------------------
# Pagination
# ----------------------------------------------------------------------


def test_full_page_triggers_another_request() -> None:
    first = [
        {
            "coin": "BTC", "px": "100", "sz": "1", "side": "B",
            "time": 1768478400000, "crossed": True, "fee": "0.1",
            "tid": 1, "feeToken": "USDC",
        },
        {
            "coin": "BTC", "px": "101", "sz": "1", "side": "B",
            "time": 1768478401000, "crossed": True, "fee": "0.1",
            "tid": 2, "feeToken": "USDC",
        },
    ]
    second = [
        {
            "coin": "BTC", "px": "102", "sz": "1", "side": "B",
            "time": 1768478402000, "crossed": True, "fee": "0.1",
            "tid": 3, "feeToken": "USDC",
        }
    ]
    session = StubSession(
        StubResponse(200, first), StubResponse(200, second)
    )
    fills = build_client(session, page_limit=2).fetch_fills()

    assert len(fills) == 3
    assert len(session.requests) == 2
    assert session.requests[1]["startTime"] == 1768478401000


def test_overlapping_pages_are_deduplicated_by_trade_id() -> None:
    """An inclusive cursor re-sends boundary fills; they must not double."""
    page = [
        {
            "coin": "BTC", "px": "100", "sz": "1", "side": "B",
            "time": 1768478400000, "crossed": True, "fee": "0.1",
            "tid": 1, "feeToken": "USDC",
        },
        {
            "coin": "BTC", "px": "101", "sz": "1", "side": "B",
            "time": 1768478401000, "crossed": True, "fee": "0.1",
            "tid": 2, "feeToken": "USDC",
        },
    ]
    overlap = [
        page[1],
        {
            "coin": "BTC", "px": "102", "sz": "1", "side": "B",
            "time": 1768478402000, "crossed": True, "fee": "0.1",
            "tid": 3, "feeToken": "USDC",
        },
    ]
    session = StubSession(StubResponse(200, page), StubResponse(200, overlap))
    fills = build_client(session, page_limit=2).fetch_fills()

    assert [fill.venue_fill_id for fill in fills] == ["1", "2", "3"]


def test_short_page_ends_pagination(fills_payload) -> None:
    session = StubSession(StubResponse(200, fills_payload))
    build_client(session, page_limit=100).fetch_fills()
    assert len(session.requests) == 1


def test_empty_response_ends_pagination() -> None:
    session = StubSession(StubResponse(200, []))
    assert build_client(session).fetch_fills() == []
    assert len(session.requests) == 1


def test_stalled_cursor_raises_instead_of_looping() -> None:
    """A full page of identical fills cannot advance the cursor.

    Raising is correct: looping forever burns rate limit, and skipping
    ahead could silently lose fills.
    """
    identical = [
        {
            "coin": "BTC", "px": "100", "sz": "1", "side": "B",
            "time": 1768478400000, "crossed": True, "fee": "0.1",
            "tid": 1, "feeToken": "USDC",
        },
        {
            "coin": "BTC", "px": "100", "sz": "1", "side": "B",
            "time": 1768478400000, "crossed": True, "fee": "0.1",
            "tid": 2, "feeToken": "USDC",
        },
    ]
    session = StubSession(StubResponse(200, identical))
    with pytest.raises(HyperliquidResponseError, match="cursor"):
        build_client(session, page_limit=2).fetch_fills()


# ----------------------------------------------------------------------
# Funding
# ----------------------------------------------------------------------


def test_funding_events_are_mapped(funding_payload) -> None:
    flows = build_client(
        StubSession(StubResponse(200, funding_payload))
    ).fetch_cash_flows()

    assert len(flows) == 2
    assert all(flow.type is CashFlowType.FUNDING for flow in flows)
    assert flows[0].symbol == "BTC-PERP"
    assert flows[0].asset == "USDC"


def test_funding_sign_is_passed_through_not_flipped(funding_payload) -> None:
    """Hyperliquid already reports funding from the account's perspective.

    This is the opposite of the fee convention, which is exactly why the
    two are tested separately.
    """
    flows = build_client(
        StubSession(StubResponse(200, funding_payload))
    ).fetch_cash_flows()

    assert funding_payload[0]["delta"]["usdc"] == "-2.851187"
    assert flows[0].amount == Decimal("-2.851187")

    assert funding_payload[1]["delta"]["usdc"] == "7.267176"
    assert flows[1].amount == Decimal("7.267176")


def test_funding_rate_is_preserved(funding_payload) -> None:
    flows = build_client(
        StubSession(StubResponse(200, funding_payload))
    ).fetch_cash_flows()
    assert flows[0].funding_rate == Decimal("0.00005566")


def test_synthetic_funding_id_is_deterministic(funding_payload) -> None:
    """The venue hash is all zeros, so idempotency needs a derived id."""
    assert funding_payload[0]["hash"].strip("0x") == ""

    first = build_client(
        StubSession(StubResponse(200, funding_payload))
    ).fetch_cash_flows()
    second = build_client(
        StubSession(StubResponse(200, funding_payload))
    ).fetch_cash_flows()

    assert first[0].venue_event_id == "funding:BTC:1768478400000"
    assert first[0].venue_event_id == second[0].venue_event_id


def test_reimporting_funding_creates_no_duplicates(
    repository, funding_payload
) -> None:
    """The synthetic id and the partial unique index together give idempotency."""
    for _ in range(2):
        flows = build_client(
            StubSession(StubResponse(200, funding_payload))
        ).fetch_cash_flows()
        for normalized in flows:
            repository.insert_cash_flow(to_cash_flow(normalized))
    assert repository.count_cash_flows() == 2


def test_non_funding_delta_is_skipped_not_mistaken_for_funding() -> None:
    event = {
        "time": 1768478400000,
        "hash": "0xabc",
        "delta": {"type": "deposit", "usdc": "1000.0"},
    }
    client = build_client(StubSession(StubResponse(200, [event])))
    assert client.fetch_cash_flows() == []
    assert len(client.skipped_events) == 1


# ----------------------------------------------------------------------
# Positions
# ----------------------------------------------------------------------


def test_positions_are_mapped_with_direction_and_magnitude(
    state_payload,
) -> None:
    positions = build_client(
        StubSession(StubResponse(200, state_payload))
    ).fetch_open_positions()

    assert len(positions) == 2
    assert positions[0].symbol == "BTC-PERP"
    assert positions[0].direction is Direction.LONG
    assert positions[0].quantity == Decimal("0.0353")


def test_negative_size_becomes_a_short_with_positive_quantity(
    state_payload,
) -> None:
    positions = build_client(
        StubSession(StubResponse(200, state_payload))
    ).fetch_open_positions()

    assert positions[1].direction is Direction.SHORT
    assert positions[1].quantity == Decimal("1.25")
    assert positions[1].quantity > 0


def test_entry_price_is_preserved(state_payload) -> None:
    positions = build_client(
        StubSession(StubResponse(200, state_payload))
    ).fetch_open_positions()
    assert positions[0].entry_price == Decimal("93787.9606019699")


def test_account_with_no_positions_returns_empty() -> None:
    payload = {"assetPositions": [], "time": 1768824000000}
    positions = build_client(
        StubSession(StubResponse(200, payload))
    ).fetch_open_positions()
    assert positions == []


# ----------------------------------------------------------------------
# Retries and failure handling
# ----------------------------------------------------------------------


def test_rate_limit_is_retried_then_succeeds(fills_payload) -> None:
    session = StubSession(
        StubResponse(429), StubResponse(200, fills_payload)
    )
    fills = build_client(session).fetch_fills()
    assert len(fills) == 3
    assert len(session.requests) == 2


def test_server_error_is_retried() -> None:
    session = StubSession(StubResponse(503), StubResponse(200, []))
    build_client(session).fetch_fills()
    assert len(session.requests) == 2


def test_client_error_is_not_retried() -> None:
    """Retrying a malformed request only consumes rate limit."""
    session = StubSession(StubResponse(422))
    with pytest.raises(HyperliquidRequestError):
        build_client(session).fetch_fills()
    assert len(session.requests) == 1


def test_retries_are_bounded() -> None:
    session = ExplodingSession()
    with pytest.raises(HyperliquidRequestError):
        build_client(session, max_retries=2).fetch_fills()
    assert session.attempts == 3


def test_backoff_grows_between_attempts() -> None:
    delays: list[float] = []
    with pytest.raises(HyperliquidRequestError):
        HyperliquidClient(
            ACCOUNT,
            session=ExplodingSession(),
            max_retries=3,
            backoff_seconds=1.0,
            sleep=delays.append,
        ).fetch_fills()
    assert delays == [1.0, 2.0, 4.0]


def test_transport_error_message_is_redacted() -> None:
    """The underlying error repr contains the request body; ours must not."""
    with pytest.raises(HyperliquidRequestError) as caught:
        build_client(ExplodingSession()).fetch_fills()

    message = str(caught.value)
    assert ACCOUNT not in message
    assert "refused for body" not in message


def test_http_error_message_is_redacted() -> None:
    with pytest.raises(HyperliquidRequestError) as caught:
        build_client(StubSession(StubResponse(403))).fetch_fills()

    message = str(caught.value)
    assert ACCOUNT not in message
    assert "0x31ca...974b" in message


# ----------------------------------------------------------------------
# Malformed responses
# ----------------------------------------------------------------------


def test_non_json_response_raises_clearly() -> None:
    session = StubSession(StubResponse(200, ValueError("not json")))
    with pytest.raises(HyperliquidResponseError, match="JSON"):
        build_client(session).fetch_fills()


@pytest.mark.parametrize("payload", [{}, "text", None, 42])
def test_non_array_fills_response_is_rejected(payload: object) -> None:
    session = StubSession(StubResponse(200, payload))
    with pytest.raises(HyperliquidResponseError):
        build_client(session).fetch_fills()


def test_garbage_entries_are_skipped_without_losing_valid_ones() -> None:
    """One malformed element must not discard an entire sync."""
    payload = [
        "not an object",
        {"coin": "BTC"},
        {
            "coin": "BTC", "px": "100", "sz": "1", "side": "B",
            "time": 1768478400000, "crossed": True, "fee": "0.1",
            "tid": 7, "feeToken": "USDC",
        },
    ]
    client = build_client(StubSession(StubResponse(200, payload)))
    fills = client.fetch_fills()

    assert len(fills) == 1
    assert fills[0].venue_fill_id == "7"
    assert len(client.skipped_events) == 2


def test_unknown_side_value_is_skipped() -> None:
    payload = [
        {
            "coin": "BTC", "px": "100", "sz": "1", "side": "X",
            "time": 1768478400000, "crossed": True, "fee": "0.1",
            "tid": 1, "feeToken": "USDC",
        }
    ]
    client = build_client(StubSession(StubResponse(200, payload)))
    assert client.fetch_fills() == []
    assert len(client.skipped_events) == 1


def test_float_price_from_a_venue_is_refused() -> None:
    """A JSON number has already lost precision before it reaches us."""
    payload = [
        {
            "coin": "BTC", "px": 93787.9606019699, "sz": "1", "side": "B",
            "time": 1768478400000, "crossed": True, "fee": "0.1",
            "tid": 1, "feeToken": "USDC",
        }
    ]
    client = build_client(StubSession(StubResponse(200, payload)))
    assert client.fetch_fills() == []
    assert "float" in client.skipped_events[0].reason


def test_missing_assetPositions_raises() -> None:
    session = StubSession(StubResponse(200, {"time": 1768824000000}))
    with pytest.raises(HyperliquidResponseError, match="assetPositions"):
        build_client(session).fetch_open_positions()


def test_malformed_funding_entry_is_skipped() -> None:
    payload = [{"time": 1768478400000}, "junk"]
    client = build_client(StubSession(StubResponse(200, payload)))
    assert client.fetch_cash_flows() == []
    assert len(client.skipped_events) == 2


# ----------------------------------------------------------------------
# Optional live test: disabled by default
# ----------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("TJ_ENABLE_LIVE_TESTS") != "1",
    reason=(
        "Live test disabled. It performs real read-only requests to "
        "Hyperliquid's public info endpoint. Enable deliberately with "
        "TJ_ENABLE_LIVE_TESTS=1 and TJ_HYPERLIQUID_ACCOUNT_ADDRESS set to "
        "your PUBLIC MASTER account address. No secret is ever required, "
        "and no write endpoint is ever contacted."
    ),
)
def test_live_read_only_access() -> None:
    address = os.environ.get("TJ_HYPERLIQUID_ACCOUNT_ADDRESS")
    if not address:
        pytest.skip("TJ_HYPERLIQUID_ACCOUNT_ADDRESS is not set")

    print(
        "\nWARNING: contacting Hyperliquid's public info endpoint over the "
        "network. This is read-only and requires no credentials."
    )

    client = HyperliquidClient(address)
    positions = client.fetch_open_positions()

    assert isinstance(positions, list)
    for position in positions:
        assert position.quantity > 0
        assert position.symbol.endswith("-PERP")