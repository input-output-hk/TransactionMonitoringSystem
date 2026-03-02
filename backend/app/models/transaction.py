"""Transaction data models"""

from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


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


class RiskLevel(str, Enum):
    """Risk classification produced by the Analysis Engine"""
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


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


class TransactionAnalysisResult(BaseModel):
    """Output record produced by the Analysis Engine for a single transaction."""
    tx_hash: str
    network: str
    risk_score: float = Field(..., ge=0.0, le=1.0, description="Composite risk score in [0, 1]")
    risk_level: RiskLevel
    cluster_id: int = Field(..., description="Deterministic address-cluster identifier (0–99)")
    is_anomaly: bool
    anomaly_reasons: List[str] = Field(default_factory=list)
    analysis_version: str
    analyzed_at: datetime


class AnalysisStats(BaseModel):
    """Aggregate statistics across all analysis results for a network."""
    total_analyzed: int = 0
    avg_risk_score: Optional[float] = None
    high_risk_count: int = 0
    anomaly_count: int = 0
    cluster_count: int = 0
    last_run_at: Optional[datetime] = None


class NormalizedTransaction(BaseModel):
    """Normalized transaction event format"""
    tx_hash: str = Field(..., description="Transaction hash")
    network: Optional[str] = Field(None, description="Network: mainnet, preprod, preview, or testnet")
    slot: Optional[int] = Field(None, description="Slot number")
    block_height: Optional[int] = Field(None, description="Block height")
    block_hash: Optional[str] = Field(None, description="Block hash")
    block_index: Optional[int] = Field(None, description="Transaction index within its block (0-based)")
    timestamp: datetime = Field(..., description="Transaction timestamp")
    fee: int = Field(..., description="Transaction fee in Lovelace")
    deposit: Optional[int] = Field(None, description="Deposit amount (positive for deposits, negative for withdrawals) in Lovelace")
    inputs: List[TransactionInput] = Field(default_factory=list)
    outputs: List[TransactionOutput] = Field(default_factory=list)
    input_count: int = Field(0, description="Number of inputs")
    output_count: int = Field(0, description="Number of outputs")
    total_input_value: Optional[int] = Field(None, description="Total input value in Lovelace; NULL when input amounts are unresolved (UTxO cache not available)")
    total_output_value: int = Field(0, description="Total output value in Lovelace")
    addresses: List[str] = Field(default_factory=list, description="All addresses involved")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Transaction metadata")
    raw_data: Optional[Dict[str, Any]] = Field(None, description="Raw transaction data for audit")
    ingestion_timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
