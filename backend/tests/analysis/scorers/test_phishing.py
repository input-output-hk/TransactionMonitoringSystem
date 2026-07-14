"""Unit tests for the Phishing scorer (Class 9)."""

import pytest
from app.analysis.scorers.phishing import PhishingScorer


@pytest.fixture
def scorer():
    return PhishingScorer()


def _features(metadata=None, addresses=None, output_count=1, raw_data=None, input_addresses=None):
    # input_addresses populate raw_data["inputs"], which is where the gate now
    # reads SENDER addresses from (the allowlist must consider senders only).
    rd = dict(raw_data or {})
    if input_addresses is not None:
        rd["inputs"] = [{"address": a} for a in input_addresses] + list(rd.get("inputs", []))
    return {
        "tx_hash": "abc123",
        "network": "preprod",
        "metadata": metadata,
        "addresses": addresses or [],
        "output_count": output_count,
        "raw_data": rd,
    }


class TestGate:
    def test_no_metadata(self, scorer):
        assert scorer.gate(_features(metadata=None)) is False

    def test_empty_metadata(self, scorer):
        assert scorer.gate(_features(metadata={})) is False

    def test_irrelevant_label(self, scorer):
        assert scorer.gate(_features(metadata={"999": "hello"})) is False

    def test_label_674_no_url(self, scorer):
        assert scorer.gate(_features(metadata={"674": "just some text"})) is False

    def test_label_674_with_url(self, scorer):
        assert scorer.gate(_features(metadata={"674": "visit https://evil.com"})) is True

    def test_label_721_with_url(self, scorer):
        meta = {"721": {"policy": {"token": {"name": "see https://scam.io"}}}}
        assert scorer.gate(_features(metadata=meta)) is True

    def test_allowlisted_sender_skipped(self, scorer):
        meta = {"674": "visit https://evil.com"}
        # A known allowlist prefix from external.py, as a RESOLVED INPUT (sender).
        addr = "addr1qx2fxv2umyhttkxyxp8x0dlpdt3k6cwng5pxj3jhsydzer_full"
        assert scorer.gate(_features(metadata=meta, input_addresses=[addr])) is False

    def test_allowlisted_recipient_does_not_suppress(self, scorer):
        """Recall: an allowlisted address as a RECIPIENT (output) must NOT
        silence detection. Previously the gate checked the merged
        input+output set, so an attacker could pay an allowlisted protocol
        address as an output and disable all phishing scoring on the tx."""
        meta = {"674": "visit https://evil.com"}
        allowlisted = "addr1qx2fxv2umyhttkxyxp8x0dlpdt3k6cwng5pxj3jhsydzer_recipient"
        attacker = "addr1qattackersender000000000000000000000000000000000"
        feats = _features(
            metadata=meta,
            addresses=[attacker, allowlisted],  # merged set incl. the recipient
            input_addresses=[attacker],  # the sender is the attacker, not allowlisted
            raw_data={"outputs": [{"address": allowlisted}]},
        )
        assert scorer.gate(feats) is True

    def test_unresolved_sender_does_not_suppress(self, scorer):
        """Recall: when inputs are unresolved (no sender address available),
        the allowlist cannot apply and detection proceeds (fail open)."""
        meta = {"674": "visit https://evil.com"}
        allowlisted = "addr1qx2fxv2umyhttkxyxp8x0dlpdt3k6cwng5pxj3jhsydzer_full"
        # Allowlisted address only in the merged set, no resolved inputs.
        assert scorer.gate(_features(metadata=meta, addresses=[allowlisted])) is True

    def test_dict_metadata_accepted(self, scorer):
        assert scorer.gate(_features(metadata={"674": "click https://phish.net"})) is True


