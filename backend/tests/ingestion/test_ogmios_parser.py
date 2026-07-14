"""Fixture-driven tests for the Ogmios v5/v6 transaction parser.

The parser had zero test coverage; the audit found three classes of bug
(v6 value-shape misses, phase-2 validation ignored, inflated counts) that
fixture tests would have caught. These encode the corrected semantics.

Malformed-payload coverage is split into two tiers that pin today's
behavior:

- Tolerated shapes (TestMalformedPayloads, TestIgnoredFields): the parser
  degrades to safe defaults (0 / None / empty) instead of raising.
- Hostile shapes (TestHostilePayloadsRaise): the parser DOES raise. The
  per-tx except-and-drop in ogmios_client (around the
  parse_ogmios_transaction call) is what keeps ingestion alive for these,
  so any change that removes either the raise or the catch must show up
  here first.
"""

import pytest

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

    @pytest.mark.parametrize("spends", ["inputs", "collaterals"])
    def test_input_count_matches_consumption_predicate(self, spends):
        # input_count and the enrichment's total_input_value both derive
        # from TransactionInput.consumed_by_ledger; this pins that the
        # count really is the predicate's cardinality for both outcomes.
        tx = parse_ogmios_transaction(_v6_tx(spends=spends))
        assert tx.input_count == sum(
            1 for i in tx.inputs if i.consumed_by_ledger(tx.script_valid)
        )

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
        # Regular inputs are persisted as ATTEMPTED spends (what a failed
        # attack tried to consume is signal), never as consumed flows.
        consumed = [
            i for i in tx.inputs
            if not i.is_collateral and not i.is_reference
            and not i.is_unspent_attempt
        ]
        assert consumed == []
        attempted = [i for i in tx.inputs if i.is_unspent_attempt]
        assert attempted  # every regular input persists, flagged
        # Attempted inputs come FIRST (indices aligned with raw_data["inputs"]
        # for the enrichment patcher).
        assert tx.inputs[0].is_unspent_attempt is True

    def test_failed_tx_collateral_return_on_chain_index(self):
        # Babbage: the collateral return's index is the regular-output
        # count, not its position in the parsed outputs list.
        body = _v6_tx(spends="collaterals")
        tx = parse_ogmios_transaction(body)
        assert tx.outputs[0].output_index == len(body["outputs"])

    def test_valid_tx_outputs_have_no_explicit_index(self):
        tx = parse_ogmios_transaction(_v6_tx())
        assert all(o.output_index is None for o in tx.outputs)
        assert all(not i.is_unspent_attempt for i in tx.inputs)

    def test_failed_tx_creates_collateral_return_only(self):
        tx = parse_ogmios_transaction(_v6_tx(spends="collaterals"))
        assert tx.output_count == 1
        assert tx.outputs[0].is_collateral is True
        assert tx.outputs[0].amount == 4_800_000
        assert tx.total_output_value == 4_800_000
        assert tx.addresses == ["addr_test1qqcollateralreturn"]

    def test_failed_tx_collateral_return_keeps_native_assets(self):
        # The collateralReturn is a failed tx's ONLY output; dropping its
        # native assets hid every asset a failed attack posted as
        # collateral (Ticket F).
        body = _v6_tx(
            spends="collaterals",
            collateralReturn={
                "address": "addr_test1qqcollateralreturn",
                "value": {
                    "ada": {"lovelace": 4_800_000},
                    POLICY: {"544f4b454e": 7},
                },
            },
        )
        tx = parse_ogmios_transaction(body)
        assert tx.outputs[0].amount == 4_800_000
        assert tx.outputs[0].assets == {f"{POLICY}.544f4b454e": 7}

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

    def test_v5_inline_input_reference(self):
        # v5 encodes the input's source tx as a bare string, not a dict.
        tx = parse_ogmios_transaction(
            _v6_tx(inputs=[{"transaction": "cd" * 32, "index": 3}])
        )
        assert tx.inputs[0].tx_hash == "cd" * 32
        assert tx.inputs[0].index == 3


