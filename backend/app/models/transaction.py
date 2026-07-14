"""Transaction data models"""

from datetime import datetime, timezone
from enum import Enum
from typing import List, Literal, Optional, Dict, Any
from pydantic import BaseModel, Field


# Cardano networks the TMS understands. Enforced at the API boundary via
# FastAPI's Query type validation. To add a new network, extend this tuple
# and update `settings.CARDANO_NETWORK`'s docstring + .env.example.
NetworkType = Literal["mainnet", "preprod", "preview"]


class LifecycleStatus(str, Enum):
    """Transaction lifecycle states.

    PENDING      — seen in mempool; not yet included in any block.
    CONFIRMED    — included in a block at a specific slot.
    ROLLED_BACK  — was CONFIRMED, but Ogmios reported rollBackward to a point whose
                   slot is less than the slot at which this transaction was confirmed.
                   All transactions confirmed at slots strictly greater than the
                   rollback target slot are marked ROLLED_BACK in a single UPDATE.
                   The transaction may re-appear in the mempool and be re-confirmed
                   at a later block; if so, the row returns to CONFIRMED.
    DROPPED      — was PENDING but not confirmed within LIFECYCLE_PENDING_TTL_SECONDS.
                   Assigned by a background cleanup sweep, not by a real-time eviction
                   event (Ogmios LocalTxMonitor does not emit eviction notifications).
                   DROPPED does not mean the transaction is invalid — it may have been
                   resubmitted, confirmed on a fork that was not observed, or simply
                   delayed beyond the monitoring window.
    """
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    ROLLED_BACK = "ROLLED_BACK"
    DROPPED = "DROPPED"


class RiskBand(str, Enum):
    """Interpretive score bands from Polimi scoring framework.

    Scores are continuous 0-100; bands guide analyst workflow and alerting.
    """
    INFORMATIONAL = "Informational"  # 0-30: no action, scored-but-not-alerting baseline
    MODERATE = "Moderate"            # 31-59: flagged for periodic review
    HIGH = "High"                    # 60-79: queued for analyst review
    CRITICAL = "Critical"            # 80-100: immediate alert

    @classmethod
    def _missing_(cls, value: object) -> "RiskBand | None":
        """Map the pre-2026-06 label "Low" onto INFORMATIONAL.

        The 0-30 band was renamed "Low" -> "Informational"; rows scored before
        the migration (and any in-flight during it) still carry "Low". Parsing
        them here keeps ``RiskBand(stored_value)`` from raising on un-migrated
        rows, so the rename is safe regardless of deploy/migration ordering.
        Remove once all stored ``risk_band`` values are migrated.
        """
        if isinstance(value, str) and value.lower() == "low":
            return cls.INFORMATIONAL
        return None


class AttackClass(str, Enum):
    """The nine attack classes defined by the Polimi detection spec, plus the
    read-time-only ``contract_anomaly`` class.

    The first nine are produced by the in-process per-transaction scorers and
    written to ``tx_class_scores``. ``contract_anomaly`` is NOT one of them: it
    is the verdict of the optional clustering sidecar, stored in
    ``tms_clustering.tx_contract_anomaly`` and merged into the score vector at
    API read time (see ``db/clustering_queries.py`` and the analysis router).
    It is deliberately absent from the per-tx write path so the host scoring
    engine can never write or clobber it.
    """
    TOKEN_DUST = "token_dust"
    LARGE_VALUE = "large_value"
    LARGE_DATUM = "large_datum"
    MULTIPLE_SAT = "multiple_sat"
    FRONT_RUNNING = "front_running"
    SANDWICH = "sandwich"
    CIRCULAR = "circular"
    FAKE_TOKEN = "fake_token"
    PHISHING = "phishing"
    CONTRACT_ANOMALY = "contract_anomaly"


class TransactionLifecycleEvent(BaseModel):
    """Transaction lifecycle state"""
    tx_id: str
    network: str = "preprod"
    status: LifecycleStatus
    first_seen_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None
    rolled_back_at: Optional[datetime] = None
    dropped_at: Optional[datetime] = None
    block_hash: Optional[str] = None
    slot: Optional[int] = None
    height: Optional[int] = None
    latency_ms: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class LifecycleSummaryStats(BaseModel):
    """Aggregate lifecycle statistics"""
    total_tracked: int = 0
    pending_count: int = 0
    confirmed_count: int = 0
    rolled_back_count: int = 0
    dropped_count: int = 0
    avg_latency_ms: Optional[float] = None
    rollback_rate: Optional[float] = None


class TransactionInput(BaseModel):
    """Transaction input (consumed UTxO)"""
    tx_hash: str
    index: int
    address: str
    amount: int
    assets: Optional[Dict[str, int]] = None
    is_reference: bool = Field(default=False, description="True if this is a reference input (read-only)")
    is_collateral: bool = Field(default=False, description="True if this is a collateral input")
    is_unspent_attempt: bool = Field(
        default=False,
        description=(
            "Regular input of a phase-2-failed tx: referenced but NOT "
            "consumed on-chain (Babbage; the collaterals carried the "
            "consumption). Excluded from flow/displacement reads."
        ),
    )

    def consumed_by_ledger(self, script_valid: bool) -> bool:
        """Whether the ledger actually consumed this input's value:
        regular inputs for a validated tx, collateral inputs for a failed
        one. Reference inputs are read-only, and a failed tx's regular
        inputs (is_unspent_attempt) stayed live.

        Single source of the consumption rule: the parser's input_count
        and the enrichment's total_input_value both derive from this
        predicate so the two can never silently disagree.
        """
        if self.is_reference:
            return False
        if script_valid:
            return not self.is_collateral and not self.is_unspent_attempt
        return self.is_collateral


