"""Contract lock for multiple_sat's lenient payment-credential decoder.

These tests freeze the CURRENT observable behavior of ``_payment_credential``
before it is consolidated with the sidecar's validating bech32 decoder
(services/clustering/backend/app/registry/bech32.py). The contract is
deliberately LENIENT and recall-load-bearing:

- the 6-char bech32 checksum is stripped WITHOUT validation, so a
  checksum-corrupted (attacker-mangled) address still yields the same
  grouping key as its well-formed sibling;
- any structural failure returns the RAW address, so grouping degrades to
  per-address rather than dropping the transaction.

A consolidation that silently switched to strict checksum validation would
change grouping keys for malformed addresses: a recall change, forbidden
without its own gated decision. Every vector below must keep passing
byte-for-byte across the swap.

Vector provenance: PAYMENT_CRED / STAKE variants were computed with the
sidecar's validating decoder as the oracle against a real preprod base
address; the sibling re-encodes the same payment credential with a
different (byte-reversed) stake credential and a valid checksum.
"""

from app.analysis.scorers.multiple_sat import (
    _is_decoded_payment_credential,
    _payment_credential,
)

# A real preprod base address (payment cred + stake cred, header 0x00).
ADDR = "addr_test1qr7zdn98e39kwqev8rkk545zxlxqtwnnuvwnw5vcllsw0atvg5rgp8086x0tndsejz8zqf68r6tla9fmxg62ga39s4sqjv80h4"
# Its 28-byte (56-hex) payment credential, per the validating oracle.
PAYMENT_CRED = "fc26cca7cc4b67032c38ed6a568237cc05ba73e31d375198ffe0e7f5"
# Same payment credential, different stake credential, valid checksum.
SIBLING = "addr_test1qr7zdn98e39kwqev8rkk545zxlxqtwnnuvwnw5vcllsw0atqs5jhdfp5xgaetl5hrerjwgywjqvmdwv768nemqqxg4kqmlc4tr"
# ADDR with its final checksum character flipped: strictly INVALID bech32.
FLIPPED_CHECKSUM = ADDR[:-1] + "k"


class TestDecodeContract:
    def test_valid_address_yields_payment_credential(self):
        assert _payment_credential(ADDR) == PAYMENT_CRED

    def test_same_payment_cred_across_stake_creds_groups_together(self):
        # The reason the decoder exists: one script, many stake creds, one key.
        assert _payment_credential(SIBLING) == PAYMENT_CRED
        assert SIBLING != ADDR

    def test_flipped_checksum_still_yields_same_key(self):
        # THE LENIENT CONTRACT. The sidecar's strict decoder returns None for
        # this input; the scorer's decoder must keep returning the credential
        # so a mangled-but-decodable address cannot escape grouping.
        assert _payment_credential(FLIPPED_CHECKSUM) == PAYMENT_CRED

    def test_uppercase_data_part_decodes(self):
        prefix, data = ADDR.rsplit("1", 1)
        assert _payment_credential(prefix + "1" + data.upper()) == PAYMENT_CRED


class TestFallbackContract:
    def test_invalid_charset_returns_raw_input(self):
        # 'b', 'i', 'o' and '1' are outside the bech32 charset.
        bad = ADDR[:-8] + "bio1" + ADDR[-4:]
        assert _payment_credential(bad) == bad

    def test_too_short_data_part_returns_raw_input(self):
        # After the 6-char checksum strip, fewer than 8+224 bits remain.
        short = "addr_test1" + ADDR.rsplit("1", 1)[1][:40]
        assert _payment_credential(short) == short

    def test_empty_and_no_separator_return_input(self):
        assert _payment_credential("") == ""
        assert _payment_credential("notanaddress") == "notanaddress"

    def test_checksum_only_data_part_returns_raw_input(self):
        assert _payment_credential("addr1qqqqqq") == "addr1qqqqqq"


class TestKeyClassifier:
    def test_decoded_credential_is_recognized(self):
        assert _is_decoded_payment_credential(_payment_credential(ADDR))

    def test_raw_fallback_is_not_recognized(self):
        bad = ADDR[:-8] + "bio1" + ADDR[-4:]
        assert not _is_decoded_payment_credential(_payment_credential(bad))


def test_large_datum_imports_the_same_callable():
    # large_datum groups by the identical function; consolidation must keep
    # this import path resolving to the same object.
    from app.analysis.scorers import large_datum

    assert large_datum._payment_credential is _payment_credential
