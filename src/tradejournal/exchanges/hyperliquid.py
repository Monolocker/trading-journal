"""Read-only adapter for Hyperliquid's public info endpoint.

Every request this module makes is a read. There is no code here 
with write functionality that can place, cancel or modify an order, 
change leverage, move funds, or sign anything, and none may be added.

Authentication
--------------
There is none. The info endpoint is public and unauthenticated: it is
queried by public wallet address. No API key, no signature, no secret.

    IMPORTANT: query the MASTER account address, not an agent or API
    wallet address. Hyperliquid returns empty results for agent
    addresses, which looks exactly like an account with no history.

Sign conventions
----------------
Hyperliquid uses opposite conventions for fees and funding, and this
adapter normalises both to one account-perspective rule: a negative
number means money left the account.

    fill "fee"      positive means the account paid       -> NEGATED here
    funding "usdc"  positive means the account received   -> passed through

Getting this backwards inverts all fee accounting, so both directions are
covered by tests.

Fees are not cash flows
-----------------------
Trading fees arrive on the fill itself, not as separate ledger events, so
this adapter never emits a CashFlowType.FEE. PnL calculation must sum
Fill.fee and must not also look for fee cash flows, or fees are counted
twice. The venue's "fee" field is already inclusive of "builderFee", so
those must not be added together either.

Endpoints used, all documented and read-only
--------------------------------------------
    userFillsByTime      execution history over a time range
    userFunding          funding payments and receipts over a time range
    clearinghouseState   current perpetual positions

Deliberately not yet used
-------------------------
    userNonFundingLedgerUpdates gives deposits, withdrawals, transfers and
    liquidations. Its per-type delta variants have not been verified
    against official documentation, and this project does not guess at
    response fields. It is scheduled for Milestone 6 with its own
    verification pass. Until then, deposits and withdrawals are absent
    from the journal. They do not affect trade PnL, but they do mean the
    journal is not a complete account ledger.
"""

from __future__ import annotations

import logging
import re
import time as time_module
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from tradejournal.db.connection import datetime_to_epoch_ms
from tradejournal.domain.enums import (
    CashFlowType,
    LiquidityRole,
    Side,
    Venue,
)
from tradejournal.exchanges.normalized import (
    EventParsingError,
    NormalizedCashFlow,
    NormalizedFill,
    NormalizedPosition,
    parse_decimal,
    parse_direction_from_signed_size,
    parse_epoch_ms,
    parse_optional_decimal,
    require_field,
    require_mapping,
)
from tradejournal.exchanges.symbols import (
    SymbolNormalizationError,
    normalize_hyperliquid_symbol,
)

LOGGER = logging.getLogger(__name__)

MAINNET_INFO_URL = "https://api.hyperliquid.xyz/info"
TESTNET_INFO_URL = "https://api.hyperliquid-testnet.xyz/info"

# The server caps a single userFillsByTime response at 2000 fills. This is
# not a request parameter; it is how the adapter recognises a full page and
# therefore knows to ask for another. Configurable only so that tests can
# exercise pagination with small fixtures.
FILLS_PAGE_LIMIT = 2000

# Hyperliquid mainnet did not exist before 2023, so this is the earliest
# useful startTime when no cursor is available.
HYPERLIQUID_EPOCH_MS = 1672531200000  # 2023-01-01T00:00:00Z

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_SECONDS = 1.0

# A runaway loop against a rate-limited endpoint is worse than an error.
MAX_PAGES = 200

# 429 is rate limiting and 5xx is a server fault; both are worth retrying.
# Other 4xx codes mean the request itself is wrong, and retrying an
# incorrect request only consumes rate limit.
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

_ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")


class HyperliquidError(Exception):
    """Base class for every failure raised by this adapter."""


class HyperliquidRequestError(HyperliquidError):
    """A request could not be completed, or returned an error status."""


class HyperliquidResponseError(HyperliquidError):
    """A response was received but could not be trusted or parsed."""


@dataclass(frozen=True, slots=True)
class SkippedEvent:
    """An event this adapter could not normalise and chose not to guess at.

    Recorded rather than discarded so that synchronisation can surface it
    as an alert. Silently dropping an event the journal does not
    understand is precisely the failure mode this project is built to
    avoid.
    """

    data_type: str
    reason: str
    venue_symbol: str | None = None
    venue_event_id: str | None = None


