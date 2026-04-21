"""Unit tests for the Phishing scorer (Class 9)."""

import pytest
from app.analysis.scorers.phishing import PhishingScorer


@pytest.fixture
def scorer():
    return PhishingScorer()


def _features(metadata=None, addresses=None, output_count=1, raw_data=None):
    return {
        "tx_hash": "abc123",
        "network": "preprod",
        "metadata": metadata,
        "addresses": addresses or [],
        "output_count": output_count,
        "raw_data": raw_data or {},
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
        # Uses a known allowlist prefix from external.py
        addr = "addr1qx2fxv2umyhttkxyxp8x0dlpdt3k6cwng5pxj3jhsydzer_full"
        assert scorer.gate(_features(metadata=meta, addresses=[addr])) is False

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
        for key in ("blacklist", "domain_suspicion", "social_engineering",
                     "content_composite", "recipients", "delivery_composite"):
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
                        {"k": {"bytes": b"name".hex()},
                         "v": {"bytes": b"Claim ADA airdrop".hex()}},
                        {"k": {"bytes": b"url".hex()},
                         "v": {"bytes": b"https://ada-rewards.example.test/claim".hex()}},
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
        datum_obj = cbor2.CBORTag(121, [{
            b"url": b"https://cardano-phish.xyz/claim",
        }])
        datum_hex = cbor2.dumps(datum_obj).hex()
        raw_data = {
            "outputs": [
                {"address": "addr_test1w...", "datum": datum_hex},
            ]
        }
        assert scorer.gate(_features(metadata=None, raw_data=raw_data)) is True

    def test_bare_domain_requires_valid_tld(self, scorer):
        # '3.14' matches the bare-domain regex but has no real TLD; the
        # PSL-backed filter should reject it.
        meta = {"674": {"msg": ["The answer is 3.14 please"]}}
        assert scorer.gate(_features(metadata=meta)) is False

    def test_prose_without_urls_is_not_phishing(self, scorer):
        meta = {"674": {"msg": ["Thanks for the gift. See you soon."]}}
        assert scorer.gate(_features(metadata=meta)) is False
