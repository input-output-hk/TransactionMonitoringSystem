"""Unit tests for the analysis engine orchestrator."""

import asyncio
import functools
import time
from unittest.mock import patch

from app.analysis.engine import (
    _CLASS_NAMES,
    _analysis_defer_attempts,
    _build_scorers,
    _handle_incomplete_scoring,
    _score_transaction,
)
from app.analysis.normalise import score_to_band
from app.analysis.scorers.base import ScorerResult


class _FixedScorer:
    """Test double that gates open and returns a fixed score for a class."""

    def __init__(self, name, score):
        self.name = name
        self._score = score

    def gate(self, features):
        return True

    def score(self, features):
        return ScorerResult(score=self._score)


def _make_row(tx_hash="tx01", metadata=None, raw_data=None):
    return {
        "tx_hash": tx_hash,
        "network": "preprod",
        "fee": 200_000,
        "input_count": 2,
        "output_count": 3,
        "total_output_value": 10_000_000,
        "metadata": metadata,
        "addresses": ["addr_test1qzabc"],
        "raw_data": raw_data or "{}",
        "slot": 50000,
        "block_height": 1000,
        "timestamp": "2025-01-01T00:00:00Z",
    }


class TestScoreTransaction:
    def test_all_classes_present(self):
        """Result dict should have all 9 class keys."""
        row = _make_row()
        scorers = _build_scorers()
        result = _score_transaction(row, scorers)
        for name in _CLASS_NAMES:
            assert name in result
            # No gates pass on a plain tx, so all should be -1
            assert result[name] == -1.0

    def test_max_score_zero_when_no_gates_pass(self):
        row = _make_row()
        result = _score_transaction(row, _build_scorers())
        assert result["max_score"] == 0.0
        assert result["max_class"] == ""
        assert result["risk_band"] == "Informational"

    def test_metadata_dict_parsed(self):
        """Dict metadata should be usable by scorers."""
        row = _make_row(metadata={"674": "visit https://cardano-airdrop.scam.com"})
        result = _score_transaction(row, _build_scorers())
        # Phishing gate should pass
        assert result["phishing"] >= 0

    def test_scorer_exception_handled(self):
        """A crashing scorer should not break the pipeline."""

        class BadScorer:
            name = "phishing"

            def gate(self, features):
                raise RuntimeError("boom")

        row = _make_row()
        result = _score_transaction(row, [BadScorer()])
        assert result["phishing"] == -1.0  # stays at default

    def test_result_has_required_fields(self):
        row = _make_row()
        result = _score_transaction(row, _build_scorers())
        for field in (
            "tx_hash",
            "network",
            "max_score",
            "max_class",
            "risk_band",
            "sub_scores",
            "analysis_version",
            "analyzed_at",
        ):
            assert field in result


class TestCorroboration:
    def test_counts_only_classes_above_threshold(self):
        """corroboration_count = number of distinct classes at/above the
        threshold; corroborating_classes lists them sorted. A sub-threshold
        class is excluded."""
        row = _make_row()
        scorers = [
            _FixedScorer("sandwich", 50.0),  # >= 40 -> counts
            _FixedScorer("token_dust", 45.0),  # >= 40 -> counts
            _FixedScorer("circular", 10.0),  # < 40 -> excluded
        ]
        result = _score_transaction(row, scorers)
        assert result["corroboration_count"] == 2
        assert result["corroborating_classes"] == "sandwich,token_dust"

    def test_single_class_not_corroborated(self):
        row = _make_row()
        result = _score_transaction(row, [_FixedScorer("sandwich", 90.0)])
        assert result["corroboration_count"] == 1
        assert result["corroborating_classes"] == "sandwich"

    def test_corroboration_does_not_change_band(self):
        """Two corroborating classes both in the Moderate range must NOT
        escalate the band: risk_band stays exactly score_to_band(max_score).
        This pins Option B (flag-only) against any future regression that wires
        corroboration into banding."""
        row = _make_row()
        scorers = [_FixedScorer("sandwich", 50.0), _FixedScorer("token_dust", 45.0)]
        result = _score_transaction(row, scorers)
        assert result["max_score"] == 50.0
        assert result["risk_band"] == score_to_band(result["max_score"]) == "Moderate"


