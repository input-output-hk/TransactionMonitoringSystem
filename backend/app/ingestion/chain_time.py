"""Slot-to-UTC conversion from Ogmios era summaries.

``transactions.timestamp`` is CHAIN time: the analysis baselines window on
it (see ``app/analysis/baselines.py``), and stamping it with ingestion
wall clock (the pre-Ticket-F behavior) collapsed all replayed history into
"now" during catch-up, skewing every 90/180-day window. The converter
derives a block's chain time from its slot using the node's own era
summaries, so replayed history lands at its true position on the time
axis.

Sources, fetched once per chain-sync session by ``OgmiosClient``:

- ``queryNetwork/startTime``: the network's systemStart as ISO-8601.
- ``queryLedgerState/eraSummaries``: per-era ``{start, end, parameters}``
  bounds with slot lengths, all relative to systemStart.

Best-effort by design: any unexpected shape yields ``None`` (no converter
or no per-slot answer) and callers fall back to ingestion wall clock.
Recall-first: a skewed timestamp must never block ingestion.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Unit conversion for the Ogmios v6 era-parameter encoding
# ``slotLength: {"milliseconds": N}``.
MILLISECONDS_PER_SECOND = 1000


def _seconds_of(value: Any) -> float:
    """Read an Ogmios duration: v6 wraps as ``{"seconds": N}`` (era start
    times) or ``{"milliseconds": N}`` (slot lengths); older payloads emit
    a bare number of seconds."""
    if isinstance(value, dict):
        if "milliseconds" in value:
            return float(value["milliseconds"]) / MILLISECONDS_PER_SECOND
        return float(value["seconds"])
    return float(value)


class SlotTimeConverter:
    """Convert absolute slot numbers to UTC datetimes via era summaries."""

    def __init__(
        self, system_start: datetime, eras: List[Tuple[int, float, float]]
    ):
        # eras: (start_slot, start_offset_seconds, slot_length_seconds),
        # ascending by start_slot; offsets are relative to system_start.
        self._system_start = system_start
        self._eras = sorted(eras)

    @classmethod
    def from_ogmios(
        cls, start_time: Any, era_summaries: Any
    ) -> Optional["SlotTimeConverter"]:
        """Build a converter from the raw Ogmios query results.

        Returns None on any unexpected shape; the caller keeps the
        wall-clock fallback rather than trusting a half-parsed summary.
        """
        if not isinstance(start_time, str) or not isinstance(era_summaries, list):
            return None
        if not era_summaries:
            return None
        try:
            system_start = datetime.fromisoformat(start_time)
            if system_start.tzinfo is None:
                system_start = system_start.replace(tzinfo=timezone.utc)
            eras: List[Tuple[int, float, float]] = []
            for summary in era_summaries:
                start = summary["start"]
                slot_length = _seconds_of(summary["parameters"]["slotLength"])
                if slot_length <= 0:
                    return None
                eras.append(
                    (int(start["slot"]), _seconds_of(start["time"]), slot_length)
                )
            return cls(system_start, eras)
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(
                f"Unusable era summaries / start time for slot-time "
                f"conversion: {e}"
            )
            return None

    def slot_to_utc(self, slot: Optional[int]) -> Optional[datetime]:
        """UTC wall time at which ``slot`` started, or None when the slot
        precedes every known era (caller falls back to wall clock).

        Slots beyond the last summary's ``end`` (the node's forecast
        horizon) extrapolate with the last era's slot length: a block that
        EXISTS at that slot is by definition in the current era, whose
        parameters only change at a hard fork, and the summaries are
        refetched on every reconnect.
        """
        if slot is None or slot < 0:
            return None
        era = None
        for candidate in self._eras:
            if slot >= candidate[0]:
                era = candidate
            else:
                break
        if era is None:
            return None
        start_slot, offset_seconds, slot_length = era
        return self._system_start + timedelta(
            seconds=offset_seconds + (slot - start_slot) * slot_length
        )
