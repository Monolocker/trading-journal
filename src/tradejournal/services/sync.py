"""Synchronization: normalized venue events land in SQLite, idempotently.

This service is the only path by which adapter output reaches the
database. It fetches from a ReadOnlyExchangeClient, converts each
normalized event into its domain model, inserts it through the
repository's idempotent insert methods, and advances a per-venue,
per-data-type cursor — all inside one transaction per stream.

Fills and cash flows are independent streams with independent cursors
and independent transactions, so a failure in one does not undo the
other.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from tradejournal.db.connection import transaction, utc_now
from tradejournal.db.repository import Repository
from tradejournal.domain.enums import SyncDataType, Venue
from tradejournal.domain.models import SyncState
from tradejournal.exchanges.base import ReadOnlyExchangeClient
from tradejournal.exchanges.normalized import (
    SkippedEvent,
    to_cash_flow,
    to_fill,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SyncReport:
    """What one sync of one stream did, in numbers.

    fetched = inserted + duplicates + refused, always. `skipped` counts
    separately: those rows never became normalized events at all, so
    they are not part of `fetched`.
    """

    venue: Venue
    data_type: SyncDataType
    fetched: int
    inserted: int
    duplicates: int
    # Cash flows refused because they carry no venue_event_id and would
    # therefore duplicate on every future sync. Always 0 for fills,
    # whose id is structurally required.
    refused: int
    # Rows the adapter declined to normalise during this fetch.
    skipped: tuple[SkippedEvent, ...]
    cursor_before: datetime | None
    cursor_after: datetime | None


class SyncService:
    """Pulls from adapters and writes to the journal database.

    Takes an open connection (the same one the Repository uses) because
    atomicity here is a transaction spanning repository calls, and a
    transaction lives on a connection, not on a repository.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._repository = Repository(connection)

    def sync(
        self, client: ReadOnlyExchangeClient
    ) -> tuple[SyncReport, SyncReport]:
        """Sync both streams for one venue: fills, then cash flows."""
        return (self.sync_fills(client), self.sync_cash_flows(client))

    # ------------------------------------------------------------------
    # Fills
    # ------------------------------------------------------------------

    def sync_fills(self, client: ReadOnlyExchangeClient) -> SyncReport:
        venue = client.venue
        state = self._repository.get_sync_state(venue, SyncDataType.FILLS)
        since = state.last_timestamp if state else None

        skip_mark = len(client.skipped_events)
        fills = client.fetch_fills(since=since)
        skipped = tuple(client.skipped_events[skip_mark:])

        inserted = 0
        newest_timestamp: datetime | None = None
        newest_id: str | None = None
        with transaction(self._connection):
            for normalized in fills:
                if (
                    self._repository.insert_fill(to_fill(normalized))
                    is not None
                ):
                    inserted += 1
                # The adapter contract sorts oldest-first, but the cursor
                # must be right even if a future adapter breaks that, so
                # the newest event is tracked rather than assumed last.
                if (
                    newest_timestamp is None
                    or normalized.timestamp > newest_timestamp
                ):
                    newest_timestamp = normalized.timestamp
                    newest_id = normalized.venue_fill_id
            if newest_timestamp is not None:
                self._repository.upsert_sync_state(
                    SyncState(
                        venue=venue,
                        data_type=SyncDataType.FILLS,
                        last_timestamp=newest_timestamp,
                        last_external_id=newest_id,
                        updated_at=utc_now(),
                    )
                )

        report = SyncReport(
            venue=venue,
            data_type=SyncDataType.FILLS,
            fetched=len(fills),
            inserted=inserted,
            duplicates=len(fills) - inserted,
            refused=0,
            skipped=skipped,
            cursor_before=since,
            cursor_after=newest_timestamp if newest_timestamp else since,
        )
        self._log(report)
        return report

    # ------------------------------------------------------------------
    # Cash flows
    # ------------------------------------------------------------------

    def sync_cash_flows(self, client: ReadOnlyExchangeClient) -> SyncReport:
        venue = client.venue
        state = self._repository.get_sync_state(
            venue, SyncDataType.CASH_FLOWS
        )
        since = state.last_timestamp if state else None

        skip_mark = len(client.skipped_events)
        flows = client.fetch_cash_flows(since=since)
        skipped = tuple(client.skipped_events[skip_mark:])

        inserted = 0
        refused = 0
        newest_timestamp: datetime | None = None
        newest_id: str | None = None
        with transaction(self._connection):
            for normalized in flows:
                if normalized.venue_event_id is None:
                    # See the module docstring: a row the database cannot
                    # recognise as a repeat would duplicate on every
                    # sync, which is the exact failure this service
                    # exists to prevent. Refuse it, loudly.
                    refused += 1
                    LOGGER.warning(
                        "cash flow refused: no venue_event_id, cannot be "
                        "ingested idempotently",
                        extra={
                            "venue": str(venue),
                            "type": str(normalized.type),
                        },
                    )
                    continue
                if (
                    self._repository.insert_cash_flow(
                        to_cash_flow(normalized)
                    )
                    is not None
                ):
                    inserted += 1
                if (
                    newest_timestamp is None
                    or normalized.timestamp > newest_timestamp
                ):
                    newest_timestamp = normalized.timestamp
                    newest_id = normalized.venue_event_id
            if newest_timestamp is not None:
                self._repository.upsert_sync_state(
                    SyncState(
                        venue=venue,
                        data_type=SyncDataType.CASH_FLOWS,
                        last_timestamp=newest_timestamp,
                        last_external_id=newest_id,
                        updated_at=utc_now(),
                    )
                )

        report = SyncReport(
            venue=venue,
            data_type=SyncDataType.CASH_FLOWS,
            fetched=len(flows),
            inserted=inserted,
            duplicates=len(flows) - inserted - refused,
            refused=refused,
            skipped=skipped,
            cursor_before=since,
            cursor_after=newest_timestamp if newest_timestamp else since,
        )
        self._log(report)
        return report

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    @staticmethod
    def _log(report: SyncReport) -> None:
        LOGGER.info(
            "sync %s %s: fetched=%d inserted=%d duplicates=%d refused=%d "
            "skipped=%d",
            report.venue,
            report.data_type,
            report.fetched,
            report.inserted,
            report.duplicates,
            report.refused,
            len(report.skipped),
        )
        if report.skipped:
            LOGGER.warning(
                "sync %s %s: %d row(s) were skipped by the adapter and "
                "will NOT be retried automatically; review them",
                report.venue,
                report.data_type,
                len(report.skipped),
            )