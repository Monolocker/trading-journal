"""Normalized exchange events and the parsing rules that produce them.

An adapter's only job is to turn a venue's response into these types. Once
an event is normalized, no downstream code needs to know which venue it
came from or what shape its JSON had.

These differ from the domain models in domain/models.py by exactly the
fields that belong to the database: id, leg_id, trade_id and ingested_at.
An adapter has no way to know which leg a fill belongs to, so it does not
carry a field it cannot fill in. Leg assignment happens in Milestone 7.

Parsing here is deliberately stricter than the storage conversion in
db/connection.py. That code round-trips values we produced ourselves;
this code reads untrusted input from a remote API, where a wrong type or
an out-of-range number is a real possibility rather than a bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from tradejournal.db.connection import utc_now
from tradejournal.domain.enums import (
    CashFlowType,
    Direction,
    LiquidityRole,
    Side,
    SyncDataType,
    Venue,
)
from tradejournal.domain.models import CashFlow, Fill

# Sanity bounds for incoming timestamps.
#
# The lower bound catches the most common integration error in this space:
# a venue returning seconds where milliseconds were expected. A seconds
# value such as 1768478400 read as milliseconds lands in January 1970,
# which fails here instead of silently filing an event 56 years early.
MINIMUM_TIMESTAMP = datetime(2015, 1, 1, tzinfo=UTC)
FUTURE_TOLERANCE = timedelta(days=2)


class EventParsingError(ValueError):
    """Raised when a venue response cannot be parsed safely."""


# --------------------------------------------------------------------------
# Parsing rules for untrusted API values
# --------------------------------------------------------------------------


def parse_decimal(value: object, *, field_name: str = "value") -> Decimal:
    """Parse a monetary value from an API response.

    Accepts strings and integers. Floats are rejected: by the time a float
    reaches this function its precision has already been lost, so storing
    it would record a subtly wrong number rather than raise a visible
    error. Both venues return monetary values as strings today.

    Booleans are rejected explicitly because bool is a subclass of int in
    Python, so True would otherwise parse as Decimal(1).
    """
    if isinstance(value, bool):
        raise EventParsingError(
            f"{field_name}: refusing a boolean where a number was expected"
        )
    if isinstance(value, float):
        raise EventParsingError(
            f"{field_name}: refusing a float, whose precision is already "
            f"lost; monetary values must arrive as strings or integers"
        )
    if not isinstance(value, (str, int)):
        raise EventParsingError(
            f"{field_name}: expected a string or integer, got "
            f"{type(value).__name__}"
        )

    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as error:
        raise EventParsingError(
            f"{field_name}: {value!r} is not a valid decimal"
        ) from error

    if not result.is_finite():
        raise EventParsingError(f"{field_name}: {value!r} is not finite")
    return result


def parse_optional_decimal(
    value: object, *, field_name: str = "value"
) -> Decimal | None:
    """Parse a monetary value that the venue may legitimately omit."""
    if value is None:
        return None
    return parse_decimal(value, field_name=field_name)


def parse_epoch_ms(
    value: object,
    *,
    field_name: str = "timestamp",
    now: datetime | None = None,
) -> datetime:
    """Parse epoch milliseconds into a timezone-aware UTC datetime.

    Range-checked rather than merely type-checked. A value outside the
    plausible window is far more likely to be a unit mistake than a real
    event, and failing here turns a silent data-quality problem into a
    visible parse error.

    now is injectable so that the upper bound is testable without
    depending on the wall clock.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise EventParsingError(
            f"{field_name}: expected integer milliseconds, got "
            f"{type(value).__name__}"
        )

    try:
        moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
            milliseconds=value
        )
    except (OverflowError, OSError, ValueError) as error:
        raise EventParsingError(
            f"{field_name}: {value!r} is out of representable range"
        ) from error

    _check_timestamp_range(moment, field_name=field_name, now=now)
    return moment


def parse_iso8601(
    value: object,
    *,
    field_name: str = "timestamp",
    now: datetime | None = None,
) -> datetime:
    """Parse an ISO-8601 timestamp into a timezone-aware UTC datetime.

    A timestamp without a timezone is rejected rather than assumed to be
    UTC. Assuming the offset is exactly how funding events end up filed
    hours away from where they belong.
    """
    if not isinstance(value, str):
        raise EventParsingError(
            f"{field_name}: expected an ISO-8601 string, got "
            f"{type(value).__name__}"
        )

    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as error:
        raise EventParsingError(
            f"{field_name}: {value!r} is not a valid ISO-8601 timestamp"
        ) from error

    if parsed.tzinfo is None:
        raise EventParsingError(
            f"{field_name}: {value!r} has no timezone; refusing to assume UTC"
        )

    moment = parsed.astimezone(UTC)
    _check_timestamp_range(moment, field_name=field_name, now=now)
    return moment


def _check_timestamp_range(
    moment: datetime, *, field_name: str, now: datetime | None
) -> None:
    reference = now or utc_now()
    if moment < MINIMUM_TIMESTAMP:
        raise EventParsingError(
            f"{field_name}: {moment.isoformat()} is early; "
            f"the value may be in seconds rather than milliseconds"
        )
    if moment > reference + FUTURE_TOLERANCE:
        raise EventParsingError(
            f"{field_name}: {moment.isoformat()} is far in the future"
        )


