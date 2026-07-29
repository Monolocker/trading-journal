"""The contract every venue adapter satisfies.

This is a typing.Protocol rather than an abstract base class. A Protocol
is structural: any object with matching methods satisfies it, with no
inheritance and no registration. This keeps adapters as plain classes with
no framework attached, which is the whole point of having only two of them.

An abstract base class would work equally well and would fail earlier if a
method were missing. It was not chosen because it would force every adapter
to import and inherit from this module for no benefit that a type checker
does not already provide.

Read-only mandate: every method here reads. There is deliberately no method
for write functionality such as: placing, cancelling or modifying an order, 
changing leverage, or moving funds, and none may be added. An adapter that 
needs a write endpoint does not belong in this application.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from tradejournal.domain.enums import Venue
from tradejournal.exchanges.normalized import (
    NormalizedCashFlow,
    NormalizedFill,
    NormalizedPosition,
)


@runtime_checkable
class ReadOnlyExchangeClient(Protocol):
    """A read-only source of trading history for one venue."""

    venue: Venue

    def fetch_fills(
        self, since: datetime | None = None
    ) -> Sequence[NormalizedFill]:
        """Return fills at or after `since`, oldest first.

        `since` is inclusive, so a caller resuming from a stored cursor may
        receive events it already holds. That overlap is intentional: it is
        cheaper to re-receive an event than to risk missing one at a page
        boundary, and the unique constraints in the database make repeated
        ingestion harmless.
        """
        ...

    def fetch_cash_flows(
        self, since: datetime | None = None
    ) -> Sequence[NormalizedCashFlow]:
        """Return funding and other cash events at or after `since`."""
        ...

    def fetch_open_positions(self) -> Sequence[NormalizedPosition]:
        """Return the venue's current open positions.

        Used to detect a position the exchange reports but the journal does
        not hold, and the reverse.

        An adapter with no live view of the account - a file importer, for
        instance - returns an empty sequence. That is a genuine limitation
        rather than an absence of positions, so an adapter in that
        situation must also set supports_positions to False and the
        reconciliation service must skip position checks for that venue
        rather than concluding everything was closed. This so happens to 
        be the case with Variational due to lack of read-only API functionality
        currently being early-stage.
        """
        ...

    @property
    def supports_positions(self) -> bool:
        """Whether fetch_open_positions reflects live account state.

        False means the venue's data reaches this application through a
        historical export with no live position view. Reconciliation uses
        this to distinguish "the exchange reports no position" from "we
        cannot see the exchange's positions at all", which are very
        different findings.
        """
        ...