class TestBuildScorers:
    def test_all_scorers_returned(self):
        scorers = _build_scorers()
        names = {s.name for s in scorers}
        assert names == set(_CLASS_NAMES)

    @patch("app.analysis.engine.settings")
    def test_disabled_scorer_excluded(self, mock_settings):
        mock_settings.ANALYSIS_ENABLED = True
        # Disable phishing, enable everything else
        for name in _CLASS_NAMES:
            flag = f"SCORER_{name.upper()}_ENABLED"
            setattr(mock_settings, flag, name != "phishing")
        scorers = _build_scorers()
        names = {s.name for s in scorers}
        assert "phishing" not in names
        assert len(names) == 8


class _RaisingScorer:
    """Test double whose gate opens but whose score() raises, simulating a
    crafted-input crash or a transient dependency error inside one scorer."""

    def __init__(self, name):
        self.name = name

    def gate(self, features):
        return True

    def score(self, features):
        raise RuntimeError("boom")


def _incomplete_result(tx_hash="txfail", failed_scorers=None, failed_enrichment=None):
    return {
        "tx_hash": tx_hash,
        "network": "preprod",
        "evidence": {},
        "_failed_scorers": failed_scorers or [],
        "_enrichment_failed": failed_enrichment or [],
    }


class TestIncompleteScoring:
    """Recall-first: a scorer that raises, or an enrichment that fails, must
    not silently persist a row with the affected class at the -1 sentinel
    (which the unanalyzed anti-join would then treat as permanently scored).
    The tx is deferred and retried, then degraded with a visible marker."""

    def setup_method(self):
        _analysis_defer_attempts.clear()

    def teardown_method(self):
        _analysis_defer_attempts.clear()

    def test_raising_scorer_is_reported_not_swallowed(self):
        row = _make_row()
        result = _score_transaction(row, [_RaisingScorer("front_running")])
        # The class stays at the -1 sentinel (it could not be scored)...
        assert result["front_running"] == -1.0
        # ...but the failure is surfaced so the engine can defer, not swallow.
        assert result["_failed_scorers"] == ["front_running"]

    @patch("app.analysis.engine.settings")
    def test_incomplete_tx_is_deferred_then_degraded(self, ms):
        ms.ANALYSIS_DEFER_ENABLED = True
        ms.ANALYSIS_DEFER_MAX_ATTEMPTS = 2
        ms.ANALYSIS_DEFER_RETRY_SECONDS = 0  # never pace-skip counting in test
        # Attempt 1 (1 < 2): deferred, dropped from the batch, no row written.
        kept = _handle_incomplete_scoring(
            [_incomplete_result(failed_scorers=["phishing"])], "preprod"
        )
        assert kept == []
        # Attempt 2 (2 < 2 is False): budget exhausted, kept WITH a marker.
        kept = _handle_incomplete_scoring(
            [_incomplete_result(failed_scorers=["phishing"])], "preprod"
        )
        assert len(kept) == 1
        assert kept[0]["evidence"]["_meta"]["scorer_failed"] == ["phishing"]

    @patch("app.analysis.engine.settings")
    def test_enrichment_failure_deferred_like_scorer_failure(self, ms):
        ms.ANALYSIS_DEFER_ENABLED = True
        ms.ANALYSIS_DEFER_MAX_ATTEMPTS = 1  # degrade immediately
        ms.ANALYSIS_DEFER_RETRY_SECONDS = 0
        kept = _handle_incomplete_scoring(
            [_incomplete_result(failed_enrichment=["collision"])], "preprod"
        )
        assert len(kept) == 1
        assert kept[0]["evidence"]["_meta"]["enrichment_unavailable"] == ["collision"]

    @patch("app.analysis.engine.settings")
    def test_complete_tx_passes_through_and_clears_ledger(self, ms):
        ms.ANALYSIS_DEFER_ENABLED = True
        ms.ANALYSIS_DEFER_MAX_ATTEMPTS = 3
        ms.ANALYSIS_DEFER_RETRY_SECONDS = 0
        _analysis_defer_attempts[("preprod", "txok")] = (1, 0.0)  # stale entry
        kept = _handle_incomplete_scoring([_incomplete_result(tx_hash="txok")], "preprod")
        assert len(kept) == 1
        assert ("preprod", "txok") not in _analysis_defer_attempts
        # Transient bookkeeping keys are stripped before insert.
        assert "_failed_scorers" not in kept[0]
        assert "_enrichment_failed" not in kept[0]

    @patch("app.analysis.engine.settings")
    def test_defer_disabled_keeps_row_but_strips_transient_keys(self, ms):
        ms.ANALYSIS_DEFER_ENABLED = False
        kept = _handle_incomplete_scoring(
            [_incomplete_result(failed_scorers=["phishing"])], "preprod"
        )
        assert len(kept) == 1
        assert "_failed_scorers" not in kept[0]
        assert "_enrichment_failed" not in kept[0]