def redact_address(address: str) -> str:
    """Shorten an account address for logs and error messages.

    The address is public information rather than a secret, but it is an
    account identifier, and this project does not write account
    identifiers into logs.
    """
    if not isinstance(address, str) or len(address) < 12:
        return "0x<redacted>"
    return f"{address[:6]}...{address[-4:]}"


def validate_account_address(address: str) -> str:
    """Check an account address at construction rather than at first use.

    A malformed address otherwise surfaces as an empty result set, which
    is indistinguishable from an account with no trading history.
    """
    if not isinstance(address, str):
        raise ValueError(
            f"Account address must be a string, got {type(address).__name__}."
        )
    candidate = address.strip()
    if not _ADDRESS_PATTERN.match(candidate):
        raise ValueError(
            "Account address must be 0x followed by 40 hexadecimal "
            f"characters. Got {len(candidate)} characters."
        )
    return candidate


class HyperliquidClient:
    """Read-only client for one Hyperliquid account.

    The session is injected rather than created internally so that tests
    can supply a stub and never reach the network. Any object with a
    post(url, json=..., timeout=...) method returning an object with
    status_code and json() will do; requests.Session is the production
    choice.
    """

    venue = Venue.HYPERLIQUID

    def __init__(
        self,
        account_address: str,
        *,
        session: Any | None = None,
        info_url: str = MAINNET_INFO_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        page_limit: int = FILLS_PAGE_LIMIT,
        sleep: Callable[[float], None] = time_module.sleep,
    ) -> None:
        self.account_address = validate_account_address(account_address)
        self.info_url = info_url
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.page_limit = page_limit
        self.skipped_events: list[SkippedEvent] = []

        self._sleep = sleep
        self._session = session if session is not None else _default_session()

    def __repr__(self) -> str:
        return (
            f"HyperliquidClient(account={redact_address(self.account_address)})"
        )

    @property
    def supports_positions(self) -> bool:
        """True: clearinghouseState gives a live view of open positions."""
        return True

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _post(self, body: dict[str, Any], *, context: str) -> Any:
        """POST a request body, with a timeout and bounded safe retries.

        Every request made here is a read, so retrying can never duplicate
        an action. Retries are still bounded and are limited to failures
        that plausibly succeed on a second attempt.

        No exception raised from this method contains the request body or
        the full account address.
        """
        last_error: str = "no attempt was made"

        for attempt in range(self.max_retries + 1):
            try:
                response = self._session.post(
                    self.info_url,
                    json=body,
                    timeout=self.timeout_seconds,
                )
            except Exception as error:  # noqa: BLE001 - transport-agnostic
                # The exception text is deliberately reduced to its type.
                # Some HTTP libraries include the full request in repr.
                last_error = f"transport error ({type(error).__name__})"
                if attempt < self.max_retries:
                    self._wait_before_retry(attempt, context, last_error)
                    continue
                raise HyperliquidRequestError(
                    f"{context}: {last_error} after "
                    f"{self.max_retries + 1} attempts"
                ) from None

            status = getattr(response, "status_code", None)
            if status == 200:
                return self._decode(response, context=context)

            if status in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                self._wait_before_retry(attempt, context, f"HTTP {status}")
                continue

            raise HyperliquidRequestError(
                f"{context}: HTTP {status} for account "
                f"{redact_address(self.account_address)}"
            )

        raise HyperliquidRequestError(f"{context}: {last_error}")

    def _wait_before_retry(
        self, attempt: int, context: str, reason: str
    ) -> None:
        delay = self.backoff_seconds * (2**attempt)
        LOGGER.warning(
            "hyperliquid retry",
            extra={
                "context": context,
                "reason": reason,
                "attempt": attempt + 1,
                "delay_seconds": delay,
                "account": redact_address(self.account_address),
            },
        )
        self._sleep(delay)

    def _decode(self, response: Any, *, context: str) -> Any:
        try:
            payload = response.json()
        except Exception as error:  # noqa: BLE001 - parser-agnostic
            raise HyperliquidResponseError(
                f"{context}: response body was not valid JSON "
                f"({type(error).__name__})"
            ) from None
        return payload

    def _post_list(self, body: dict[str, Any], *, context: str) -> list[Any]:
        """POST a request whose documented response is a JSON array."""
        payload = self._post(body, context=context)
        if not isinstance(payload, list):
            raise HyperliquidResponseError(
                f"{context}: expected a JSON array, got "
                f"{type(payload).__name__}"
            )
        return payload

    # ------------------------------------------------------------------
    # Fills
    # ------------------------------------------------------------------

    def fetch_fills(
        self, since: datetime | None = None
    ) -> Sequence[NormalizedFill]:
        """Return fills at or after `since`, oldest first.

        `since` is inclusive, so resuming from a stored cursor re-fetches
        the events on the boundary. That overlap is intentional: it is
        cheaper to receive an event twice than to lose one at a page
        boundary, and the unique constraint on (venue, venue_fill_id)
        makes repeated ingestion harmless.

        The venue's ordering is not assumed. Each page is sorted locally
        by (time, tid) and the cursor is taken from the maximum timestamp
        seen, so the method behaves correctly whether the API returns
        oldest-first or newest-first.
        """
        start_ms = (
            HYPERLIQUID_EPOCH_MS
            if since is None
            else datetime_to_epoch_ms(since)
        )

        collected: list[NormalizedFill] = []
        seen_trade_ids: set[str] = set()

        for page_number in range(MAX_PAGES):
            raw_page = self._post_list(
                {
                    "type": "userFillsByTime",
                    "user": self.account_address,
                    "startTime": start_ms,
                },
                context="userFillsByTime",
            )
            if not raw_page:
                break

            page_max_ms = start_ms
            new_in_page = 0

            for element in raw_page:
                try:
                    entry = require_mapping(element, field_name="fill")
                    trade_id = self._read_identifier(entry, "tid")
                    event_ms = self._read_epoch_ms(entry, "time")
                except EventParsingError as error:
                    self._skip("fills", str(error))
                    continue

                page_max_ms = max(page_max_ms, event_ms)
                if trade_id in seen_trade_ids:
                    continue
                seen_trade_ids.add(trade_id)
                new_in_page += 1

                normalized = self._to_fill(entry, trade_id)
                if normalized is not None:
                    collected.append(normalized)

            if len(raw_page) < self.page_limit:
                break

            if page_max_ms <= start_ms and new_in_page == 0:
                raise HyperliquidResponseError(
                    "userFillsByTime: a full page produced no new fills and "
                    "the cursor could not advance. Refusing to loop or to "
                    "skip ahead, because skipping could silently lose fills."
                )

            start_ms = page_max_ms
        else:
            raise HyperliquidResponseError(
                f"userFillsByTime: exceeded {MAX_PAGES} pages; stopping "
                f"rather than continuing to consume rate limit."
            )

        collected.sort(key=lambda fill: (fill.timestamp, fill.venue_fill_id))
        self._warn_if_empty(collected, "fills")
        return collected

    def _to_fill(
        self, entry: dict[str, Any], trade_id: str
    ) -> NormalizedFill | None:
        """Map one Hyperliquid fill, or record why it was skipped."""
        venue_symbol = entry.get("coin")

        try:
            symbol = normalize_hyperliquid_symbol(venue_symbol)
        except SymbolNormalizationError as error:
            self._skip(
                "fills",
                error.reason,
                venue_symbol=str(venue_symbol),
                venue_event_id=trade_id,
            )
            return None

        try:
            raw_side = require_field(entry, "side", context="fill")
            if raw_side == "B":
                side = Side.BUY
            elif raw_side == "A":
                side = Side.SELL
            else:
                raise EventParsingError(
                    f"fill: unknown side {raw_side!r}; expected 'B' or 'A'"
                )

            crossed = entry.get("crossed")
            if crossed is True:
                liquidity_role = LiquidityRole.TAKER
            elif crossed is False:
                liquidity_role = LiquidityRole.MAKER
            else:
                # Absent or non-boolean: recorded as unknown rather than
                # guessed, because the value drives fee analysis.
                liquidity_role = LiquidityRole.UNKNOWN

            # SIGN FLIP. Hyperliquid reports a positive fee as an amount
            # the account paid, and a negative fee as a rebate received.
            # This project stores every monetary value from the account's
            # perspective, where negative means money left the account.
            venue_fee = parse_decimal(
                require_field(entry, "fee", context="fill"),
                field_name="fee",
            )
            fee = -venue_fee

            order_id = entry.get("oid")

            return NormalizedFill(
                venue=Venue.HYPERLIQUID,
                venue_fill_id=trade_id,
                venue_order_id=None if order_id is None else str(order_id),
                venue_symbol=str(venue_symbol),
                symbol=symbol,
                timestamp=parse_epoch_ms(
                    require_field(entry, "time", context="fill"),
                    field_name="time",
                ),
                side=side,
                price=parse_decimal(
                    require_field(entry, "px", context="fill"),
                    field_name="px",
                ),
                quantity=parse_decimal(
                    require_field(entry, "sz", context="fill"),
                    field_name="sz",
                ),
                fee=fee,
                fee_asset=str(entry.get("feeToken") or "USDC").strip(),
                liquidity_role=liquidity_role,
                raw_payload=entry,
            )
        except EventParsingError as error:
            self._skip(
                "fills",
                str(error),
                venue_symbol=str(venue_symbol),
                venue_event_id=trade_id,
            )
            return None

    # ------------------------------------------------------------------
    # Cash flows
    # ------------------------------------------------------------------

    def fetch_cash_flows(
        self, since: datetime | None = None
    ) -> Sequence[NormalizedCashFlow]:
        """Return funding payments and receipts at or after `since`.

        Funding only. See the module docstring for why non-funding ledger
        events are not yet ingested.

        Hyperliquid supplies no usable identifier for a funding event: the
        hash field is all zeros. A deterministic identifier is therefore
        synthesised as funding:{coin}:{time}. Funding is charged per market
        per hour, so that combination is unique, and because it is derived
        rather than random it is stable across re-imports, which is what
        makes the unique index able to deduplicate.
        """
        start_ms = (
            HYPERLIQUID_EPOCH_MS
            if since is None
            else datetime_to_epoch_ms(since)
        )

        raw_events = self._post_list(
            {
                "type": "userFunding",
                "user": self.account_address,
                "startTime": start_ms,
            },
            context="userFunding",
        )

        collected: list[NormalizedCashFlow] = []
        for element in raw_events:
            normalized = self._to_funding_cash_flow(element)
            if normalized is not None:
                collected.append(normalized)

        collected.sort(
            key=lambda flow: (flow.timestamp, flow.venue_event_id or "")
        )
        self._warn_if_empty(collected, "funding")
        return collected

    def _to_funding_cash_flow(
        self, element: object
    ) -> NormalizedCashFlow | None:
        try:
            entry = require_mapping(element, field_name="funding event")
            delta = require_mapping(
                require_field(entry, "delta", context="funding event"),
                field_name="funding delta",
            )
        except EventParsingError as error:
            self._skip("cash_flows", str(error))
            return None

        venue_symbol = delta.get("coin")
        try:
            symbol = normalize_hyperliquid_symbol(venue_symbol)
        except SymbolNormalizationError as error:
            self._skip(
                "cash_flows", error.reason, venue_symbol=str(venue_symbol)
            )
            return None

        try:
            event_ms = require_field(entry, "time", context="funding event")
            timestamp = parse_epoch_ms(event_ms, field_name="time")

            delta_type = delta.get("type")
            if delta_type != "funding":
                raise EventParsingError(
                    f"funding delta: expected type 'funding', got "
                    f"{delta_type!r}"
                )

            # NO SIGN FLIP. Hyperliquid already reports funding from the
            # account's perspective: positive means the account received
            # funding, negative means it paid. This is the opposite
            # convention to the fee field above, which is why the two are
            # handled differently and tested separately.
            amount = parse_decimal(
                require_field(delta, "usdc", context="funding delta"),
                field_name="usdc",
            )

            return NormalizedCashFlow(
                venue=Venue.HYPERLIQUID,
                venue_event_id=f"funding:{venue_symbol}:{event_ms}",
                venue_symbol=str(venue_symbol),
                symbol=symbol,
                timestamp=timestamp,
                type=CashFlowType.FUNDING,
                amount=amount,
                asset="USDC",
                funding_rate=parse_optional_decimal(
                    delta.get("fundingRate"), field_name="fundingRate"
                ),
                raw_payload=entry,
            )
        except EventParsingError as error:
            self._skip(
                "cash_flows", str(error), venue_symbol=str(venue_symbol)
            )
            return None

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def fetch_open_positions(self) -> Sequence[NormalizedPosition]:
        """Return the account's current perpetual positions.

        Used to detect a position the exchange holds that the journal does
        not know about, and the reverse.
        """
        payload = self._post(
            {"type": "clearinghouseState", "user": self.account_address},
            context="clearinghouseState",
        )
        try:
            state = require_mapping(payload, field_name="clearinghouseState")
            asset_positions = require_field(
                state, "assetPositions", context="clearinghouseState"
            )
        except EventParsingError as error:
            raise HyperliquidResponseError(
                f"clearinghouseState: {error}"
            ) from None

        if not isinstance(asset_positions, list):
            raise HyperliquidResponseError(
                "clearinghouseState: assetPositions was not a JSON array"
            )

        collected: list[NormalizedPosition] = []
        for element in asset_positions:
            normalized = self._to_position(element)
            if normalized is not None:
                collected.append(normalized)
        return collected

    def _to_position(self, element: object) -> NormalizedPosition | None:
        try:
            wrapper = require_mapping(element, field_name="assetPosition")
            position = require_mapping(
                require_field(wrapper, "position", context="assetPosition"),
                field_name="position",
            )
        except EventParsingError as error:
            self._skip("positions", str(error))
            return None

        venue_symbol = position.get("coin")
        try:
            symbol = normalize_hyperliquid_symbol(venue_symbol)
        except SymbolNormalizationError as error:
            self._skip(
                "positions", error.reason, venue_symbol=str(venue_symbol)
            )
            return None

        try:
            direction, quantity = parse_direction_from_signed_size(
                require_field(position, "szi", context="position"),
                field_name="szi",
            )
            return NormalizedPosition(
                venue=Venue.HYPERLIQUID,
                venue_symbol=str(venue_symbol),
                symbol=symbol,
                direction=direction,
                quantity=quantity,
                entry_price=parse_optional_decimal(
                    position.get("entryPx"), field_name="entryPx"
                ),
                raw_payload=wrapper,
            )
        except EventParsingError as error:
            # A zero-size entry is a flat market rather than a position,
            # and is not worth recording as an anomaly.
            if "zero-size" in str(error):
                return None
            self._skip(
                "positions", str(error), venue_symbol=str(venue_symbol)
            )
            return None

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _read_identifier(entry: dict[str, Any], key: str) -> str:
        value = require_field(entry, key, context="fill")
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise EventParsingError(
                f"fill: {key} must be an integer or string, got "
                f"{type(value).__name__}"
            )
        identifier = str(value).strip()
        if not identifier:
            raise EventParsingError(f"fill: {key} was empty")
        return identifier

    @staticmethod
    def _read_epoch_ms(entry: dict[str, Any], key: str) -> int:
        value = require_field(entry, key, context="fill")
        if isinstance(value, bool) or not isinstance(value, int):
            raise EventParsingError(
                f"fill: {key} must be integer milliseconds, got "
                f"{type(value).__name__}"
            )
        return value

    def _skip(
        self,
        data_type: str,
        reason: str,
        *,
        venue_symbol: str | None = None,
        venue_event_id: str | None = None,
    ) -> None:
        """Record an event that could not be normalised confidently."""
        self.skipped_events.append(
            SkippedEvent(
                data_type=data_type,
                reason=reason,
                venue_symbol=venue_symbol,
                venue_event_id=venue_event_id,
            )
        )
        LOGGER.warning(
            "hyperliquid event skipped",
            extra={
                "data_type": data_type,
                "reason": reason,
                "venue_symbol": venue_symbol,
                "account": redact_address(self.account_address),
            },
        )

    def _warn_if_empty(self, collected: Sequence[object], label: str) -> None:
        """Hint at the agent-wallet trap when a result set is empty.

        An agent or API wallet address returns empty results rather than an
        error, which is indistinguishable from an account with no history.
        Saying so once is far cheaper than debugging it.
        """
        if collected:
            return
        LOGGER.info(
            "hyperliquid returned no %s; if this account has history, check "
            "that the configured address is the master account rather than "
            "an agent or API wallet",
            label,
            extra={"account": redact_address(self.account_address)},
        )


def _default_session() -> Any:
    """Build a requests Session, imported lazily.

    Importing inside the function keeps the module importable, and its
    pure-mapping logic testable, in an environment where requests is not
    installed.
    """
    try:
        import requests
    except ImportError as error:  # pragma: no cover - environment issue
        raise HyperliquidError(
            "The requests package is required for live Hyperliquid access. "
            "Install it with: pip install requests"
        ) from error
    return requests.Session()