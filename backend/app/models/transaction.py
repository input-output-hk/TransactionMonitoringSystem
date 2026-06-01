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
    LOW = "Low"             # 0-30: no action, baseline calibration
    MODERATE = "Moderate"   # 31-59: flagged for periodic review
    HIGH = "High"           # 60-79: queued for analyst review
    CRITICAL = "Critical"   # 80-100: immediate alert


class AttackClass(str, Enum):
    """The nine attack classes defined by the Polimi detection spec."""
    TOKEN_DUST = "token_dust"
    LARGE_VALUE = "large_value"
    LARGE_DATUM = "large_datum"
    MULTIPLE_SAT = "multiple_sat"
    FRONT_RUNNING = "front_running"
    SANDWICH = "sandwich"
    CIRCULAR = "circular"
    FAKE_TOKEN = "fake_token"
    PHISHING = "phishing"


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


class TransactionOutput(BaseModel):
    """Transaction output (new UTxO)"""
    address: str
    amount: int
    assets: Optional[Dict[str, int]] = None
    is_collateral: bool = Field(default=False, description="True if this is a collateral return output")



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
    risk_band: RiskBand = RiskBand.LOW
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
    total_input_value: Optional[int] = Field(None, description="Total input value in lovelace; NULL when input amounts are unresolved (UTxO cache not available)")
    total_output_value: int = Field(0, description="Total output value in lovelace")
    addresses: List[str] = Field(default_factory=list, description="All addresses involved")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Transaction metadata")
    raw_data: Optional[Dict[str, Any]] = Field(None, description="Raw transaction data for audit")
    ingestion_timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
