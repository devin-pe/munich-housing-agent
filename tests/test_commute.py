"""Deterministic tests for the commute filter and arrival-time logic.

These use a stub provider so they don't depend on any external API (the free
transit mirrors are flaky and Google needs a key). Run: python -m pytest -q
or simply: python tests/test_commute.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from housing_agent.commute import CommuteFilter, next_weekday_arrival, BERLIN
from housing_agent.config import load_config
from housing_agent.models import Listing


def _mk_listing(idx: int) -> Listing:
    # Distinct coordinates per listing so each gets its own commute-cache key.
    return Listing(
        source="test", url=f"http://x/{idx}", listing_id=str(idx),
        title=f"Listing {idx}", price_eur=1200, price_type="warm", rooms=1,
        furnished=True, address_or_area="Somewhere, München",
        lat=48.14 + idx * 0.001, lng=11.57 + idx * 0.001,
    )


def test_next_weekday_arrival_skips_weekend():
    # Saturday 2026-07-11 12:00 -> next weekday is Monday 2026-07-13 09:00
    sat = datetime(2026, 7, 11, 12, 0, tzinfo=BERLIN)
    arr = next_weekday_arrival("09:00", base=sat)
    assert arr.weekday() < 5, "arrival must be a weekday"
    assert (arr.hour, arr.minute) == (9, 0)
    assert arr.date() == datetime(2026, 7, 13).date()


def test_filter_keeps_under_drops_over_flags_unknown():
    cfg = load_config()
    cfg.search.max_commute_minutes = 40

    # Stub provider: returns a preset time per listing id.
    minutes_by_id = {"1": 15, "2": 40, "3": 41, "4": None}

    filt = CommuteFilter(cfg)

    class Stub:
        name = "stub"
        available = True
        def travel_minutes(self, origin, dest, arrival):
            return Stub._current
    filt.provider = Stub()
    # Ensure a fresh, isolated cache so we don't read stale values.
    filt.cache._data = {}

    listings = [_mk_listing(i) for i in (1, 2, 3, 4)]
    kept = []
    for lg in listings:
        Stub._current = minutes_by_id[lg.listing_id]
        k, _ = filt.annotate_and_filter([lg])
        kept.extend(k)

    by_id = {lg.listing_id: lg for lg in kept}
    assert "1" in by_id and by_id["1"].commute_minutes == 15   # under -> keep
    assert "2" in by_id and by_id["2"].commute_minutes == 40   # == limit -> keep
    assert "3" not in by_id                                    # over -> dropped
    assert "4" in by_id and by_id["4"].commute_minutes is None # unknown -> kept, flagged


if __name__ == "__main__":
    test_next_weekday_arrival_skips_weekend()
    test_filter_keeps_under_drops_over_flags_unknown()
    print("OK: all commute tests passed")