from datetime import UTC, datetime, timedelta

import pytest

import app.analysis.engine as _engine_mod
from app.config import settings

# Test values for the lane knobs, set on the patched settings below.
_LIVE_WINDOW_SECONDS = 600
_LIVE_BATCH = 100
_BACKLOG_PICK = 2000
_RANGE_START = datetime(2026, 3, 2, tzinfo=UTC)
_RANGE_END = _RANGE_START + timedelta(days=1)


class TestLanes:
    """The live lane polls the newest of its window, the backlog lane the
    oldest of the range it is handed: a backlog of any size cannot sit in
    front of a fresh transaction."""

    @patch("app.analysis.engine.clickhouse")
    @patch("app.analysis.engine.settings")
    def test_live_lane_polls_its_window_newest_first(self, ms, mock_ch):
        ms.ANALYSIS_ENABLED = True
        ms.ANALYSIS_ENGINE_BATCH_SIZE = _LIVE_BATCH
        ms.ANALYSIS_LIVE_WINDOW_SECONDS = _LIVE_WINDOW_SECONDS
        mock_ch.get_unanalyzed_transactions.return_value = []
        window = timedelta(seconds=_LIVE_WINDOW_SECONDS)
        before = datetime.now(UTC)
        assert _engine_mod.run_once("preprod") == 0
        args, kwargs = mock_ch.get_unanalyzed_transactions.call_args
        assert args == ("preprod", _LIVE_BATCH)
        assert kwargs["newest_first"] is True
        assert "until" not in kwargs
        assert before - window <= kwargs["since"] <= datetime.now(UTC) - window

    @patch("app.analysis.engine.clickhouse")
    @patch("app.analysis.engine.settings")
    def test_backlog_lane_picks_its_range_oldest_first(self, ms, mock_ch):
        ms.ANALYSIS_BACKLOG_PICK_SIZE = _BACKLOG_PICK
        # Naive, as ClickHouse hands them back.
        stamp = datetime(2026, 3, 2, 9, 0, 0)
        mock_ch.pick_unanalyzed.return_value = [("a", stamp)]
        picked = _engine_mod.pick_backlog("preprod", _RANGE_START, _RANGE_END)
        assert picked == [("a", stamp.replace(tzinfo=UTC))]
        args, kwargs = mock_ch.pick_unanalyzed.call_args
        assert args == ("preprod", _BACKLOG_PICK)
        assert kwargs == {"since": _RANGE_START, "until": _RANGE_END}

    @patch("app.analysis.engine._score_batch", return_value=1)
    @patch("app.analysis.engine.clickhouse")
    @patch("app.analysis.engine.settings")
    def test_a_backlog_batch_reports_what_it_scored(self, ms, mock_ch, _scored):
        ms.ANALYSIS_ENABLED = True
        mock_ch.hydrate_transactions.return_value = [{"tx_hash": "a"}, {"tx_hash": "b"}]
        batch = _engine_mod.run_backlog_once("preprod", ["a", "b"], _RANGE_START, _RANGE_END)
        # One deferred: the lane knows not to pick the slice again at once.
        assert batch == (2, 1)
        args, kwargs = mock_ch.hydrate_transactions.call_args
        assert args == ("preprod", ["a", "b"])
        assert kwargs == {"since": _RANGE_START, "until": _RANGE_END}

    @patch("app.analysis.engine.clickhouse")
    @patch("app.analysis.engine.settings")
    def test_neither_lane_scores_while_analysis_is_off(self, ms, mock_ch):
        ms.ANALYSIS_ENABLED = False
        assert _engine_mod.run_once("preprod") == 0
        assert _engine_mod.run_backlog_once("preprod", ["a"], _RANGE_START, _RANGE_END) == (0, 0)
        mock_ch.get_unanalyzed_transactions.assert_not_called()
        mock_ch.hydrate_transactions.assert_not_called()