class TestScore:
    def test_blacklist_match_scores_high(self, scorer):
        meta = {"674": "claim your ADA at https://cardano-airdrop.fake.com"}
        result = scorer.score(_features(metadata=meta))
        assert result.score > 0
        assert result.sub_scores["blacklist"] > 0

    def test_social_engineering_tier1(self, scorer):
        meta = {"674": "Enter your seed phrase at https://example.com"}
        result = scorer.score(_features(metadata=meta))
        assert result.sub_scores["social_engineering"] == 1.0
        # After the social-weight rebalance, Tier-1 hits with any domain
        # signal cross the critical_threshold and surface as
        # SUSPICIOUS_NEW_DOMAIN — a more severe label than
        # SOCIAL_ENGINEERING. KNOWN_BAD still applies if the URL matches
        # a blacklist pattern.
        assert result.severity in ("KNOWN_BAD", "SOCIAL_ENGINEERING", "SUSPICIOUS_NEW_DOMAIN")

    def test_clean_url_low_score(self, scorer):
        meta = {"674": "Check https://randomsite.org for info"}
        result = scorer.score(_features(metadata=meta))
        # No blacklist, no brand similarity, no social engineering
        assert result.score < 30

    def test_mass_distribution_boosts_delivery(self, scorer):
        meta = {"674": "visit https://cardano-giveaway.xyz"}
        low = scorer.score(_features(metadata=meta, output_count=2))
        high = scorer.score(_features(metadata=meta, output_count=200))
        assert high.score > low.score

    def test_sub_scores_present(self, scorer):
        meta = {"674": "https://example.com"}
        result = scorer.score(_features(metadata=meta))
        for key in (
            "blacklist",
            "domain_suspicion",
            "social_engineering",
            "content_composite",
            "recipients",
            "delivery_composite",
        ):
            assert key in result.sub_scores


