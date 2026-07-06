"""Shared helpers for the scorer test suite.

Only the genuinely common factory lives here: the minimal feature dict
for scorers that read ``raw_data["outputs"]`` (token_dust, large_value,
large_datum). The other scorers keep their local ``_features`` because
each one feeds scorer-specific keys (collision, cycle, sandwich,
redeemers, ...) and merging those into one kitchen-sink factory would
hide which inputs a scorer actually consumes.
"""


def features_for_outputs(outputs, tx_hash="scorer-test"):
    """Feature dict for a single-tx scorer driven by its outputs list."""
    return {
        "tx_hash": tx_hash,
        "network": "preprod",
        "raw_data": {"outputs": outputs},
    }