class TestInFlightClaims:
    """A transaction is scored by one lane at a time."""

    def teardown_method(self):
        _engine_mod._in_flight.clear()

    def test_what_one_lane_holds_the_other_does_not_take(self):
        live = _engine_mod._claim("preprod", [{"tx_hash": "a"}, {"tx_hash": "b"}])
        backlog = _engine_mod._claim("preprod", [{"tx_hash": "b"}, {"tx_hash": "c"}])
        assert [r["tx_hash"] for r in live] == ["a", "b"]
        assert [r["tx_hash"] for r in backlog] == ["c"]

    def test_a_release_frees_the_claim(self):
        _engine_mod._release("preprod", _engine_mod._claim("preprod", [{"tx_hash": "a"}]))
        assert _engine_mod._claim("preprod", [{"tx_hash": "a"}]) == [{"tx_hash": "a"}]

    def test_claims_are_per_network(self):
        _engine_mod._claim("preprod", [{"tx_hash": "a"}])
        assert _engine_mod._claim("preview", [{"tx_hash": "a"}]) == [{"tx_hash": "a"}]

    @patch("app.analysis.engine._score_claimed", side_effect=RuntimeError("insert failed"))
    def test_a_failed_batch_releases_its_claims(self, _failing):
        # Otherwise the transactions would never be polled again in this process.
        with pytest.raises(RuntimeError):
            _engine_mod._score_batch("preprod", [{"tx_hash": "a"}], "live")
        assert not _engine_mod._in_flight


class TestLaneMarkers:
    """What a score says about the lane that wrote it."""

    def _scored(self, lane, history_complete=True):
        with (
            patch.multiple(
                "app.analysis.engine",
                _enrich_inputs_with_resolved_addresses=lambda rows, network: None,
                _enrich_collision_features=lambda rows, network: None,
                _enrich_cycle_features=lambda rows, network: None,
                _enrich_sandwich_features=lambda rows, network: None,
                _build_scorers=lambda: [],
            ),
            patch("app.analysis.engine.clickhouse") as mock_ch,
            patch("app.analysis.engine.notifications") as mock_notify,
        ):
            _engine_mod._score_claimed("preprod", [_make_row()], lane, history_complete)
        (results,) = mock_ch.insert_class_scores.call_args.args
        # The alert hook sees exactly what was persisted.
        assert mock_notify.on_new_scores.call_args.args[0] is results
        return results[0]

    def test_a_backlog_score_is_a_late_detection_with_its_tx_time(self):
        result = self._scored("backlog")
        assert result["late_detection"] is True
        assert result["tx_timestamp"] == _make_row()["timestamp"]

    def test_a_live_score_is_not_late(self):
        assert self._scored("live")["late_detection"] is False

    def test_a_live_score_ahead_of_the_backlog_says_its_history_is_incomplete(self):
        result = self._scored("live", history_complete=False)
        assert result["evidence"]["_meta"]["history_incomplete"] is True

    def test_a_live_score_with_the_history_complete_carries_no_marker(self):
        assert "_meta" not in self._scored("live")["evidence"]