class TestMalformedPayloads:
    """Shapes a flaky or hostile node could emit that the parser tolerates.

    Every case must produce a NormalizedTransaction with safe defaults,
    never an exception: a single bad tx must not require the client-level
    drop path when the field-level degradation can absorb it.
    """

    def test_empty_payload(self):
        tx = parse_ogmios_transaction({})
        assert tx.tx_hash == ""
        assert tx.fee == 0
        assert tx.inputs == []
        assert tx.outputs == []
        assert tx.script_valid is True
        assert tx.metadata is None

    def test_missing_id_yields_empty_hash(self):
        body = _v6_tx()
        del body["id"]
        tx = parse_ogmios_transaction(body)
        assert tx.tx_hash == ""
        # The rest of the tx still parses; only the identity is lost.
        assert tx.fee == 200_000
        assert tx.input_count == 2

    @pytest.mark.parametrize(
        "bad_fee",
        ["lots", None, {}, {"ada": {"lovelace": "abc"}}, {"ada": "abc"}],
        ids=["string", "none", "empty-dict", "non-numeric", "flat-garbage"],
    )
    def test_garbage_fee_degrades_to_zero(self, bad_fee):
        tx = parse_ogmios_transaction(_v6_tx(fee=bad_fee))
        assert tx.fee == 0

    @pytest.mark.parametrize(
        "bad_value",
        ["zilch", None, {}, {"ada": {"lovelace": "abc"}}],
        ids=["string", "none", "empty-dict", "non-numeric"],
    )
    def test_garbage_output_value_degrades_to_zero(self, bad_value):
        tx = parse_ogmios_transaction(
            _v6_tx(outputs=[{"address": "addr_test1qq", "value": bad_value}])
        )
        assert tx.output_count == 1
        assert tx.outputs[0].amount == 0
        assert tx.outputs[0].assets is None
        assert tx.total_output_value == 0

    def test_non_dict_metadata_degrades_to_none(self):
        tx = parse_ogmios_transaction(_v6_tx(metadata="not-a-dict"))
        assert tx.metadata is None

    def test_metadata_null_labels_degrades_to_none(self):
        tx = parse_ogmios_transaction(_v6_tx(metadata={"labels": None}))
        assert tx.metadata is None

    def test_metadata_without_labels_wrapper_passes_through(self):
        # v5 puts the label map at the top level with no "labels" key.
        tx = parse_ogmios_transaction(
            _v6_tx(metadata={"674": {"json": {"msg": ["x"]}}})
        )
        assert tx.metadata == {"674": {"msg": ["x"]}}

    def test_metadata_scalar_label_content_kept_raw(self):
        tx = parse_ogmios_transaction(
            _v6_tx(metadata={"labels": {"674": "raw-string"}})
        )
        assert tx.metadata == {"674": "raw-string"}

    def test_garbage_deposit_degrades_to_zero(self):
        # Sentinel contract: an unparseable deposit becomes a known 0,
        # while an absent deposit stays None (never observed).
        garbage = parse_ogmios_transaction(_v6_tx(deposit="nope"))
        body = _v6_tx()
        body.pop("deposit", None)
        absent = parse_ogmios_transaction(body)
        assert garbage.deposit == 0
        assert absent.deposit is None

    def test_unknown_spends_value_treated_as_valid(self):
        # Recall-first: an unrecognized validation marker must not divert
        # the tx onto the failed-tx path where its outputs would vanish.
        tx = parse_ogmios_transaction(_v6_tx(spends="banana"))
        assert tx.script_valid is True
        assert tx.output_count == 2

    def test_failed_tx_with_null_collateral_return(self):
        tx = parse_ogmios_transaction(
            _v6_tx(spends="collaterals", collateralReturn=None)
        )
        assert tx.script_valid is False
        assert tx.output_count == 0

    def test_input_missing_index_defaults_to_zero(self):
        tx = parse_ogmios_transaction(
            _v6_tx(inputs=[{"transaction": {"id": "cd" * 32}}])
        )
        assert tx.inputs[0].index == 0

    def test_input_missing_transaction_yields_empty_hash(self):
        tx = parse_ogmios_transaction(_v6_tx(inputs=[{"index": 1}]))
        assert tx.inputs[0].tx_hash == ""
        assert tx.inputs[0].index == 1


