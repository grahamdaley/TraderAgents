from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting_human"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUBMITTED = "submitted"
    FILLED = "filled"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class NodeStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class Citation(BaseModel):
    source_id: str
    label: str
    as_of: datetime | None = None
    detail: str | None = None


class FundamentalsReport(BaseModel):
    ticker: str
    revenue_trend: str
    balance_sheet_health: str
    cash_flow_quality: str
    key_ratios: dict[str, float | None] = Field(default_factory=dict)
    verdict: Literal["STRONG", "SOLID", "MIXED", "WEAK"]
    summary: str
    citations: list[Citation] = Field(default_factory=list)
    model_id: str | None = None


class TechnicalsReport(BaseModel):
    ticker: str
    trend: Literal["UP", "DOWN", "SIDEWAYS"]
    momentum: Literal["STRONG", "NEUTRAL", "WEAK"]
    volatility: Literal["LOW", "MEDIUM", "HIGH"]
    indicators: dict[str, float | None] = Field(default_factory=dict)
    verdict: Literal["BULLISH", "NEUTRAL", "BEARISH"]
    summary: str
    citations: list[Citation] = Field(default_factory=list)
    model_id: str | None = None


class SentimentReport(BaseModel):
    ticker: str
    narrative: str
    verdict: Literal[
        "STRONGLY_BULLISH",
        "BULLISH",
        "NEUTRAL",
        "BEARISH",
        "STRONGLY_BEARISH",
    ]
    bullish_catalysts: list[str] = Field(default_factory=list)
    bearish_risks: list[str] = Field(default_factory=list)
    material_risk_flags: list[str] = Field(default_factory=list)
    confidence: Literal["LOW", "MEDIUM", "HIGH"] = "MEDIUM"
    summary: str
    citations: list[Citation] = Field(default_factory=list)
    model_id: str | None = None


class MacroReport(BaseModel):
    regime: Literal["RISK_ON", "RISK_OFF", "NEUTRAL"]
    vix: float | None = None
    rates_note: str
    market_note: str
    implications: str
    summary: str
    citations: list[Citation] = Field(default_factory=list)
    model_id: str | None = None


class PmThesis(BaseModel):
    ticker: str
    side: Side
    conviction: Literal["LOW", "MEDIUM", "HIGH"]
    horizon_days: int = 30
    size_pct_nav: float = Field(ge=0.0, le=1.0)
    thesis: str
    key_risks: list[str] = Field(default_factory=list)
    disagreements: list[str] = Field(default_factory=list)
    model_id: str | None = None


class RiskNarrative(BaseModel):
    ticker: str
    narrative: str
    flags: list[str] = Field(default_factory=list)
    recommend_proceed: bool = True
    model_id: str | None = None


class PolicyCheck(BaseModel):
    name: str
    passed: bool
    detail: str


class TradeProposal(BaseModel):
    proposal_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    ticker: str
    market: Literal["us", "hk"] = "us"
    side: Side
    quantity: float
    size_pct_nav: float
    order_type: OrderType = OrderType.LIMIT
    limit_price: float | None = None
    reference_price: float
    thesis_summary: str
    citation_ids: list[str] = Field(default_factory=list)
    policy_checks: list[PolicyCheck] = Field(default_factory=list)
    model_ids: dict[str, str] = Field(default_factory=dict)
    content_hash: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    status: RunStatus = RunStatus.AWAITING_HUMAN

    def compute_hash(self) -> str:
        payload = {
            "ticker": self.ticker,
            "market": self.market,
            "side": self.side.value,
            "quantity": self.quantity,
            "size_pct_nav": self.size_pct_nav,
            "order_type": self.order_type.value,
            "limit_price": self.limit_price,
            "reference_price": self.reference_price,
            "thesis_summary": self.thesis_summary,
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def with_hash(self) -> TradeProposal:
        self.content_hash = self.compute_hash()
        return self


class ExecutionIntent(BaseModel):
    intent_id: str = Field(default_factory=lambda: str(uuid4()))
    proposal_id: str
    run_id: str
    approval_id: str
    content_hash: str
    ticker: str
    market: Literal["us", "hk"] = "us"
    side: Side
    quantity: float
    order_type: OrderType
    limit_price: float | None = None
    created_at: datetime = Field(default_factory=utcnow)
    client_order_id: str = Field(default_factory=lambda: f"td-{uuid4().hex[:16]}")


class ApprovalRequest(BaseModel):
    action: Literal["approve", "approve_with_edits", "reject", "request_research"]
    operator: str = "operator"
    quantity: float | None = None
    limit_price: float | None = None
    note: str | None = None


class AuditEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str | None = None
    event_type: str
    actor: str = "system"
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class NodeState(BaseModel):
    name: str
    status: NodeStatus = NodeStatus.QUEUED
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class PositionView(BaseModel):
    ticker: str
    quantity: float
    avg_cost: float | None = None
    market_value: float | None = None
    currency: str | None = None
    exchange: str | None = None


class BookSnapshot(BaseModel):
    account_id: str
    cash: float
    buying_power: float | None = None
    positions: list[PositionView] = Field(default_factory=list)
    open_orders: list[dict[str, Any]] = Field(default_factory=list)
    as_of: datetime = Field(default_factory=utcnow)
    source: Literal["ibkr", "mock"] = "mock"
    market: Literal["us", "hk"] = "us"
    currency: str = "USD"


class FillRecord(BaseModel):
    fill_id: str = Field(default_factory=lambda: str(uuid4()))
    intent_id: str
    broker_order_id: str
    ticker: str
    market: Literal["us", "hk"] = "us"
    side: Side
    quantity: float
    fill_price: float
    filled_at: datetime = Field(default_factory=utcnow)
    account_id: str


class SubmitResult(BaseModel):
    """Outcome of broker.submit — filled now, or resting until the session opens."""

    status: Literal["filled", "submitted"]
    broker_order_id: str
    broker_status: str
    fill: FillRecord | None = None
    message: str | None = None