class _EvidenceScorer:
    """Test double that gates open and returns a fixed score plus evidence."""

    def __init__(self, name, score, evidence):
        self.name = name
        self._score = score
        self._evidence = evidence

    def gate(self, features):
        return True

    def score(self, features):
        return ScorerResult(score=self._score, evidence=self._evidence)


class TestContractAddressOnScoreRow:
    """The engine normalizes the winning class's contract into its own column.

    Grouping alerts by contract reads this column, so it has to be written at
    scoring time from whichever evidence key the winning scorer used.
    """

    SCRIPT_ADDR = "addr_test1wq9contracttarget"

    def test_winning_class_target_is_persisted(self):
        row = _make_row()
        scorer = _EvidenceScorer(
            "large_datum",
            70.0,
            {"target_script_address": self.SCRIPT_ADDR},
        )
        result = _score_transaction(row, [scorer])
        assert result["max_class"] == "large_datum"
        assert result["contract_address"] == self.SCRIPT_ADDR

    def test_losing_class_target_is_not_attributed_to_the_winner(self):
        row = _make_row()
        winner = _EvidenceScorer("token_dust", 80.0, {"target_script_address": self.SCRIPT_ADDR})
        loser = _EvidenceScorer(
            "large_datum",
            20.0,
            {"target_script_address": "addr_test1wq9OTHER"},
        )
        result = _score_transaction(row, [winner, loser])
        assert result["max_class"] == "token_dust"
        assert result["contract_address"] == self.SCRIPT_ADDR

    def test_class_without_contract_identity_writes_the_sentinel(self):
        row = _make_row()
        scorer = _EvidenceScorer("phishing", 75.0, {"urls": ["http://evil.example"]})
        result = _score_transaction(row, [scorer])
        assert result["max_class"] == "phishing"
        assert result["contract_address"] == ""

    def test_no_applicable_class_writes_the_sentinel(self):
        result = _score_transaction(_make_row(), _build_scorers())
        assert result["max_class"] == ""
        assert result["contract_address"] == ""


class TestCycleEnrichment:
    """How a cycle read's outcome reaches the engine's defer-and-retry path."""

    def _enrich(self, detect):
        from app.analysis import enrichment

        row = _make_row()
        with (
            patch.object(enrichment.settings, "SCORER_CIRCULAR_ENABLED", True),
            patch.object(enrichment.settings, "CYCLE_DETECTION_ENABLED", True),
            patch("app.analysis.graph.detect_cycle", side_effect=detect),
        ):
            enrichment.enrich_cycle_features([row], "preprod")
        return row

    def test_a_failed_search_asks_for_a_retry_without_a_cycle(self):
        def detect(tx_hash, network):
            raise ConnectionError("ClickHouse went away")

        row = self._enrich(detect)
        assert "cycle" not in row
        assert row["_enrichment_failed"] == ["cycle"]

    def test_a_cycle_scored_without_its_history_is_kept_and_retried(self):
        """Kept, so a read that keeps failing still writes the cycle's score
        once the retries run out, instead of no circular finding at all."""
        cycle = {"cycle_length": 3, "history_unavailable": True}
        row = self._enrich(lambda tx_hash, network: cycle)
        assert row["cycle"] is cycle
        assert row["_enrichment_failed"] == ["cycle_history"]

    def test_a_cycle_measured_without_its_closing_leg_is_kept_and_retried(self):
        """Kept with the matched row's reading, exact for a close paying one
        origin address, so a read that keeps failing still writes the cycle's
        score once the retries run out."""
        cycle = {"cycle_length": 3, "history_unavailable": False, "closing_leg_unavailable": True}
        row = self._enrich(lambda tx_hash, network: cycle)
        assert row["cycle"] is cycle
        assert row["_enrichment_failed"] == ["cycle_closing_leg"]

    def test_a_complete_cycle_asks_for_nothing(self):
        cycle = {"cycle_length": 3, "history_unavailable": False}
        row = self._enrich(lambda tx_hash, network: cycle)
        assert row["cycle"] is cycle
        assert "_enrichment_failed" not in row


