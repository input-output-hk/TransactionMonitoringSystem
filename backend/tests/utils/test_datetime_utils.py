"""to_aware_utc: the shared tz-normalizer that replaced four hand-rolled copies.

Pins the naive=UTC convention (matching ClickHouse) and the None passthrough, so
comparing a naive ClickHouse timestamp against a tz-aware API bound never raises
the naive-vs-aware TypeError."""

from datetime import datetime, timedelta, timezone

from app.utils.datetime_utils import to_aware_utc


def test_none_passes_through():
    assert to_aware_utc(None) is None


def test_naive_is_assumed_utc():
    out = to_aware_utc(datetime(2026, 6, 1, 12, 0, 0))
    assert out == datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert out.tzinfo is timezone.utc


def test_aware_utc_unchanged():
    aware = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert to_aware_utc(aware) == aware


def test_aware_non_utc_converted_to_utc():
    plus2 = timezone(timedelta(hours=2))
    out = to_aware_utc(datetime(2026, 6, 1, 14, 0, 0, tzinfo=plus2))
    assert out == datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_naive_and_aware_are_comparable_after_normalisation():
    # The whole point: the two forms compare without raising.
    naive = to_aware_utc(datetime(2026, 6, 1, 12, 0, 0))
    aware = to_aware_utc(datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc))
    assert aware < naive