def parse_direction_from_signed_size(
    value: object, *, field_name: str = "size"
) -> tuple[Direction, Decimal]:
    """Split a signed position size into a direction and absolute quantity.

    Venues commonly report a position as one signed number, where a
    negative value means short. Everything downstream stores direction and
    magnitude separately, so that a sign convention never has to be
    remembered in two places.
    """
    size = parse_decimal(value, field_name=field_name)
    if size > 0:
        return (Direction.LONG, size)
    if size < 0:
        return (Direction.SHORT, -size)
    raise EventParsingError(f"{field_name}: a zero-size position has no direction")


def require_mapping(value: object, *, field_name: str = "payload") -> dict:
    """Assert that an API element is a JSON object before indexing into it.

    Guards against a malformed response where a list of objects turns out
    to contain a string or null, which would otherwise raise an obscure
    TypeError deep inside an adapter.
    """
    if not isinstance(value, dict):
        raise EventParsingError(
            f"{field_name}: expected a JSON object, got {type(value).__name__}"
        )
    return value


def require_field(
    payload: dict, key: str, *, context: str = "payload"
) -> object:
    """Read a required key, failing with a message naming what was missing."""
    if key not in payload:
        raise EventParsingError(f"{context}: missing required field {key!r}")
    return payload[key]


# --------------------------------------------------------------------------
# Normalized event models
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NormalizedFill:
    """A single execution, translated out of a venue's response format.

    venue_symbol preserves what the venue called the market; symbol holds
    the canonical name. Both are kept so that a normalization mistake can
    be diagnosed later without re-fetching.
    """

    venue: Venue
    venue_fill_id: str
    venue_symbol: str
    symbol: str
    timestamp: datetime
    side: Side
    price: Decimal
    quantity: Decimal
    fee: Decimal
    fee_asset: str
    liquidity_role: LiquidityRole
    raw_payload: dict[str, Any]
    venue_order_id: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedCashFlow:
    """An account-level cash movement, translated out of a venue response.

    Sign convention, applied by the adapter before construction: amounts
    are from the account's perspective. Funding received and rebates are
    positive; funding paid and fees are negative.

    symbol is None for account-level events such as deposits, which belong
    to no market.
    """

    venue: Venue
    timestamp: datetime
    type: CashFlowType
    amount: Decimal
    asset: str
    raw_payload: dict[str, Any]
    venue_event_id: str | None = None
    venue_symbol: str | None = None
    symbol: str | None = None
    funding_rate: Decimal | None = None


@dataclass(frozen=True, slots=True)
class NormalizedPosition:
    """A live position snapshot reported by a venue.

    This has no domain equivalent because it is never stored. It exists to
    answer one reconciliation question: does the exchange currently hold a
    position that the journal does not know about, or vice versa?

    quantity is always positive; direction carries the sign.
    """

    venue: Venue
    venue_symbol: str
    symbol: str
    direction: Direction
    quantity: Decimal
    raw_payload: dict[str, Any]
    entry_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SyncCursor:
    """Where synchronisation of one venue and data type last reached.

    Frozen, and without the updated_at bookkeeping field that SyncState
    carries, because a cursor in flight is a value rather than a record.
    """

    venue: Venue
    data_type: SyncDataType
    last_timestamp: datetime | None = None
    last_external_id: str | None = None


# --------------------------------------------------------------------------
# Conversion into domain models
# --------------------------------------------------------------------------


def to_fill(
    normalized: NormalizedFill,
    *,
    leg_id: int | None = None,
    ingested_at: datetime | None = None,
) -> Fill:
    """Convert a normalized fill into a storable domain fill.

    leg_id stays None unless a caller has confidently determined it. That
    default is the mechanism behind the rule that uncertain events are
    never forced into a trade.
    """
    return Fill(
        venue=normalized.venue,
        venue_fill_id=normalized.venue_fill_id,
        venue_order_id=normalized.venue_order_id,
        venue_symbol=normalized.venue_symbol,
        symbol=normalized.symbol,
        timestamp=normalized.timestamp,
        side=normalized.side,
        price=normalized.price,
        quantity=normalized.quantity,
        fee=normalized.fee,
        fee_asset=normalized.fee_asset,
        liquidity_role=normalized.liquidity_role,
        raw_payload=normalized.raw_payload,
        leg_id=leg_id,
        ingested_at=ingested_at or utc_now(),
    )


def to_cash_flow(
    normalized: NormalizedCashFlow,
    *,
    trade_id: int | None = None,
    leg_id: int | None = None,
    ingested_at: datetime | None = None,
) -> CashFlow:
    """Convert a normalized cash flow into a storable domain cash flow."""
    return CashFlow(
        venue=normalized.venue,
        venue_event_id=normalized.venue_event_id,
        venue_symbol=normalized.venue_symbol,
        symbol=normalized.symbol,
        timestamp=normalized.timestamp,
        type=normalized.type,
        amount=normalized.amount,
        asset=normalized.asset,
        funding_rate=normalized.funding_rate,
        raw_payload=normalized.raw_payload,
        trade_id=trade_id,
        leg_id=leg_id,
        ingested_at=ingested_at or utc_now(),
    )