class TestHistoryRescore:
    """A ring's later passes stored before an earlier one are scored again.

    The lookup and the real re-score run against ClickHouse in the live tier
    (test_graph_cycle_live.TestLaterPassesStoredFirst); these pin what the
    engine does with the lookup's answer.
    """

    _CYCLE = {"first_slot": 1, "intermediaries": ["addr_b"], "origin_keys": ["addr_o"]}

    def _stored(self, tx_hash, circular=50.0):
        return {"tx_hash": tx_hash, "circular": circular, "evidence": {"circular": self._CYCLE}}

    def _later(self, *hashes):
        # Stored circular 10, headline 10, in chain order of the arguments.
        return {
            h: _engine_mod.graph_mod.StoredCycle(first_slot=i, circular=10.0, max_score=10.0)
            for i, h in enumerate(hashes)
        }

    def _run(self, stored, lookups, rescored, cap=None):
        calls = []

        def score_claimed(network, rows, lane, history_complete, keep=None, should_stop=None):
            calls.append([r["tx_hash"] for r in rows])
            return rescored.pop(0) if rescored else []

        with (
            patch.object(_engine_mod.graph_mod, "stored_later_cycles", side_effect=lookups) as look,
            patch.object(
                _engine_mod.clickhouse,
                "hydrate_transactions",
                side_effect=lambda network, hashes, since: [{"tx_hash": h} for h in hashes],
            ),
            patch.object(_engine_mod, "_score_claimed", side_effect=score_claimed),
            patch.object(
                _engine_mod.settings,
                "ANALYSIS_HISTORY_RESCORE_MAX_TXS",
                cap if cap is not None else _engine_mod.settings.ANALYSIS_HISTORY_RESCORE_MAX_TXS,
            ),
        ):
            _engine_mod._rescore_later_passes("preprod", stored, "live", True)
        return look, calls

    def test_only_a_rescore_that_raises_circular_and_keeps_the_headline_is_kept(self):
        before = self._later("b")
        keep = functools.partial(_engine_mod._raises_circular, before)
        assert keep({"tx_hash": "b", "circular": 30.0, "max_score": 30.0})
        assert not keep({"tx_hash": "b", "circular": 10.0, "max_score": 30.0}), "not raised"
        # Scored late, another class can read lower: never trade a detection.
        assert not keep({"tx_hash": "b", "circular": 30.0, "max_score": 5.0})

    def test_the_later_passes_are_rescored_in_chain_order(self):
        later = self._later("b", "c")
        _, calls = self._run([self._stored("a")], [later, {}], [])
        assert calls == [["b", "c"]]

    def test_a_kept_rescore_looks_up_the_passes_after_it(self):
        look, calls = self._run(
            [self._stored("a")],
            [self._later("b"), self._later("c"), {}],
            [[self._stored("b")], []],
        )
        assert calls == [["b"], ["c"]]
        assert [c.args[1][0][0] for c in look.call_args_list] == ["a", "b"]

    def test_the_cap_keeps_the_earliest_and_says_so(self, caplog):
        _, calls = self._run([self._stored("a")], [self._later("b", "c", "d")], [], cap=2)
        assert calls == [["b", "c"]]
        assert "keep their stored score" in caplog.text

    def test_a_failed_lookup_does_not_fail_the_batch(self, caplog):
        self._run([self._stored("a")], RuntimeError("CH down"), [])
        assert "re-score failed" in caplog.text

    def test_a_batch_without_cycles_looks_nothing_up(self):
        look, calls = self._run([{"tx_hash": "a", "evidence": {}}], [{}], [])
        assert not look.called and calls == []

    def test_off_at_zero(self):
        look, _ = self._run([self._stored("a")], [self._later("b")], [], cap=0)
        assert not look.called


