"""Tests for the normalization boundary.

Covers symbol normalization, the parsing rules applied to untrusted API
values, and conversion from normalized events into domain models.

No test here contacts a live API.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tradejournal.domain.enums import (
    CashFlowType,
    Direction,
    LiquidityRole,
    Side,
    SyncDataType,
    Venue,
)
from tradejournal.exchanges.base import ReadOnlyExchangeClient
from tradejournal.exchanges.normalized import (
    EventParsingError,
    NormalizedCashFlow,
    NormalizedFill,
    NormalizedPosition,
    SyncCursor,
    parse_decimal,
    parse_direction_from_signed_size,
    parse_epoch_ms,
    parse_iso8601,
    parse_optional_decimal,
    require_field,
    require_mapping,
    to_cash_flow,
    to_fill,
)
from tradejournal.exchanges.symbols import (
    SymbolNormalizationError,
    base_asset,
    is_canonical,
    market_namespace,
    normalize_hyperliquid_symbol,
    normalize_variational_symbol,
    split_canonical,
    to_canonical,
)

NOW = datetime(2026, 1, 20, 12, 0, 0, tzinfo=UTC)


# ----------------------------------------------------------------------
# Symbol normalization
# ----------------------------------------------------------------------


def test_hyperliquid_symbols_from_fixture(load_fixture) -> None:
    for case in load_fixture("symbols.json")["hyperliquid_valid"]:
        assert (
            normalize_hyperliquid_symbol(case["venue_symbol"])
            == case["canonical"]
        )


def test_hyperliquid_rejections_from_fixture(load_fixture) -> None:
    for case in load_fixture("symbols.json")["hyperliquid_rejected"]:
        with pytest.raises(SymbolNormalizationError):
            normalize_hyperliquid_symbol(case["venue_symbol"])


def test_variational_symbols_from_fixture(load_fixture) -> None:
    for case in load_fixture("symbols.json")["variational_valid"]:
        assert (
            normalize_variational_symbol(case["venue_symbol"])
            == case["canonical"]
        )


def test_variational_rejections_from_fixture(load_fixture) -> None:
    for case in load_fixture("symbols.json")["variational_rejected"]:
        with pytest.raises(SymbolNormalizationError):
            normalize_variational_symbol(case["venue_symbol"])


def test_multiplier_prefix_is_preserved() -> None:
    """kPEPE is a 1000x contract and must not collapse onto plain PEPE.

    If it did, quantity reconciliation would compare two legs whose units
    differ by a factor of 1000 while believing them to be the same market.
    """
    assert normalize_hyperliquid_symbol("kPEPE") == "KPEPE-PERP"
    assert normalize_hyperliquid_symbol("PEPE") == "PEPE-PERP"
    assert normalize_hyperliquid_symbol("kPEPE") != normalize_hyperliquid_symbol(
        "PEPE"
    )


def test_the_same_market_normalizes_identically_across_venues() -> None:
    """The whole purpose of the boundary, stated as a test."""
    assert normalize_hyperliquid_symbol("BTC") == normalize_variational_symbol(
        "BTC-USD"
    )


@pytest.mark.parametrize("value", [None, 123, 4.5, [], {}, True])
def test_non_string_symbol_is_rejected(value: object) -> None:
    with pytest.raises(SymbolNormalizationError):
        normalize_hyperliquid_symbol(value)


def test_symbol_error_carries_the_raw_value() -> None:
    with pytest.raises(SymbolNormalizationError) as caught:
        normalize_hyperliquid_symbol("PURR/USDC")
    assert caught.value.venue_symbol == "PURR/USDC"


def test_canonical_form_helpers() -> None:
    assert to_canonical("btc") == "BTC-PERP"
    assert is_canonical("BTC-PERP")
    assert not is_canonical("BTC")
    assert not is_canonical("btc-perp")


def test_surrounding_whitespace_is_tolerated() -> None:
    assert normalize_hyperliquid_symbol("  BTC  ") == "BTC-PERP"


# ----------------------------------------------------------------------
# HIP-3 builder-deployed markets
# ----------------------------------------------------------------------


def test_hip3_symbols_from_fixture(load_fixture) -> None:
    for case in load_fixture("symbols.json")["hyperliquid_hip3_valid"]:
        assert (
            normalize_hyperliquid_symbol(case["venue_symbol"])
            == case["canonical"]
        )


def test_hip3_market_does_not_collide_with_primary_dex() -> None:
    """xyz:BTC and BTC are different markets and must stay different.

    Separate order book, separate deployer-run oracle, separate funding.
    Collapsing them would make leg reconstruction merge fills from two
    unrelated positions into one leg.
    """
    assert normalize_hyperliquid_symbol("xyz:BTC") == "XYZ:BTC-PERP"
    assert normalize_hyperliquid_symbol("BTC") == "BTC-PERP"
    assert normalize_hyperliquid_symbol("xyz:BTC") != normalize_hyperliquid_symbol("BTC")


def test_two_dexes_listing_the_same_asset_stay_distinct() -> None:
    assert normalize_hyperliquid_symbol("xyz:AAPL") != normalize_hyperliquid_symbol(
        "abc:AAPL"
    )


def test_equity_ticker_with_a_dot_is_accepted() -> None:
    """Deployers choose HIP-3 asset names; equities are not all plain letters."""
    assert normalize_hyperliquid_symbol("xyz:BRK.B") == "XYZ:BRK.B-PERP"


def test_namespace_case_is_normalised() -> None:
    assert normalize_hyperliquid_symbol("xyz:AAPL") == normalize_hyperliquid_symbol(
        "XYZ:AAPL"
    )


def test_base_asset_ignores_the_namespace() -> None:
    """This is what lets a HIP-3 leg pair with a plain leg on another venue."""
    assert base_asset("XYZ:AAPL-PERP") == "AAPL"
    assert base_asset("AAPL-PERP") == "AAPL"
    assert base_asset("XYZ:AAPL-PERP") == base_asset("AAPL-PERP")


def test_market_namespace_is_reported() -> None:
    assert market_namespace("XYZ:AAPL-PERP") == "XYZ"
    assert market_namespace("BTC-PERP") is None


def test_split_canonical_rejects_a_raw_venue_symbol() -> None:
    """Guards against passing an unnormalised name into pairing logic."""
    with pytest.raises(SymbolNormalizationError):
        split_canonical("xyz:AAPL")


def test_namespaced_canonical_form_is_recognised() -> None:
    assert is_canonical("XYZ:AAPL-PERP")
    assert not is_canonical("xyz:AAPL-PERP")
    assert not is_canonical("XYZ:AAPL")


def test_to_canonical_accepts_an_explicit_namespace() -> None:
    assert to_canonical("aapl", namespace="xyz") == "XYZ:AAPL-PERP"


def test_variational_rejects_a_namespace_rather_than_guessing() -> None:
    """No evidence exists that this venue uses namespaces; do not invent one."""
    with pytest.raises(SymbolNormalizationError):
        normalize_variational_symbol("xyz:AAPL")


# ----------------------------------------------------------------------
# Decimal parsing
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", Decimal("0")),
        ("93787.9606019699", Decimal("93787.9606019699")),
        ("-2.851187", Decimal("-2.851187")),
        ("0.00000001", Decimal("0.00000001")),
        (42, Decimal("42")),
        ("1e-8", Decimal("0.00000001")),
    ],
)
def test_decimal_parsing_accepts_valid_values(
    value: object, expected: Decimal
) -> None:
    assert parse_decimal(value) == expected


def test_float_is_rejected_with_a_precision_explanation() -> None:
    """A float has already lost precision before it reaches us."""
    with pytest.raises(EventParsingError, match="float"):
        parse_decimal(93787.9606019699)


def test_boolean_is_rejected_despite_being_an_int_subclass() -> None:
    with pytest.raises(EventParsingError):
        parse_decimal(True)


@pytest.mark.parametrize("value", [None, [], {}, "abc", "", "NaN", "Infinity"])
def test_invalid_decimal_values_are_rejected(value: object) -> None:
    with pytest.raises(EventParsingError):
        parse_decimal(value)


def test_optional_decimal_allows_none_but_still_validates() -> None:
    assert parse_optional_decimal(None) is None
    assert parse_optional_decimal("1.5") == Decimal("1.5")
    with pytest.raises(EventParsingError):
        parse_optional_decimal(1.5)


def test_error_message_names_the_field() -> None:
    with pytest.raises(EventParsingError, match="funding_rate"):
        parse_decimal("oops", field_name="funding_rate")


# ----------------------------------------------------------------------
# Timestamp parsing
# ----------------------------------------------------------------------


def test_epoch_milliseconds_round_trip() -> None:
    assert parse_epoch_ms(1768478400000, now=NOW) == datetime(
        2026, 1, 15, 12, 0, 0, tzinfo=UTC
    )


def test_seconds_mistaken_for_milliseconds_is_caught() -> None:
    """The single most common unit bug in exchange integrations.

    1768478400 is a valid seconds timestamp. Read as milliseconds it lands
    in January 1970, which the lower bound rejects.
    """
    with pytest.raises(EventParsingError, match="early"):
        parse_epoch_ms(1768478400, now=NOW)


def test_far_future_timestamp_is_caught() -> None:
    far_future = int(
        (NOW + timedelta(days=400) - datetime(1970, 1, 1, tzinfo=UTC))
        / timedelta(milliseconds=1)
    )
    with pytest.raises(EventParsingError, match="future"):
        parse_epoch_ms(far_future, now=NOW)


def test_small_clock_skew_is_tolerated() -> None:
    """A venue slightly ahead of us is normal and must not fail."""
    slightly_ahead = int(
        (NOW + timedelta(hours=1) - datetime(1970, 1, 1, tzinfo=UTC))
        / timedelta(milliseconds=1)
    )
    assert parse_epoch_ms(slightly_ahead, now=NOW) > NOW


@pytest.mark.parametrize("value", [None, "1768478400000", 1.5, True, []])
def test_non_integer_epoch_values_are_rejected(value: object) -> None:
    with pytest.raises(EventParsingError):
        parse_epoch_ms(value, now=NOW)


def test_iso8601_is_converted_to_utc() -> None:
    assert parse_iso8601("2026-01-15T12:00:00Z", now=NOW) == datetime(
        2026, 1, 15, 12, 0, 0, tzinfo=UTC
    )


def test_iso8601_offset_is_applied_not_discarded() -> None:
    stockholm = timezone(timedelta(hours=1))
    expected = datetime(2026, 1, 15, 13, 0, 0, tzinfo=stockholm)
    assert parse_iso8601("2026-01-15T13:00:00+01:00", now=NOW) == expected
    assert parse_iso8601("2026-01-15T13:00:00+01:00", now=NOW) == datetime(
        2026, 1, 15, 12, 0, 0, tzinfo=UTC
    )


def test_iso8601_without_timezone_is_rejected() -> None:
    """Assuming UTC is how an event ends up hours from where it belongs."""
    with pytest.raises(EventParsingError, match="no timezone"):
        parse_iso8601("2026-01-15T12:00:00", now=NOW)


@pytest.mark.parametrize("value", [None, 123, "not-a-date", ""])
def test_invalid_iso8601_values_are_rejected(value: object) -> None:
    with pytest.raises(EventParsingError):
        parse_iso8601(value, now=NOW)


# ----------------------------------------------------------------------
# Signed size, and malformed response guards
# ----------------------------------------------------------------------


def test_positive_size_is_a_long() -> None:
    assert parse_direction_from_signed_size("0.0353") == (
        Direction.LONG,
        Decimal("0.0353"),
    )


def test_negative_size_is_a_short_with_positive_quantity() -> None:
    direction, quantity = parse_direction_from_signed_size("-0.0353")
    assert direction is Direction.SHORT
    assert quantity == Decimal("0.0353")
    assert quantity > 0


def test_zero_size_has_no_direction() -> None:
    with pytest.raises(EventParsingError):
        parse_direction_from_signed_size("0")


@pytest.mark.parametrize("value", ["a string", None, 123, []])
def test_non_object_payload_element_is_rejected(value: object) -> None:
    """A list of objects that contains something else must fail clearly."""
    with pytest.raises(EventParsingError):
        require_mapping(value)


def test_missing_required_field_names_the_key() -> None:
    with pytest.raises(EventParsingError, match="'px'"):
        require_field({"coin": "BTC"}, "px")


def test_present_field_is_returned() -> None:
    assert require_field({"px": "100"}, "px") == "100"


# ----------------------------------------------------------------------
# Normalized events and conversion into domain models
# ----------------------------------------------------------------------


@pytest.fixture
def normalized_fill() -> NormalizedFill:
    return NormalizedFill(
        venue=Venue.HYPERLIQUID,
        venue_fill_id="118906512037719",
        venue_order_id="90542681",
        venue_symbol="BTC",
        symbol="BTC-PERP",
        timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        side=Side.BUY,
        price=Decimal("93787.9606019699"),
        quantity=Decimal("0.0353"),
        fee=Decimal("-0.026868"),
        fee_asset="USDC",
        liquidity_role=LiquidityRole.TAKER,
        raw_payload={"coin": "BTC", "px": "93787.9606019699"},
    )


@pytest.fixture
def normalized_cash_flow() -> NormalizedCashFlow:
    return NormalizedCashFlow(
        venue=Venue.HYPERLIQUID,
        venue_event_id="funding:BTC:1768478400000",
        venue_symbol="BTC",
        symbol="BTC-PERP",
        timestamp=datetime(2026, 1, 15, 16, 0, 0, tzinfo=UTC),
        type=CashFlowType.FUNDING,
        amount=Decimal("-2.851187"),
        asset="USDC",
        funding_rate=Decimal("0.00005566"),
        raw_payload={"coin": "BTC", "usdc": "-2.851187"},
    )


def test_normalized_events_are_immutable(
    normalized_fill: NormalizedFill,
) -> None:
    with pytest.raises(Exception):
        normalized_fill.price = Decimal("1")  # type: ignore[misc]


def test_fill_conversion_preserves_every_value(
    normalized_fill: NormalizedFill,
) -> None:
    fill = to_fill(normalized_fill)

    assert fill.venue is normalized_fill.venue
    assert fill.venue_fill_id == normalized_fill.venue_fill_id
    assert fill.venue_order_id == normalized_fill.venue_order_id
    assert fill.venue_symbol == normalized_fill.venue_symbol
    assert fill.symbol == normalized_fill.symbol
    assert fill.timestamp == normalized_fill.timestamp
    assert fill.side is normalized_fill.side
    assert fill.price == normalized_fill.price
    assert fill.quantity == normalized_fill.quantity
    assert fill.fee == normalized_fill.fee
    assert fill.fee_asset == normalized_fill.fee_asset
    assert fill.liquidity_role is normalized_fill.liquidity_role
    assert fill.raw_payload == normalized_fill.raw_payload


def test_converted_fill_is_unassigned_by_default(
    normalized_fill: NormalizedFill,
) -> None:
    """An adapter cannot know the leg, so conversion must not invent one."""
    assert to_fill(normalized_fill).leg_id is None


def test_converted_fill_records_ingestion_time(
    normalized_fill: NormalizedFill,
) -> None:
    fill = to_fill(normalized_fill)
    assert fill.ingested_at is not None
    assert fill.ingested_at.tzinfo is not None
    assert fill.ingested_at != fill.timestamp


def test_cash_flow_conversion_preserves_every_value(
    normalized_cash_flow: NormalizedCashFlow,
) -> None:
    cash_flow = to_cash_flow(normalized_cash_flow)

    assert cash_flow.venue is normalized_cash_flow.venue
    assert cash_flow.venue_event_id == normalized_cash_flow.venue_event_id
    assert cash_flow.symbol == normalized_cash_flow.symbol
    assert cash_flow.timestamp == normalized_cash_flow.timestamp
    assert cash_flow.type is normalized_cash_flow.type
    assert cash_flow.amount == normalized_cash_flow.amount
    assert cash_flow.funding_rate == normalized_cash_flow.funding_rate
    assert cash_flow.trade_id is None
    assert cash_flow.leg_id is None


def test_converted_fill_persists_and_round_trips(
    repository, normalized_fill: NormalizedFill
) -> None:
    """The boundary and the database agree on every type."""
    assert repository.insert_fill(to_fill(normalized_fill)) is not None

    stored = repository.get_fill_by_venue_id(
        Venue.HYPERLIQUID, normalized_fill.venue_fill_id
    )
    assert stored is not None
    assert stored.price == normalized_fill.price
    assert stored.symbol == "BTC-PERP"
    assert stored.venue_symbol == "BTC"


def test_converted_cash_flow_persists(
    repository, normalized_cash_flow: NormalizedCashFlow
) -> None:
    assert repository.insert_cash_flow(to_cash_flow(normalized_cash_flow)) is not None
    assert repository.count_cash_flows() == 1


def test_position_keeps_quantity_positive() -> None:
    direction, quantity = parse_direction_from_signed_size("-0.0353")
    position = NormalizedPosition(
        venue=Venue.HYPERLIQUID,
        venue_symbol="BTC",
        symbol="BTC-PERP",
        direction=direction,
        quantity=quantity,
        raw_payload={"szi": "-0.0353"},
    )
    assert position.direction is Direction.SHORT
    assert position.quantity > 0


def test_sync_cursor_defaults_to_no_position() -> None:
    cursor = SyncCursor(venue=Venue.HYPERLIQUID, data_type=SyncDataType.FILLS)
    assert cursor.last_timestamp is None
    assert cursor.last_external_id is None


# ----------------------------------------------------------------------
# The adapter contract
# ----------------------------------------------------------------------


def test_protocol_accepts_a_conforming_object() -> None:
    """A plain class satisfies the contract with no inheritance."""

    class StubClient:
        venue = Venue.HYPERLIQUID

        def __init__(self) -> None:
            # Part of the contract since M6: sync service reads 
            # skipped events venue-agnostically
            self.skipped_events: list = []

        def fetch_fills(self, since=None):
            return []

        def fetch_cash_flows(self, since=None):
            return []

        def fetch_open_positions(self):
            return []

        @property
        def supports_positions(self) -> bool:
            return True

    assert isinstance(StubClient(), ReadOnlyExchangeClient)


def test_protocol_rejects_an_incomplete_object() -> None:
    class Incomplete:
        venue = Venue.HYPERLIQUID

        def fetch_fills(self, since=None):
            return []

    assert not isinstance(Incomplete(), ReadOnlyExchangeClient)