class TestWithdrawals:
    """Reward-account withdrawals: the stake addresses are involved
    parties and must reach the address list (Ticket F); the withdrawn
    value feeds total_input_value in the enrichment step, not here."""

    def test_v6_reward_address_recorded(self):
        tx = parse_ogmios_transaction(
            _v6_tx(withdrawals={"stake1xyz": {"ada": {"lovelace": 1_000}}})
        )
        assert "stake1xyz" in tx.addresses
        assert tx.withdrawal_total == 1_000
        # Withdrawals are input-side value: outputs are untouched.
        assert tx.total_output_value == 4_500_000

    def test_failed_tx_stamps_raw_withdrawal_total(self):
        # The parser stamps the RAW declared total; the script_valid gate
        # (a failed tx's withdrawal never applied) lives in enrichment.
        tx = parse_ogmios_transaction(
            _v6_tx(spends="collaterals",
                   withdrawals={"stake1xyz": {"ada": {"lovelace": 1_000}}})
        )
        assert tx.withdrawal_total == 1_000

    def test_v5_bare_int_shape_recorded(self):
        tx = parse_ogmios_transaction(_v6_tx(withdrawals={"stake1abc": 5_000}))
        assert "stake1abc" in tx.addresses

    def test_failed_tx_attempted_withdrawal_recorded(self):
        # The ledger never applied it, but what a failed attack TRIED to
        # withdraw is signal, like its is_unspent_attempt inputs.
        tx = parse_ogmios_transaction(
            _v6_tx(spends="collaterals",
                   withdrawals={"stake1xyz": {"ada": {"lovelace": 1_000}}})
        )
        assert tx.script_valid is False
        assert "stake1xyz" in tx.addresses

    @pytest.mark.parametrize(
        "bad", ["nope", 12, ["stake1x"], None],
        ids=["string", "int", "list", "none"],
    )
    def test_malformed_withdrawals_tolerated(self, bad):
        tx = parse_ogmios_transaction(_v6_tx(withdrawals=bad))
        assert tx.tx_hash == "ab" * 32
        assert not any(a.startswith("stake") for a in tx.addresses)


class TestIgnoredFields:
    """Fields the parser deliberately does not read must never break it.

    Mint/burn is consumed downstream from raw_data by the feature
    extractor; certificates are unparsed today. These pin that their
    presence is harmless.
    """

    def test_mint_and_burn_ignored(self):
        minted = parse_ogmios_transaction(
            _v6_tx(mint={POLICY: {"544f4b454e": 5}})
        )
        burned = parse_ogmios_transaction(
            _v6_tx(mint={POLICY: {"544f4b454e": -5}})
        )
        # Preserved verbatim in raw_data for the feature extractor.
        assert minted.raw_data["mint"] == {POLICY: {"544f4b454e": 5}}
        assert burned.raw_data["mint"] == {POLICY: {"544f4b454e": -5}}

    def test_certificates_ignored(self):
        tx = parse_ogmios_transaction(
            _v6_tx(certificates=[{"type": "stakeDelegation"}])
        )
        assert tx.tx_hash == "ab" * 32

    def test_unknown_keys_ignored(self):
        tx = parse_ogmios_transaction(
            _v6_tx(votingProcedures={"x": 1}, proposals=[{"y": 2}])
        )
        assert tx.tx_hash == "ab" * 32


class TestHostilePayloadsRaise:
    """Shapes the parser does NOT absorb: it raises, and the per-tx
    except-and-drop in ogmios_client is the safety net. If one of these
    starts passing, the parser grew tolerance; move the case up to
    TestMalformedPayloads rather than deleting it.
    """

    @pytest.mark.parametrize(
        "bad_id", [{"weird": 1}, 12345], ids=["dict", "int"]
    )
    def test_non_string_id_raises(self, bad_id):
        with pytest.raises(Exception):
            parse_ogmios_transaction(_v6_tx(id=bad_id))

    @pytest.mark.parametrize(
        "field", ["inputs", "outputs", "references", "collaterals"]
    )
    def test_explicit_null_collection_raises(self, field):
        # JSON null (as opposed to an absent key) defeats the .get(...)
        # defaults and the parser iterates None.
        with pytest.raises(TypeError):
            parse_ogmios_transaction(_v6_tx(**{field: None}))

    def test_null_input_entry_fields_raise(self):
        with pytest.raises(Exception):
            parse_ogmios_transaction(
                _v6_tx(inputs=[{"transaction": {"id": None}, "index": None}])
            )

    def test_non_dict_output_entry_raises(self):
        with pytest.raises(AttributeError):
            parse_ogmios_transaction(_v6_tx(outputs=["garbage"]))