class TestBareDomainAndDatum:
    """Regression coverage for the attacks.py phishing harness. These three
    cases slipped through the pre-extension scorer because (a) bare domains
    need no ``http://`` scheme and the old URL regex required one, and (b)
    CIP-68 delivery puts the phishing URL inside an inline datum instead of
    tx-level metadata, which the old gate ignored entirely.

    These tests assert gate-level detection (the scorer RUNS on the payload);
    final scoring depends on social-engineering pattern coverage which is
    orthogonal to the URL-extraction fix and covered by other tests."""

    def test_cip20_bare_domain_is_detected(self, scorer):
        # attacks.py build_phishing(delivery_method='cip20') case #1:
        # phishing_url='claim-reward-ada.xyz' (no scheme).
        meta = {
            "674": {
                "msg": [
                    "Reward available - verify wallet",
                    "claim-reward-ada.xyz",
                ]
            }
        }
        assert scorer.gate(_features(metadata=meta)) is True

    def test_cip20_bare_domain_with_path_is_detected(self, scorer):
        # case #3: phishing_url='cardano-drop.io/claim'.
        meta = {
            "674": {
                "msg": [
                    "Governance grant ready for claim",
                    "cardano-drop.io/claim",
                ]
            }
        }
        assert scorer.gate(_features(metadata=meta)) is True

    def test_cip20_bare_domain_plus_tier2_scores(self, scorer):
        # Same payload shape as case #3 but with a text clause that hits
        # a Tier-2 urgency pattern ("claim your"). Verifies the full chain
        # end-to-end: bare URL extracted + social-engineering pattern +
        # domain similarity produce a non-zero score.
        meta = {
            "674": {
                "msg": [
                    "Claim your governance reward now",
                    "cardano-drop.io/claim",
                ]
            }
        }
        result = scorer.score(_features(metadata=meta))
        assert result.score > 0

    def test_cip68_inline_datum_is_detected(self, scorer):
        # case #2: delivery_method='cip68'. Metadata is empty; the phishing
        # URL lives in the reference NFT's inline datum (here provided in
        # Ogmios Plutus-Data-JSON form for test clarity).
        # CIP-68 datum shape per the spec:
        # Constr 0 [map{name/image/url/description: bytes}, version_int, extra]
        cip68_datum = {
            "constructor": 0,
            "fields": [
                {
                    "map": [
                        {"k": {"bytes": b"name".hex()}, "v": {"bytes": b"Claim ADA airdrop".hex()}},
                        {
                            "k": {"bytes": b"url".hex()},
                            "v": {"bytes": b"https://ada-rewards.example.test/claim".hex()},
                        },
                    ]
                },
                {"int": 1},
                {"constructor": 0, "fields": []},
            ],
        }
        raw_data = {
            "outputs": [
                {"address": "addr_test1w...", "datum": cip68_datum},
            ]
        }
        assert scorer.gate(_features(metadata=None, raw_data=raw_data)) is True

    def test_cip68_hex_encoded_datum_is_detected(self, scorer):
        # Ogmios sometimes emits inline datums as a hex-encoded CBOR blob
        # (string) rather than the structured JSON form. The scorer must
        # handle both. Here we construct a CBOR blob manually containing
        # a bytes-string value holding an https URL.
        import cbor2

        datum_obj = cbor2.CBORTag(
            121,
            [
                {
                    b"url": b"https://cardano-phish.xyz/claim",
                }
            ],
        )
        datum_hex = cbor2.dumps(datum_obj).hex()
        raw_data = {
            "outputs": [
                {"address": "addr_test1w...", "datum": datum_hex},
            ]
        }
        assert scorer.gate(_features(metadata=None, raw_data=raw_data)) is True

    def test_cbor_tagged_map_yields_clean_leaves(self):
        # Regression: a previous byte-scan implementation concatenated
        # adjacent CBOR map values because the byte-string length-prefix
        # bytes (0x40-0x57 = ASCII A-W) fall in printable ASCII and looked
        # like part of the next text run. The classic shape this produced
        # was a CIP-25-style map ``{name, image, url}`` collapsing into a
        # single garbled string like
        # ``walletEimageTclaim-reward-ada.xyzCurlTclaim-reward-ada.xyz``,
        # which then fooled the URL regex (it caught the host but lost
        # the leading ``E``/``T``/``C`` length prefixes' boundary).
        import cbor2
        from app.analysis.scorers.phishing import _decode_datum_strings

        datum = cbor2.CBORTag(
            121,
            [
                {
                    b"name": b"wallet",
                    b"image": b"claim-reward-ada.xyz",
                    b"url": b"claim-reward-ada.xyz",
                }
            ],
        )
        leaves = _decode_datum_strings(cbor2.dumps(datum).hex())
        # Each value (and each key) comes out as a separate entry; no
        # leaf carries the adjacent value's length-prefix byte.
        assert "wallet" in leaves
        assert "claim-reward-ada.xyz" in leaves
        assert not any("Eimage" in s or "Curl" in s or "Timage" in s for s in leaves), leaves

    def test_bare_domain_requires_valid_tld(self, scorer):
        # '3.14' matches the bare-domain regex but has no real TLD; the
        # PSL-backed filter should reject it.
        meta = {"674": {"msg": ["The answer is 3.14 please"]}}
        assert scorer.gate(_features(metadata=meta)) is False

    def test_prose_without_urls_is_not_phishing(self, scorer):
        meta = {"674": {"msg": ["Thanks for the gift. See you soon."]}}
        assert scorer.gate(_features(metadata=meta)) is False


