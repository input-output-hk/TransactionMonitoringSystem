"""The contract-grouped risk-alerts endpoint.

Covers the two properties the grouped view depends on for correctness: a group's
count is a true count under the active filters (not a page-window artefact), and
the synthetic contract_anomaly class is reconciled into the groups rather than
being invisible in the very view meant to tame it.

Hermetic: the SQL grouping and the sidecar reads are both stubbed.
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.api.contract_anomaly_read import _augment_groups_with_contract_anomaly

GROUPED_URL = "/api/v1/analysis/results/grouped"

DJED = "addr_test1wq9djed"
STRIKE = "addr_test1wq9strike"


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def _dev_mode_auth(monkeypatch):
    from app.auth import api_key

    monkeypatch.setattr(api_key, "_dev_mode", True)


@pytest.fixture(autouse=True)
def _clustering_off(monkeypatch):
    """Default the sidecar off so the SQL path is tested in isolation."""
    from app.config import settings

    monkeypatch.setattr(settings, "CLUSTERING_ENABLED", False)


def _group(address, count=3, worst=72.0, band="High", at=None):
    # clickhouse-driver hands back tz-NAIVE datetimes; the reconciliation has
    # to normalise before comparing against the tz-aware model value.
    at = at if at is not None else datetime(2026, 8, 1, 10, 0)
    return {
        "contract_address": address,
        "alert_count": count,
        "worst_score": worst,
        "worst_band": band,
        "latest_analyzed_at": at,
    }


def _alert_row(tx_hash, score=65.0, band="High", cls="phishing", at=None):
    """A flat score row for an alert that names no contract."""
    return {
        "tx_hash": tx_hash,
        "network": "preprod",
        "max_score": score,
        "max_class": cls,
        "risk_band": band,
        "analyzed_at": at if at is not None else datetime(2026, 8, 1, 9, 0),
    }


def _stub_groups(monkeypatch, groups, unattributed=None, unattributed_total=None):
    """Stub all three halves of the grouped view: the aggregate, the no-contract
    page, and the no-contract COUNT that feeds the pager total."""
    from app.db import clickhouse

    captured: dict = {}
    captured_flat: dict = {}
    captured_count: dict = {}
    rows = list(unattributed or [])

    async def _grouped(**kwargs):
        captured.update(kwargs)
        return [dict(g) for g in groups]

    async def _flat(**kwargs):
        captured_flat.update(kwargs)
        # Honour the endpoint's page-depth bound so the stub cannot hide a
        # regression that fetches (or slices) the wrong number of rows.
        return [dict(r) for r in rows[: kwargs.get("limit")]]

    async def _count(**kwargs):
        captured_count.update(kwargs)
        return len(rows) if unattributed_total is None else unattributed_total

    monkeypatch.setattr(clickhouse, "group_class_scores_by_contract_async", _grouped)
    monkeypatch.setattr(clickhouse, "get_class_scores_list_async", _flat)
    monkeypatch.setattr(clickhouse, "count_class_scores_async", _count)
    captured["_flat"] = captured_flat
    captured["_count"] = captured_count
    return captured


class TestGroupedResponse:
    def test_returns_one_row_per_contract(self, client, monkeypatch):
        _stub_groups(monkeypatch, [_group(DJED, count=12), _group(STRIKE, count=4)])
        body = client.get(f"{GROUPED_URL}?network=preprod").json()
        assert body["total"] == 2
        assert body["count"] == 2
        assert {g["contract_address"] for g in body["data"]} == {DJED, STRIKE}
        assert {g["kind"] for g in body["data"]} == {"group"}

    def test_alert_count_is_carried_through_unchanged(self, client, monkeypatch):
        # The count is the whole point of grouping: a wrong one is worse than no
        # grouping at all, so it must not be recomputed from the page.
        _stub_groups(monkeypatch, [_group(DJED, count=12)])
        body = client.get(f"{GROUPED_URL}?network=preprod").json()
        assert body["data"][0]["alert_count"] == 12

    def test_filters_are_forwarded_to_the_aggregate(self, client, monkeypatch):
        captured = _stub_groups(monkeypatch, [])
        client.get(
            f"{GROUPED_URL}?network=preprod&risk_band=High&min_score=1&attack_class=phishing"
        )
        assert captured["risk_band"] == ["High"]
        assert captured["min_score"] == 1.0
        assert captured["attack_class"] == "phishing"

    def test_pagination_is_by_group(self, client, monkeypatch):
        _stub_groups(
            monkeypatch,
            [
                _group(f"addr{i}", worst=float(90 - i), at=datetime(2026, 8, i + 1, 10, 0))
                for i in range(4)
            ],
        )
        body = client.get(f"{GROUPED_URL}?network=preprod&limit=2&offset=1&sort=score").json()
        assert body["total"] == 4  # total counts rows, not the alerts inside them
        assert body["count"] == 2
        assert [g["contract_address"] for g in body["data"]] == ["addr1", "addr2"]

    def test_unclusterable_marker_reaches_the_row(self, client, monkeypatch):
        # Set by the reconciliation, not by SQL, so a group the aggregate produced
        # alone reports False rather than omitting the field.
        marked = _group(DJED)
        marked["unclusterable_model"] = True
        _stub_groups(monkeypatch, [marked, _group(STRIKE)])
        body = client.get(f"{GROUPED_URL}?network=preprod").json()
        by_address = {r["contract_address"]: r for r in body["data"]}
        assert by_address[DJED]["unclusterable_model"] is True
        assert by_address[STRIKE]["unclusterable_model"] is False

    def test_score_sort_orders_by_worst_first(self, client, monkeypatch):
        _stub_groups(monkeypatch, [_group(DJED, worst=40.0), _group(STRIKE, worst=95.0)])
        body = client.get(f"{GROUPED_URL}?network=preprod&sort=score").json()
        assert [g["contract_address"] for g in body["data"]] == [STRIKE, DJED]


class TestUnattributedAlerts:
    """Alerts naming no contract share the same ordered, paginated list."""

    def test_rendered_as_alert_rows_not_a_bucket(self, client, monkeypatch):
        _stub_groups(monkeypatch, [_group(DJED)], unattributed=[_alert_row("tx9")])
        body = client.get(f"{GROUPED_URL}?network=preprod").json()
        kinds = {r["kind"]: r for r in body["data"]}
        assert set(kinds) == {"group", "alert"}
        alert = kinds["alert"]
        assert alert["tx_hash"] == "tx9"
        assert alert["attack_class"] == "phishing"
        assert alert["contract_address"] == ""
        # One alert per row: nothing is collapsed behind a chevron that has no
        # contract to name.
        assert alert["alert_count"] == 1

    def test_selected_with_the_empty_contract_filter(self, client, monkeypatch):
        captured = _stub_groups(monkeypatch, [], unattributed=[])
        client.get(f"{GROUPED_URL}?network=preprod")
        # The complement of what the aggregate returns, so the two together cover
        # every alert exactly once.
        assert captured["_flat"]["contract"] == ""

    def test_counted_in_the_pager_total(self, client, monkeypatch):
        _stub_groups(
            monkeypatch,
            [_group(DJED)],
            unattributed=[_alert_row("tx8"), _alert_row("tx9")],
        )
        body = client.get(f"{GROUPED_URL}?network=preprod").json()
        # 1 group row + 2 alert rows. Paginating the two lists separately would
        # leave neither pager knowing the combined total.
        assert body["total"] == 3

    def test_interleaved_by_the_active_sort(self, client, monkeypatch):
        _stub_groups(
            monkeypatch,
            [_group(DJED, worst=50.0)],
            unattributed=[_alert_row("tx_hi", score=99.0), _alert_row("tx_lo", score=10.0)],
        )
        body = client.get(f"{GROUPED_URL}?network=preprod&sort=score").json()
        ordering = [r.get("tx_hash") or r["contract_address"] for r in body["data"]]
        assert ordering == ["tx_hi", DJED, "tx_lo"]

    def test_filters_are_applied_to_them_too(self, client, monkeypatch):
        captured = _stub_groups(monkeypatch, [], unattributed=[])
        client.get(f"{GROUPED_URL}?network=preprod&risk_band=High&min_score=1")
        flat = captured["_flat"]
        assert flat["risk_band"] == ["High"]
        assert flat["min_score"] == 1.0

    def test_absent_under_the_anomaly_only_filter(self, client, monkeypatch):
        # contract_anomaly always names its watched target, so no alert under that
        # filter can be un-attributed.
        from app.config import settings

        monkeypatch.setattr(settings, "CLUSTERING_ENABLED", True)
        captured = _stub_groups(monkeypatch, [], unattributed=[_alert_row("tx9")])

        from app.db import clustering_queries

        async def _none(_net, *a, **k):
            return {}

        monkeypatch.setattr(clustering_queries, "flagged_for_network_async", _none)
        body = client.get(f"{GROUPED_URL}?network=preprod&attack_class=contract_anomaly").json()
        assert body["data"] == []
        assert captured["_flat"] == {}
        assert captured["_count"] == {}


class TestUnattributedPaging:
    """The un-attributed side is bounded by page depth, not by its own size."""

    def test_total_comes_from_a_count_not_the_fetched_slice(self, client, monkeypatch):
        # Only the requested page's depth is fetched, so len(rows) would understate
        # the pager. A wrong total here hides alerts behind a page that never
        # renders.
        _stub_groups(
            monkeypatch,
            [_group(DJED)],
            unattributed=[_alert_row(f"tx{i}") for i in range(3)],
            unattributed_total=4_211,
        )
        body = client.get(f"{GROUPED_URL}?network=preprod&limit=2").json()
        assert body["total"] == 1 + 4_211
        assert body["count"] == 2

    def test_fetch_is_bounded_to_the_requested_page_depth(self, client, monkeypatch):
        # Nothing ranked below offset+limit can enter the merge window, so the
        # fetch must not pull the whole un-attributed set to sort it in Python.
        captured = _stub_groups(monkeypatch, [], unattributed=[])
        client.get(f"{GROUPED_URL}?network=preprod&limit=25&offset=50")
        assert captured["_flat"]["limit"] == 75
        assert captured["_flat"]["offset"] == 0

    def test_fetch_uses_the_requested_sort(self, client, monkeypatch):
        # Fetching the newest N and then merging by score would leave a
        # high-scoring older alert unreachable on a score-sorted page 1.
        captured = _stub_groups(monkeypatch, [], unattributed=[])
        client.get(f"{GROUPED_URL}?network=preprod&sort=score")
        assert captured["_flat"]["sort"] == "score"

    def test_count_applies_the_same_filters_as_the_page(self, client, monkeypatch):
        captured = _stub_groups(monkeypatch, [], unattributed=[])
        client.get(f"{GROUPED_URL}?network=preprod&risk_band=High&min_score=1")
        count = captured["_count"]
        assert count["risk_band"] == ["High"]
        assert count["min_score"] == 1.0
        assert count["contract"] == ""


class TestReconciliationGate:
    """The synthetic class is reconciled only where it can satisfy the filter."""

    @staticmethod
    def _spy(monkeypatch):
        from app.api import analysis as analysis_mod
        from app.config import settings

        monkeypatch.setattr(settings, "CLUSTERING_ENABLED", True)
        calls: list[dict] = []

        async def _augment(_net, _groups, **kwargs):
            calls.append(kwargs)

        monkeypatch.setattr(analysis_mod, "_augment_groups_with_contract_anomaly", _augment)
        return calls

    def test_skipped_under_a_nine_class_attack_filter(self, client, monkeypatch):
        # attack_class is a stored-column predicate the synthetic class cannot
        # satisfy: reconciling under it would add groups the filter excludes and
        # decrement groups SQL never counted. Mirrors the flat list's rescue gate.
        calls = self._spy(monkeypatch)
        _stub_groups(monkeypatch, [_group(DJED)], unattributed=[])
        client.get(f"{GROUPED_URL}?network=preprod&attack_class=phishing")
        assert calls == []

    def test_runs_with_no_attack_filter(self, client, monkeypatch):
        calls = self._spy(monkeypatch)
        _stub_groups(monkeypatch, [_group(DJED)], unattributed=[])
        client.get(f"{GROUPED_URL}?network=preprod")
        assert len(calls) == 1
        assert calls[0]["anomaly_only"] is False

    def test_runs_when_the_filter_is_the_synthetic_class_itself(self, client, monkeypatch):
        calls = self._spy(monkeypatch)
        _stub_groups(monkeypatch, [], unattributed=[])
        client.get(f"{GROUPED_URL}?network=preprod&attack_class=contract_anomaly")
        assert len(calls) == 1
        assert calls[0]["anomaly_only"] is True


class TestValidation:
    def test_unknown_attack_class_is_422(self, client, monkeypatch):
        _stub_groups(monkeypatch, [])
        assert client.get(f"{GROUPED_URL}?network=preprod&attack_class=nope").status_code == 422

    def test_bad_sort_is_422(self, client, monkeypatch):
        _stub_groups(monkeypatch, [])
        assert client.get(f"{GROUPED_URL}?network=preprod&sort=sideways").status_code == 422

    def test_anomaly_filter_is_empty_when_clustering_disabled(self, client, monkeypatch):
        # The class cannot exist with the sidecar off, so an empty page is the
        # honest answer rather than an error.
        _stub_groups(monkeypatch, [_group(DJED)])
        body = client.get(f"{GROUPED_URL}?network=preprod&attack_class=contract_anomaly").json()
        assert body == {"count": 0, "total": 0, "data": []}


class TestAnomalyReconciliation:
    """contract_anomaly has no stored column, so groups must be reconciled."""

    @staticmethod
    def _flagged_row(target, verdict="malicious"):
        return {
            "tx_hash": "tx1",
            "target": target,
            "cluster_id": 1,
            "iso_score": 0.9,
            "lof_score": 0.9,
            "consensus": 1.0,
            "votes": 2,
            "verdict": verdict,
            "model_id": "m1",
            "feature_set": "shape",
            "unclusterable_fit": 0,
            "evidence": {},
            "scored_at": datetime(2026, 6, 22, tzinfo=UTC),
        }

    @staticmethod
    def _stored(monkeypatch, rows, flagged):
        from app.db import clickhouse, clustering_queries

        async def _by_hashes(_net, _hashes, *a, **k):
            return rows

        async def _flagged(_net, *a, **k):
            return flagged

        monkeypatch.setattr(clickhouse, "get_class_scores_by_hashes_async", _by_hashes)
        monkeypatch.setattr(clustering_queries, "flagged_for_network_async", _flagged)

    @pytest.mark.anyio
    async def test_verdict_moves_a_tx_to_the_watched_target(self, monkeypatch):
        from tests.analysis.test_contract_anomaly_projection import _full_score_row

        stored = _full_score_row("tx1", 20.0)
        stored["contract_address"] = DJED
        self._stored(monkeypatch, [stored], {"tx1": [self._flagged_row(STRIKE)]})

        groups = [_group(DJED, count=1, worst=20.0, band="Informational")]
        await _augment_groups_with_contract_anomaly(
            "preprod",
            groups,
            bands=None,
            min_score=1.0,
            analyzed_from=None,
            analyzed_to=None,
            min_corroboration=0,
        )
        by_address = {g["contract_address"]: g for g in groups}
        # It left the stored contract's group, which is now empty and dropped,
        # and appears under the target the verdict actually implicates.
        assert DJED not in by_address
        assert by_address[STRIKE]["alert_count"] == 1
        assert by_address[STRIKE]["worst_band"] == "Critical"

    @pytest.mark.anyio
    async def test_group_is_created_when_sql_produced_none(self, monkeypatch):
        from tests.analysis.test_contract_anomaly_projection import _full_score_row

        stored = _full_score_row("tx1", 0.0)
        stored["contract_address"] = ""
        self._stored(monkeypatch, [stored], {"tx1": [self._flagged_row(STRIKE)]})

        groups: list[dict] = []
        await _augment_groups_with_contract_anomaly(
            "preprod",
            groups,
            bands=None,
            min_score=1.0,
            analyzed_from=None,
            analyzed_to=None,
            min_corroboration=0,
        )
        assert [g["contract_address"] for g in groups] == [STRIKE]
        assert groups[0]["alert_count"] == 1

    @pytest.mark.anyio
    async def test_no_double_count_when_target_matches_stored_contract(self, monkeypatch):
        from tests.analysis.test_contract_anomaly_projection import _full_score_row

        stored = _full_score_row("tx1", 70.0)
        stored["contract_address"] = STRIKE
        self._stored(monkeypatch, [stored], {"tx1": [self._flagged_row(STRIKE)]})

        groups = [_group(STRIKE, count=1, worst=70.0, band="High")]
        await _augment_groups_with_contract_anomaly(
            "preprod",
            groups,
            bands=None,
            min_score=1.0,
            analyzed_from=None,
            analyzed_to=None,
            min_corroboration=0,
        )
        # SQL already counted this tx under STRIKE; the verdict raises its score
        # but must not add it a second time.
        assert len(groups) == 1
        assert groups[0]["alert_count"] == 1
        assert groups[0]["worst_band"] == "Critical"

    @pytest.mark.anyio
    async def test_naive_and_aware_timestamps_do_not_raise(self, monkeypatch):
        # The SQL group's latest_analyzed_at arrives tz-naive from ClickHouse while
        # the model's is tz-aware. Comparing them raw raises TypeError, which this
        # endpoint would swallow into a silently unaugmented page.
        from tests.analysis.test_contract_anomaly_projection import _full_score_row

        stored = _full_score_row("tx1", 70.0)
        stored["contract_address"] = STRIKE
        stored["analyzed_at"] = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
        self._stored(monkeypatch, [stored], {"tx1": [self._flagged_row(STRIKE)]})

        groups = [_group(STRIKE, count=1, at=datetime(2026, 8, 1, 10, 0))]
        await _augment_groups_with_contract_anomaly(
            "preprod",
            groups,
            bands=None,
            min_score=1.0,
            analyzed_from=None,
            analyzed_to=None,
            min_corroboration=0,
        )
        assert groups[0]["latest_analyzed_at"] == datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

    @pytest.mark.anyio
    async def test_anomaly_only_excludes_stored_class_detections(self, monkeypatch):
        # Under attack_class=contract_anomaly, a tx whose stored 9-class score
        # still dominates is a stored-class detection and must not appear.
        from tests.analysis.test_contract_anomaly_projection import _full_score_row

        stored = _full_score_row("tx1", 95.0)  # stored Critical, beats the verdict
        stored["contract_address"] = DJED
        self._stored(monkeypatch, [stored], {"tx1": [self._flagged_row(STRIKE, "anomaly")]})

        groups: list[dict] = []
        await _augment_groups_with_contract_anomaly(
            "preprod",
            groups,
            bands=None,
            min_score=1.0,
            analyzed_from=None,
            analyzed_to=None,
            min_corroboration=0,
            anomaly_only=True,
        )
        assert groups == []

    @pytest.mark.anyio
    async def test_unclusterable_verdict_marks_the_group(self, monkeypatch):
        # The un-clusterable marker is how an operator de-prioritises an
        # auto-anomaly whose model could not fit the contract. It rides on the
        # group's WORST row, so it has to be set where worst_score is.
        from tests.analysis.test_contract_anomaly_projection import _full_score_row

        stored = _full_score_row("tx1", 20.0)
        stored["contract_address"] = STRIKE
        flagged = self._flagged_row(STRIKE, "anomaly")
        flagged["unclusterable_fit"] = 1
        self._stored(monkeypatch, [stored], {"tx1": [flagged]})

        groups = [_group(STRIKE, count=1, worst=20.0, band="Informational")]
        await _augment_groups_with_contract_anomaly(
            "preprod",
            groups,
            bands=None,
            min_score=1.0,
            analyzed_from=None,
            analyzed_to=None,
            min_corroboration=0,
        )
        assert groups[0]["unclusterable_model"] is True

    @pytest.mark.anyio
    async def test_marker_is_cleared_when_a_stored_class_tops_the_group(self, monkeypatch):
        # A group marked un-clusterable on a row that no longer tops it would
        # tell the operator to de-prioritise a contract on stale grounds.
        from tests.analysis.test_contract_anomaly_projection import _full_score_row

        stored = _full_score_row("tx1", 95.0)  # stored Critical beats the verdict
        stored["contract_address"] = STRIKE
        flagged = self._flagged_row(STRIKE, "anomaly")
        flagged["unclusterable_fit"] = 1
        self._stored(monkeypatch, [stored], {"tx1": [flagged]})

        groups = [_group(STRIKE, count=1, worst=20.0, band="Informational")]
        await _augment_groups_with_contract_anomaly(
            "preprod",
            groups,
            bands=None,
            min_score=1.0,
            analyzed_from=None,
            analyzed_to=None,
            min_corroboration=0,
        )
        assert groups[0]["worst_score"] == 95.0
        assert groups[0]["unclusterable_model"] is False


class TestGroupExpansionAgreesWithTheCount:
    """A group's count and the rows its expansion lists must describe one set.

    The grouped row is reconciled to the EFFECTIVE contract (a winning sidecar
    verdict re-homes a tx to its watched target), while expanding the group is a
    plain ``?contract=`` list request. The rescue is what keeps the two in step:
    the DB filters on the STORED contract, so a re-homed tx is missing from the
    SQL page even when its score passes, and only the rescue can put it back.
    """

    @pytest.mark.anyio
    async def test_rehomed_alert_is_listed_when_its_group_is_expanded(self, monkeypatch):
        from app.api.contract_anomaly_read import _rescue_flagged_onto_page
        from app.config import settings
        from app.db import clickhouse, clustering_queries
        from tests.analysis.test_contract_anomaly_projection import _full_score_row

        monkeypatch.setattr(settings, "CLUSTERING_ENABLED", True)
        # Stored High under DJED (so the score/band filter alone would have kept
        # it), but the verdict re-homes it to STRIKE and outranks it.
        stored = _full_score_row("tx1", 70.0)
        stored["contract_address"] = DJED
        stored["risk_band"] = "High"

        async def _by_hashes(_net, _hashes, *a, **k):
            return [stored]

        async def _flagged(_net, *a, **k):
            return {"tx1": [TestAnomalyReconciliation._flagged_row(STRIKE)]}

        monkeypatch.setattr(clickhouse, "get_class_scores_by_hashes_async", _by_hashes)
        monkeypatch.setattr(clustering_queries, "flagged_for_network_async", _flagged)

        # The SQL page for ?contract=STRIKE is empty: the stored row names DJED.
        data: list = []
        rescued = await _rescue_flagged_onto_page(
            "preprod",
            data,
            min_score=1.0,
            bands=["High", "Critical"],
            attack_class=None,
            min_corroboration=0,
            analyzed_from=None,
            analyzed_to=None,
            sort="date",
            limit=50,
            offset=0,
            contract=STRIKE,
        )
        assert [d.tx_hash for d in data] == ["tx1"]
        # Additive: the contract predicate, not the score, is what excluded it.
        assert rescued == 1

    @pytest.mark.anyio
    async def test_row_the_db_already_returned_is_not_counted_twice(self, monkeypatch):
        from app.api.contract_anomaly_read import _rescue_flagged_onto_page
        from app.config import settings
        from app.db import clickhouse, clustering_queries
        from tests.analysis.test_contract_anomaly_projection import _full_score_row

        monkeypatch.setattr(settings, "CLUSTERING_ENABLED", True)
        # Stored under the SAME contract the request filters on, and already
        # passing the band filter, so the SQL page returned it.
        stored = _full_score_row("tx2", 70.0)
        stored["contract_address"] = STRIKE
        stored["risk_band"] = "High"

        async def _by_hashes(_net, _hashes, *a, **k):
            return [stored]

        async def _flagged(_net, *a, **k):
            flagged = TestAnomalyReconciliation._flagged_row(STRIKE)
            flagged["tx_hash"] = "tx2"
            return {"tx2": [flagged]}

        monkeypatch.setattr(clickhouse, "get_class_scores_by_hashes_async", _by_hashes)
        monkeypatch.setattr(clustering_queries, "flagged_for_network_async", _flagged)

        # `data` is empty only because this unit test does not run the SQL half;
        # the rescue must still decline it, since the DB filter selected it.
        data: list = []
        rescued = await _rescue_flagged_onto_page(
            "preprod",
            data,
            min_score=1.0,
            bands=["High", "Critical"],
            attack_class=None,
            min_corroboration=0,
            analyzed_from=None,
            analyzed_to=None,
            sort="date",
            limit=50,
            offset=0,
            contract=STRIKE,
        )
        assert rescued == 0