class TestLaneStop:
    """A batch that outlives its lane's stop at shutdown stores nothing: its
    enrichments may have run after the event loop stopped, and its alerts
    could not be sent. Unstored, its rows are picked again after the restart."""

    def test_a_stopped_lanes_batch_stores_and_alerts_nothing(self):
        with (
            patch.multiple(
                "app.analysis.engine",
                _enrich_inputs_with_resolved_addresses=lambda rows, network: None,
                _enrich_collision_features=lambda rows, network: None,
                _enrich_cycle_features=lambda rows, network: None,
                _enrich_sandwich_features=lambda rows, network: None,
                _build_scorers=lambda: [],
            ),
            patch("app.analysis.engine.clickhouse") as mock_ch,
            patch("app.analysis.engine.notifications") as mock_notify,
        ):
            stored = _engine_mod._score_claimed(
                "preprod", [_make_row()], "backlog", True, should_stop=lambda: True
            )
        assert stored == []
        mock_ch.insert_class_scores.assert_not_called()
        mock_notify.on_new_scores.assert_not_called()


class TestPacedRowsLeaveTheLivePoll:
    """A deferred transaction re-polled inside its pacing window defers again
    uncounted; as the newest unscored it would be all the poll returned, so the
    drain would never reach the older rows of the window."""

    def teardown_method(self):
        _engine_mod._analysis_defer_attempts.clear()
        _engine_mod._raw_fallback_attempts.clear()

    @patch("app.analysis.engine._score_batch", return_value=0)
    @patch("app.analysis.engine.clickhouse")
    def test_the_poll_passes_over_what_waits_out_its_pacing(self, mock_ch, _scored):
        now = time.monotonic()
        overdue = now - max(settings.ANALYSIS_DEFER_RETRY_SECONDS, 1) - 1
        _engine_mod._analysis_defer_attempts[("preprod", "paced")] = (1, now)
        _engine_mod._analysis_defer_attempts[("preprod", "due")] = (1, overdue)
        _engine_mod._analysis_defer_attempts[("preview", "other")] = (1, now)
        _engine_mod._raw_fallback_attempts[("preprod", "raw")] = (1, now)
        mock_ch.get_unanalyzed_transactions.return_value = []
        with patch.object(settings, "ANALYSIS_ENABLED", True):
            _engine_mod.run_once("preprod")
        _, kwargs = mock_ch.get_unanalyzed_transactions.call_args
        assert kwargs["exclude"] == {"paced", "raw"}


class TestCollisionEnrichmentLoop:
    """front_running reads mempool collisions through the main event loop."""

    def test_a_stopped_loop_is_a_failure_not_a_skip(self, monkeypatch):
        # A batch outliving shutdown: skipped, the tx would be stored with
        # front_running as no-signal and never picked again.
        from app.analysis import enrichment

        loop = asyncio.new_event_loop()
        loop.close()
        monkeypatch.setattr(settings, "SCORER_FRONT_RUNNING_ENABLED", True)
        monkeypatch.setattr(enrichment, "_main_loop", loop)
        row = _make_row()
        enrichment.enrich_collision_features([row], "preprod")
        assert row[enrichment._ENRICHMENT_FAILED_KEY] == ["collision"]

    def test_no_loop_at_all_is_still_a_skip(self, monkeypatch):
        from app.analysis import enrichment

        monkeypatch.setattr(settings, "SCORER_FRONT_RUNNING_ENABLED", True)
        monkeypatch.setattr(enrichment, "_main_loop", None)
        row = _make_row()
        enrichment.enrich_collision_features([row], "preprod")
        assert enrichment._ENRICHMENT_FAILED_KEY not in row
