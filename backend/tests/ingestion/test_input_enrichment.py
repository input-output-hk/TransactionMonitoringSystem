"""Value-flow semantics of the input enrichment (Ticket F).

Two consumed-value classes were invisible before these changes: reward-
account withdrawals (never read at all) and a failed tx's collateral
inputs (skipped by both resolution paths, so the only value a failed
attack actually moved never resolved). These pin the rules:

- total_input_value counts what the ledger CONSUMED: regular inputs plus
  withdrawals for a validated tx, collateral inputs for a failed one.
- Resolution is broader than consumption: attempted inputs and collateral
  resolve for address visibility, with flags telling readers what to
  exclude.
"""

from unittest.mock import AsyncMock, patch

from app.analysis.features import total_withdrawal_lovelace
from app.ingestion.input_enrichment import (
    apply_resolved_inputs,
    resolve_input_amounts,
)
from app.ingestion.ogmios_parser import parse_ogmios_transaction
from tests.ingestion.conftest import run_async as _run

SOURCE_TX = "11" * 32
COLLATERAL_TX = "44" * 32
REFERENCE_TX = "33" * 32
STAKE = "stake1qqrewards"


def _tx(spends="inputs", **overrides):
    """Parse a minimal tx so raw_data (which the withdrawal fold reads)
    stays consistent with the model fields."""
    body = {
        "id": "ab" * 32,
        "spends": spends,
        "fee": {"ada": {"lovelace": 200_000}},
        "inputs": [{"transaction": {"id": SOURCE_TX}, "index": 0}],
        "references": [{"transaction": {"id": REFERENCE_TX}, "index": 0}],
        "collaterals": [{"transaction": {"id": COLLATERAL_TX}, "index": 0}],
        "outputs": [
            {"address": "addr_test1qqout", "value": {"ada": {"lovelace": 1_000_000}}}
        ],
    }
    body.update(overrides)
    return parse_ogmios_transaction(body)


def _resolve(txs, resolved_refs):
    """Run resolve_input_amounts with a mocked ClickHouse ref lookup."""
    with patch(
        "app.ingestion.input_enrichment.clickhouse.get_outputs_for_refs_async",
        AsyncMock(return_value=resolved_refs),
    ):
        return _run(resolve_input_amounts(txs, "preprod"))


class TestTotalWithdrawalLovelace:
    def test_v6_nested_shape(self):
        raw = {"withdrawals": {STAKE: {"ada": {"lovelace": 1_500}}}}
        assert total_withdrawal_lovelace(raw) == 1_500

    def test_v5_bare_int_shape(self):
        assert total_withdrawal_lovelace({"withdrawals": {STAKE: 2_000}}) == 2_000

    def test_multiple_accounts_sum(self):
        raw = {"withdrawals": {STAKE: 1_000, "stake1other": 250}}
        assert total_withdrawal_lovelace(raw) == 1_250

    def test_absent_and_malformed_degrade_to_zero(self):
        assert total_withdrawal_lovelace({}) == 0
        assert total_withdrawal_lovelace(None) == 0
        assert total_withdrawal_lovelace({"withdrawals": "nope"}) == 0
        assert total_withdrawal_lovelace({"withdrawals": {STAKE: "garbage"}}) == 0


class TestWithdrawalValueFlow:
    WITHDRAWALS = {STAKE: {"ada": {"lovelace": 3_000_000}}}

    def test_apply_folds_withdrawal_into_total(self):
        tx = _tx(withdrawals=self.WITHDRAWALS)
        resolved = {(SOURCE_TX, 0): {"address": "addr_test1qqsrc", "amount": 5_000_000}}
        out = apply_resolved_inputs(tx, resolved)
        assert out.total_input_value == 8_000_000

    def test_apply_ignores_failed_tx_withdrawal(self):
        # Phase-2 failure: the ledger never applied the withdrawal.
        tx = _tx(spends="collaterals", withdrawals=self.WITHDRAWALS)
        out = apply_resolved_inputs(tx, {})
        assert out.total_input_value is None

    def test_resolve_withdrawal_only_sets_total(self):
        # No input resolves, but the withdrawal is consumed value the tx
        # provably moved: the total must not stay NULL.
        tx = _tx(withdrawals=self.WITHDRAWALS)
        out = _resolve([tx], {})[0]
        assert out.total_input_value == 3_000_000

    def test_resolve_sums_inputs_and_withdrawal(self):
        tx = _tx(withdrawals=self.WITHDRAWALS)
        out = _resolve([tx], {(SOURCE_TX, 0): ("addr_test1qqsrc", 5_000_000)})[0]
        assert out.total_input_value == 8_000_000

    def test_no_withdrawal_no_resolution_leaves_tx_untouched(self):
        tx = _tx()
        out = _resolve([tx], {})[0]
        assert out.total_input_value is None
        assert out is tx  # not even copied


