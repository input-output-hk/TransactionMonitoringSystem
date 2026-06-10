"""Fixture-driven tests for the Ogmios v5/v6 transaction parser.

The parser had zero test coverage; the audit found three classes of bug
(v6 value-shape misses, phase-2 validation ignored, inflated counts) that
fixture tests would have caught. These encode the corrected semantics.
"""

from app.ingestion.ogmios_parser import parse_ogmios_transaction

POLICY = "f" * 56


def _v6_tx(**overrides):
    """A representative Ogmios v6 (Conway/Babbage) block transaction."""
    tx = {
        "id": "ab" * 32,
        "spends": "inputs",
        "fee": {"ada": {"lovelace": 200_000}},
        "inputs": [
            {"transaction": {"id": "11" * 32}, "index": 0},
            {"transaction": {"id": "22" * 32}, "index": 1},
        ],
        "references": [
            {"transaction": {"id": "33" * 32}, "index": 0},
        ],
        "collaterals": [
            {"transaction": {"id": "44" * 32}, "index": 0},
        ],
        "collateralReturn": {
            "address": "addr_test1qqcollateralreturn",
            "value": {"ada": {"lovelace": 4_800_000}},
        },
        "outputs": [
            {
                "address": "addr_test1qqrecipient",
                "value": {
                    "ada": {"lovelace": 1_500_000},
                    POLICY: {"544f4b454e": 42},
                },
            },
            {
                "address": "addr_test1qqchange",
                "value": {"ada": {"lovelace": 3_000_000}},
            },
        ],
        "metadata": {"labels": {"674": {"json": {"msg": ["hello"]}}}},
    }
    tx.update(overrides)
    return tx


class TestValidV6Transaction:
    def test_value_parsing(self):
        tx = parse_ogmios_transaction(_v6_tx())
        assert tx.fee == 200_000
        assert tx.total_output_value == 4_500_000
        assert tx.outputs[0].amount == 1_500_000
        assert tx.outputs[0].assets == {f"{POLICY}.544f4b454e": 42}
        assert tx.outputs[1].assets is None

    def test_consumed_counts_exclude_reference_and_collateral(self):
        tx = parse_ogmios_transaction(_v6_tx())
        # 2 spending inputs; the reference and collateral rows are recorded
        # with their flags but are NOT consumed and must not inflate counts.
        assert tx.input_count == 2
        assert sum(1 for i in tx.inputs if i.is_reference) == 1
        assert sum(1 for i in tx.inputs if i.is_collateral) == 1
        assert len(tx.inputs) == 4

    def test_collateral_return_not_created_when_valid(self):
        # A validated tx never creates the collateralReturn output.
        tx = parse_ogmios_transaction(_v6_tx())
        assert tx.output_count == 2
        assert all(not o.is_collateral for o in tx.outputs)
        assert tx.script_valid is True

    def test_metadata_labels(self):
        tx = parse_ogmios_transaction(_v6_tx())
        assert tx.metadata == {"674": {"msg": ["hello"]}}

    def test_spends_absent_means_valid(self):
        body = _v6_tx()
        del body["spends"]
        tx = parse_ogmios_transaction(body)
        assert tx.script_valid is True
        assert tx.output_count == 2


class TestFailedV6Transaction:
    """spends == "collaterals": the ledger consumed the collateral and
    created only the collateralReturn; regular inputs stayed live and
    regular outputs never existed on-chain."""

    def test_failed_tx_consumes_collateral_only(self):
        tx = parse_ogmios_transaction(_v6_tx(spends="collaterals"))
        assert tx.script_valid is False
        assert tx.input_count == 1
        # Regular (unspent) inputs are omitted from the consumed list.
        non_flagged = [i for i in tx.inputs if not i.is_collateral and not i.is_reference]
        assert non_flagged == []

    def test_failed_tx_creates_collateral_return_only(self):
        tx = parse_ogmios_transaction(_v6_tx(spends="collaterals"))
        assert tx.output_count == 1
        assert tx.outputs[0].is_collateral is True
        assert tx.outputs[0].amount == 4_800_000
        assert tx.total_output_value == 4_800_000
        assert tx.addresses == ["addr_test1qqcollateralreturn"]

    def test_failed_tx_without_collateral_return(self):
        body = _v6_tx(spends="collaterals")
        del body["collateralReturn"]
        tx = parse_ogmios_transaction(body)
        assert tx.output_count == 0
        assert tx.total_output_value == 0


class TestV5Shapes:
    def test_v5_fee_and_value(self):
        body = _v6_tx(
            fee={"lovelace": 170_000},
            outputs=[
                {
                    "address": "addr_test1qqrecipient",
                    "value": {"lovelace": 2_000_000, POLICY: {"544f4b454e": 1}},
                }
            ],
        )
        tx = parse_ogmios_transaction(body)
        assert tx.fee == 170_000
        assert tx.outputs[0].amount == 2_000_000
        assert tx.outputs[0].assets == {f"{POLICY}.544f4b454e": 1}

    def test_deposit_shapes(self):
        v5 = parse_ogmios_transaction(_v6_tx(deposit={"lovelace": 2_000_000}))
        v6 = parse_ogmios_transaction(_v6_tx(deposit={"ada": {"lovelace": 2_000_000}}))
        assert v5.deposit == 2_000_000
        assert v6.deposit == 2_000_000