class TransactionOutput(BaseModel):
    """Transaction output (new UTxO)"""
    address: str
    amount: int
    assets: Optional[Dict[str, int]] = None
    is_collateral: bool = Field(default=False, description="True if this is a collateral return output")
    output_index: Optional[int] = Field(
        default=None,
        description=(
            "Explicit on-chain output index; None = position in the "
            "outputs list. Set for collateral returns, whose on-chain "
            "index is the regular-output count (Babbage), not 0."
        ),
    )



class ClassScoreResult(BaseModel):
    """Multi-class scoring output produced by the Analysis Engine.

    Each transaction receives an independent 0-100 score for every applicable
    attack class.  A score of -1 means the class was not applicable (gate
    condition failed).
    """
    tx_hash: str
    network: str
    scores: Dict[str, float] = Field(
        default_factory=dict,
        description="Attack class name -> score (0-100, or -1 if not applicable)",
    )
    max_score: float = Field(0.0, description="Highest score across all classes")
    max_class: str = Field("", description="Attack class with the highest score")
    risk_band: RiskBand = RiskBand.INFORMATIONAL
    sub_scores: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="Per-class sub-score breakdown for drill-down",
    )
    evidence: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="Per-class raw evidence (addresses, byte counts, lists) for UI drill-down",
    )
    analysis_version: str = ""
    analyzed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    corroboration_count: int = Field(
        0,
        description=(
            "Number of distinct attack classes that independently scored at or "
            "above the corroboration threshold. A flag for analyst triage only; "
            "does not affect risk_band."
        ),
    )
    corroborating_classes: str = Field(
        "",
        description="Comma-separated names of the corroborating classes.",
    )
    contract_anomaly_corroborates: bool = Field(
        False,
        description=(
            "True when the clustering sidecar's contract_anomaly score (if "
            "present) is at or above the corroboration threshold. Surfaced "
            "separately from corroboration_count, which is the stored, "
            "server-side-filterable count over the nine per-tx classes and is "
            "never mutated by the read-time merge."
        ),
    )
    contract_anomaly_scored_at: Optional[datetime] = Field(
        None,
        description=(
            "When the clustering sidecar last scored this transaction. Lets "
            "the UI mark a contract_anomaly verdict as stale if the sidecar is "
            "down. Absent when no contract_anomaly verdict was merged."
        ),
    )
    fee: Optional[int] = Field(None, description="Transaction fee in lovelace")
    output_count: Optional[int] = Field(None, description="Number of transaction outputs")
    archived: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Present when an admin has archived this transaction as a known "
            "false positive. Contains note, archived_by, archived_at, source. "
            "Absent otherwise."
        ),
    )


class NormalizedTransaction(BaseModel):
    """Normalized transaction event format"""
    tx_hash: str = Field(..., description="Transaction hash")
    network: Optional[str] = Field(None, description="Network: mainnet, preprod, preview, or testnet")
    slot: Optional[int] = Field(None, description="Slot number")
    block_height: Optional[int] = Field(None, description="Block height")
    block_hash: Optional[str] = Field(None, description="Block hash")
    block_index: Optional[int] = Field(None, description="Transaction index within its block (0-based)")
    timestamp: datetime = Field(..., description="Transaction timestamp")
    fee: int = Field(..., description="Transaction fee in lovelace")
    deposit: Optional[int] = Field(None, description="Deposit amount (positive for deposits, negative for withdrawals) in lovelace")
    inputs: List[TransactionInput] = Field(default_factory=list)
    outputs: List[TransactionOutput] = Field(default_factory=list)
    input_count: int = Field(0, description="Number of inputs")
    output_count: int = Field(0, description="Number of outputs")
    total_input_value: Optional[int] = Field(
        None,
        description=(
            "Consumed value in lovelace resolved so far: regular inputs "
            "(validated txs) or collateral inputs (failed txs), plus "
            "reward-account withdrawals. NULL when nothing is resolved "
            "and no withdrawal applies; a partial LOWER BOUND when only "
            "some inputs (or only the withdrawal) resolved."
        ),
    )
    withdrawal_total: int = Field(
        0,
        description=(
            "Sum of the tx's reward-account withdrawals in lovelace, "
            "stamped by the parser from the raw payload. Transient (no "
            "ClickHouse column): the enrichment folds it into "
            "total_input_value for validated txs."
        ),
    )
    total_output_value: int = Field(0, description="Total output value in lovelace")
    addresses: List[str] = Field(default_factory=list, description="All addresses involved")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Transaction metadata")
    script_valid: bool = Field(
        True,
        description=(
            "Phase-2 validation outcome (Ogmios v6 'spends' marker). False "
            "means a Plutus script failed: the ledger consumed the collateral "
            "inputs and created only the collateralReturn output."
        ),
    )
    raw_data: Optional[Dict[str, Any]] = Field(None, description="Raw transaction data for audit")
    ingestion_timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