class TestCollateralResolution:
    def test_failed_tx_collateral_feeds_total_and_addresses(self):
        # The collateral is exactly what the ledger consumed for a failed
        # tx; before Ticket F it never resolved, so a failed attack's only
        # real value flow was invisible.
        tx = _tx(spends="collaterals")
        out = _resolve(
            [tx],
            {
                (COLLATERAL_TX, 0): ("addr_test1qqpayer", 5_000_000),
                (SOURCE_TX, 0): ("addr_test1qqattempted", 9_000_000),
            },
        )[0]
        collateral = next(i for i in out.inputs if i.is_collateral)
        assert collateral.address == "addr_test1qqpayer"
        assert collateral.amount == 5_000_000
        # Consumed = collateral only; the attempted input resolves for
        # address visibility but was never consumed.
        assert out.total_input_value == 5_000_000
        assert "addr_test1qqpayer" in out.addresses
        assert "addr_test1qqattempted" in out.addresses

    def test_valid_tx_collateral_resolves_but_never_counts(self):
        # When a valid tx's collateral IS resolvable (e.g. its source sits
        # in the same block), it applies for display behind the flag but
        # must never feed the consumed total or the address list.
        tx = _tx()
        out = _resolve(
            [tx],
            {
                (COLLATERAL_TX, 0): ("addr_test1qqpayer", 5_000_000),
                (SOURCE_TX, 0): ("addr_test1qqsrc", 2_000_000),
            },
        )[0]
        collateral = next(i for i in out.inputs if i.is_collateral)
        assert collateral.amount == 5_000_000  # visible behind the flag
        assert out.total_input_value == 2_000_000  # regular input only
        # Not consumed, so not an involved-party address on a valid tx.
        assert "addr_test1qqpayer" not in out.addresses

    def test_valid_tx_collateral_not_fetched_cross_block(self):
        # A validated tx's collateral feeds neither totals, addresses, nor
        # any detection query, so it must not grow the checkpoint-blocking
        # per-block ClickHouse lookup; a FAILED tx's collateral is the
        # consumed flow and must be fetched.
        requested = {}

        async def capture(refs, network):
            requested["refs"] = list(refs)
            return {}

        with patch(
            "app.ingestion.input_enrichment.clickhouse.get_outputs_for_refs_async",
            AsyncMock(side_effect=capture),
        ):
            _run(resolve_input_amounts([_tx()], "preprod"))
        assert (COLLATERAL_TX, 0) not in requested["refs"]
        assert (SOURCE_TX, 0) in requested["refs"]

        with patch(
            "app.ingestion.input_enrichment.clickhouse.get_outputs_for_refs_async",
            AsyncMock(side_effect=capture),
        ):
            _run(resolve_input_amounts([_tx(spends="collaterals")], "preprod"))
        assert (COLLATERAL_TX, 0) in requested["refs"]

    def test_reference_inputs_never_resolve(self):
        tx = _tx()
        out = _resolve([tx], {(REFERENCE_TX, 0): ("addr_test1qqref", 1_000_000)})[0]
        ref = next(i for i in out.inputs if i.is_reference)
        assert ref.address == ""
        assert ref.amount == 0

    def test_apply_counts_cached_collateral_for_failed_tx(self):
        tx = _tx(spends="collaterals")
        resolved = {(COLLATERAL_TX, 0): {"address": "addr_test1qqpayer", "amount": 4_000_000}}
        out = apply_resolved_inputs(tx, resolved)
        assert out.total_input_value == 4_000_000
        collateral = next(i for i in out.inputs if i.is_collateral)
        assert collateral.is_collateral is True
        assert collateral.amount == 4_000_000
