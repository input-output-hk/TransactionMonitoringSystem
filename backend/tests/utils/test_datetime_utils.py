"""to_aware_utc: the shared tz-normalizer that replaced four hand-rolled copies.

Pins the naive=UTC convention (matching ClickHouse) and the None passthrough, so
comparing a naive ClickHouse timestamp against a tz-aware API bound never raises
the naive-vs-aware TypeError."""

from datetime import UTC, datetime, timedelta, timezone

from app.utils.datetime_utils import to_aware_utc


def test_none_passes_through():
    assert to_aware_utc(None) is None


def test_naive_is_assumed_utc():
    out = to_aware_utc(datetime(2026, 6, 1, 12, 0, 0))
    assert out == datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    assert out.tzinfo is UTC


def test_aware_utc_unchanged():
    aware = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    assert to_aware_utc(aware) == aware


def test_aware_non_utc_converted_to_utc():
    plus2 = timezone(timedelta(hours=2))
    out = to_aware_utc(datetime(2026, 6, 1, 14, 0, 0, tzinfo=plus2))
    assert out == datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def test_naive_and_aware_are_comparable_after_normalisation():
    # The whole point: the two forms compare without raising.
    naive = to_aware_utc(datetime(2026, 6, 1, 12, 0, 0))
    aware = to_aware_utc(datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC))
    assert aware < naive


class TestUtcDateTime:
    """The pydantic wire type: every serialization lands on '...Z'."""

    def _dump(self, value):
        from pydantic import BaseModel

        from app.utils.datetime_utils import UtcDateTime

        class M(BaseModel):
            t: UtcDateTime | None = None

        import json

        return json.loads(M(t=value).model_dump_json())["t"]

    def test_naive_assumed_utc(self):
        assert self._dump(datetime(2026, 7, 15, 12, 30, 45)) == "2026-07-15T12:30:45Z"

    def test_aware_utc(self):
        assert self._dump(datetime(2026, 7, 15, 12, 30, 45, tzinfo=UTC)) == "2026-07-15T12:30:45Z"

    def test_offset_converted(self):
        plus2 = timezone(timedelta(hours=2))
        assert self._dump(datetime(2026, 7, 15, 14, 30, 45, tzinfo=plus2)) == "2026-07-15T12:30:45Z"

    def test_none_passes_through(self):
        assert self._dump(None) is None