class TestAssetNameCarrier:
    """Carrier 3: phishing URLs delivered in the native-asset name itself.

    The canonical in-the-wild Cardano scam mints a token literally named
    after the phishing domain and mass-airdrops it to wallet addresses with
    NO metadata and NO datum, so the metadata and datum carriers never see
    it. These are the attack-must-fire cases for that shape, plus the
    benign-name guards.
    """

    _POLICY = "f" * 56

    def _airdrop_features(self, token_name, recipients=40, with_mint=True):
        hex_name = token_name.encode("utf-8").hex()
        outputs = [
            {
                "address": f"addr_test1qq_victim_{i:03d}",
                "value": {
                    "ada": {"lovelace": 1_200_000},
                    self._POLICY: {hex_name: 1},
                },
            }
            for i in range(recipients)
        ]
        raw = {"outputs": outputs}
        if with_mint:
            raw["mint"] = {self._POLICY: {hex_name: recipients}}
        return _features(metadata=None, raw_data=raw, output_count=recipients)

    def test_url_named_airdrop_gates_with_no_metadata(self, scorer):
        feats = self._airdrop_features("claim-ada-reward.xyz")
        assert scorer.gate(feats) is True

    def test_url_named_airdrop_scores_and_flags(self, scorer):
        result = scorer.score(self._airdrop_features("claim-ada-reward.xyz"))
        assert result.score > 0
        assert "url_in_asset_name" in result.reasons
        assert "claim-ada-reward.xyz" in result.evidence["asset_name_urls"]
        # 40 distinct recipients against the {p50: 1, p99: 50} bootstrap
        # anchor: the mass-distribution axis must be engaged.
        assert result.sub_scores["recipients"] > 0.5

    def test_outputs_only_redistribution_gates(self, scorer):
        # Re-distribution of a previously minted scam token: no mint map,
        # the name only appears in the output value bundles.
        feats = self._airdrop_features("cardano-drop.io", with_mint=False)
        assert scorer.gate(feats) is True

    def test_mint_only_gates(self, scorer):
        hex_name = "visit-ada.top".encode("utf-8").hex()
        raw = {"mint": {self._POLICY: {hex_name: 5}}}
        assert scorer.gate(_features(metadata=None, raw_data=raw)) is True

    def test_v5_value_shape_gates(self, scorer):
        # Ogmios v5 puts lovelace at the top level of the value dict.
        hex_name = "claim-ada-reward.xyz".encode("utf-8").hex()
        raw = {
            "outputs": [
                {
                    "address": "addr_test1qq_victim",
                    "value": {"lovelace": 1_200_000, self._POLICY: {hex_name: 1}},
                }
            ]
        }
        assert scorer.gate(_features(metadata=None, raw_data=raw)) is True

    def test_plain_token_name_does_not_gate(self, scorer):
        assert scorer.gate(self._airdrop_features("SUNDAE")) is False

    def test_version_like_name_does_not_gate(self, scorer):
        # "token.v1.2" matches the bare-domain regex shape but "2" is not a
        # public suffix; the PSL filter must reject it.
        assert scorer.gate(self._airdrop_features("token.v1.2")) is False

    def test_non_utf8_hex_name_does_not_gate(self, scorer):
        raw = {
            "outputs": [
                {
                    "address": "addr_test1qq_victim",
                    "value": {self._POLICY: {"ff00ff00": 1}},
                }
            ]
        }
        assert scorer.gate(_features(metadata=None, raw_data=raw)) is False


class TestDefangedUrls:
    """Defanged URLs (bracketed dots, Unicode dot lookalikes, hxxp schemes)
    must be re-fanged before extraction; otherwise a trivially obfuscated
    payload evades the gate entirely."""

    def test_bracket_dot_defang_detected(self, scorer):
        meta = {"674": {"msg": ["claim rewards at cardano-drop[.]io/claim"]}}
        assert scorer.gate(_features(metadata=meta)) is True

    def test_dot_word_defang_detected(self, scorer):
        meta = {"674": {"msg": ["visit claim-ada-reward[dot]xyz now"]}}
        assert scorer.gate(_features(metadata=meta)) is True

    def test_ideographic_dot_detected(self, scorer):
        # U+3002 IDEOGRAPHIC FULL STOP standing in for the dot.
        meta = {"674": {"msg": ["visit cardano-drop。io for rewards"]}}
        assert scorer.gate(_features(metadata=meta)) is True

    def test_hxxp_scheme_detected(self, scorer):
        meta = {"674": {"msg": ["hxxps://evil-claim.example/wallet"]}}
        assert scorer.gate(_features(metadata=meta)) is True

    def test_plain_text_still_not_gated(self, scorer):
        meta = {"674": {"msg": ["thanks for the great meetup[no urls here]"]}}
        assert scorer.gate(_features(metadata=meta)) is False


