"""Unit tests for the Phishing scorer (Class 9)."""

import pytest
from app.analysis.scorers.phishing import PhishingScorer


@pytest.fixture
def scorer():
    return PhishingScorer()


def _features(metadata=None, addresses=None, output_count=1):
    return {
        "tx_hash": "abc123",
        "network": "preprod",
        "metadata": metadata,
        "addresses": addresses or [],
        "output_count": output_count,
        "raw_data": {},
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
        assert result.severity == "KNOWN_BAD" or result.severity == "SOCIAL_ENGINEERING"

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