class TestDeepNestingResilience:
    """A deeply nested (attacker-controlled) datum or metadata value must not
    raise RecursionError. Unbounded recursion previously raised, the engine's
    per-scorer try/except swallowed it, and phishing was silently scored -1 --
    a recall-evasion primitive. The walks are now depth-bounded."""

    def test_flatten_to_text_deep_metadata_does_not_raise(self, scorer):
        deep = "leaf"
        for _ in range(3000):  # far beyond CPython's default recursion limit
            deep = {"m": deep}
        # Must return a string without raising, regardless of what it finds.
        assert isinstance(scorer._flatten_to_text(deep), str)

    def test_gate_on_deeply_nested_metadata_does_not_raise(self, scorer):
        deep = ["visit https://evil.example/claim"]
        for _ in range(3000):
            deep = [deep]
        # Deeply nested under a relevant label: gate must terminate (True or
        # False) rather than raising and being swallowed into a silent -1.
        result = scorer.gate(_features(metadata={"674": deep}))
        assert result in (True, False)

    def test_decode_datum_strings_deep_json_does_not_raise(self):
        from app.analysis.plutus_text import decode_datum_strings

        deep = {"bytes": "68747470733a2f2f6576696c2e78797a"}  # 'https://evil.xyz'
        for _ in range(3000):
            deep = {"list": [deep]}
        spans = decode_datum_strings(deep, min_len=4)
        assert isinstance(spans, list)  # terminated, no RecursionError

    def test_shallow_datum_url_still_decoded(self):
        from app.analysis.plutus_text import decode_datum_strings

        # 'https://cardano-airdrop.scam.example' as a hex byte-string leaf,
        # nested a few levels: still within the cap, so it must be recovered.
        clean = "https://cardano-airdrop.scam.example".encode().hex()
        node = {"fields": [{"list": [{"bytes": clean}]}]}
        spans = decode_datum_strings(node, min_len=4)
        assert any("cardano-airdrop" in s for s in spans)


class TestTextOnlyAndBytesCarriers:
    """Recall-first gate/carrier fixes: a URL-less social-engineering message
    and a URL delivered as a CBOR bytes metadatum must both be detected."""

    def test_text_only_credential_request_gates_and_scores(self, scorer):
        # No URL anywhere, just a Tier-1 credential-harvesting phrase.
        meta = {"674": {"msg": ["Please send your seed phrase to restore rewards"]}}
        feats = _features(metadata=meta)
        assert scorer.gate(feats) is True  # previously False (URL-only gate)
        result = scorer.score(feats)
        assert result.score > 0.0
        assert result.evidence["se_tier"].startswith("Tier 1")

    def test_url_as_bytes_metadatum_is_detected(self, scorer):
        # A phishing URL delivered as a CBOR bytes metadatum ({"bytes": hex}),
        # which _flatten_to_text alone leaves as un-decoded hex.
        url_hex = "https://cardano-airdrop.scam.example/claim".encode().hex()
        meta = {"674": {"bytes": url_hex}}
        feats = _features(metadata=meta)
        assert scorer.gate(feats) is True
        result = scorer.score(feats)
        assert result.score > 0.0

    def test_plain_benign_text_still_not_gated(self, scorer):
        # No URL, no SE phrasing: must stay gated out (no false positive).
        meta = {"674": {"msg": ["thanks for coming to the meetup, see you next time"]}}
        assert scorer.gate(_features(metadata=meta)) is False
