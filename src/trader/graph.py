from __future__ import annotations

import json
import logging
import operator
import threading
from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from trader.broker import get_broker
from trader.config import get_settings
from trader.nous import get_chat_model
from trader.policy import PolicyRejection, build_trade_proposal
from trader.store import list_open_proposal_tickers_sync
from trader.schemas import (
    FundamentalsReport,
    MacroReport,
    NodeState,
    NodeStatus,
    PmThesis,
    RiskNarrative,
    RunStatus,
    SentimentReport,
    Side,
    TechnicalsReport,
    TradeProposal,
)
from trader.tools.market_data import get_market_data_tools

logger = logging.getLogger(__name__)


def _merge_dicts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left or {})
    merged.update(right or {})
    return merged


class ICState(TypedDict, total=False):
    run_id: str
    ticker: str
    nodes: Annotated[dict[str, Any], _merge_dicts]
    fundamentals_report: dict[str, Any] | None
    technicals_report: dict[str, Any] | None
    sentiment_report: dict[str, Any] | None
    macro_report: dict[str, Any] | None
    pm_thesis: dict[str, Any] | None
    risk_narrative: dict[str, Any] | None
    trade_proposal: dict[str, Any] | None
    policy_rejection: str | None
    human_decision: dict[str, Any] | None
    fill: dict[str, Any] | None
    execution_intent: dict[str, Any] | None
    model_ids: Annotated[dict[str, str], _merge_dicts]
    citation_ids: Annotated[list[str], operator.add]
    error: str | None
    status: str


def _node_update(
    name: str,
    status: NodeStatus,
    *,
    previous: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Return a single-node delta so parallel writers can merge safely."""
    now = datetime.now(timezone.utc).isoformat()
    node = dict(previous or NodeState(name=name).model_dump(mode="json"))
    node["name"] = name
    node["status"] = status.value
    if status == NodeStatus.RUNNING:
        node["started_at"] = now
    if status in {NodeStatus.DONE, NodeStatus.FAILED, NodeStatus.SKIPPED}:
        node["finished_at"] = now
    if error:
        node["error"] = error
    return {name: node}


def _mark(
    nodes: dict[str, Any], name: str, status: NodeStatus, error: str | None = None
) -> dict[str, Any]:
    previous = (nodes or {}).get(name)
    return _node_update(name, status, previous=previous, error=error)


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def _invoke_structured(role: str, system: str, user: str, model_cls: type):
    settings = get_settings()
    model_ids = settings.model_map()
    model_id = model_ids[role]

    if settings.allow_mock_llm or not settings.nous_api_key:
        return _mock_structured(role, user, model_cls), model_id

    llm = get_chat_model(role)
    messages = [SystemMessage(content=system), HumanMessage(content=user)]
    last_error: Exception | None = None
    for _ in range(2):
        try:
            response = llm.invoke(messages)
            payload = _extract_json(str(response.content))
            return model_cls.model_validate(payload), model_id
        except (ValidationError, json.JSONDecodeError, TypeError, ValueError) as exc:
            last_error = exc
            messages.append(
                HumanMessage(
                    content=(
                        "Your previous answer was invalid JSON for the schema. "
                        f"Error: {exc}. Reply with ONLY valid JSON."
                    )
                )
            )
    raise RuntimeError(f"{role} failed structured parse: {last_error}")


def _mock_structured(role: str, user: str, model_cls: type):
    ticker = "TICK"
    for token in user.replace(",", " ").split():
        if token.isupper() and 1 < len(token) <= 5:
            ticker = token
            break
    if model_cls is FundamentalsReport:
        return FundamentalsReport(
            ticker=ticker,
            revenue_trend="stable",
            balance_sheet_health="adequate",
            cash_flow_quality="solid",
            key_ratios={"trailing_pe": 20.0},
            verdict="SOLID",
            summary=f"Mock fundamental view for {ticker}",
        )
    if model_cls is TechnicalsReport:
        return TechnicalsReport(
            ticker=ticker,
            trend="UP",
            momentum="NEUTRAL",
            volatility="MEDIUM",
            indicators={},
            verdict="BULLISH",
            summary=f"Mock technical view for {ticker}",
        )
    if model_cls is SentimentReport:
        return SentimentReport(
            ticker=ticker,
            narrative="Mixed coverage",
            verdict="NEUTRAL",
            summary=f"Mock sentiment for {ticker}",
        )
    if model_cls is MacroReport:
        return MacroReport(
            regime="NEUTRAL",
            vix=18.0,
            rates_note="Rates steady",
            market_note="Equity tape mixed",
            implications="Neutral for risk assets",
            summary="Mock macro",
        )
    if model_cls is PmThesis:
        return PmThesis(
            ticker=ticker,
            side=Side.BUY,
            conviction="MEDIUM",
            size_pct_nav=0.03,
            thesis=f"Mock thesis: accumulate {ticker}",
            key_risks=["macro"],
            disagreements=[],
        )
    if model_cls is RiskNarrative:
        return RiskNarrative(
            ticker=ticker,
            narrative="Risks acceptable for a starter size",
            flags=[],
            recommend_proceed=True,
        )
    raise TypeError(model_cls)


def fundamental_node(state: ICState) -> dict[str, Any]:
    nodes = _mark(state.get("nodes", {}), "fundamental", NodeStatus.RUNNING)
    tools = get_market_data_tools()
    data = tools.get_fundamentals(state["ticker"])
    citation_ids = []
    if data.get("citation"):
        citation_ids.append(data["citation"]["source_id"])
    report, model_id = _invoke_structured(
        "fundamental",
        (
            "You are a fundamental equity analyst. Use ONLY the provided tool JSON. "
            "Return JSON matching FundamentalsReport fields: ticker, revenue_trend, "
            "balance_sheet_health, cash_flow_quality, key_ratios, verdict "
            "(STRONG|SOLID|MIXED|WEAK), summary. No prose outside JSON."
        ),
        json.dumps({"ticker": state["ticker"], "data": data}, default=str),
        FundamentalsReport,
    )
    report.model_id = model_id
    report.key_ratios = {
        **{k: v for k, v in (data.get("ratios") or {}).items() if v is not None},
        **report.key_ratios,
    }
    nodes = _mark(nodes, "fundamental", NodeStatus.DONE)
    return {
        "nodes": nodes,
        "fundamentals_report": report.model_dump(mode="json"),
        "model_ids": {"fundamental": model_id},
        "citation_ids": citation_ids,
    }


def technical_node(state: ICState) -> dict[str, Any]:
    nodes = _mark(state.get("nodes", {}), "technical", NodeStatus.RUNNING)
    tools = get_market_data_tools()
    data = tools.compute_indicators(state["ticker"])
    citation_ids = []
    if data.get("citation"):
        citation_ids.append(data["citation"]["source_id"])
    report, model_id = _invoke_structured(
        "technical",
        (
            "You are a technical analyst. Use ONLY the provided indicators. "
            "Return JSON for TechnicalsReport: ticker, trend (UP|DOWN|SIDEWAYS), "
            "momentum (STRONG|NEUTRAL|WEAK), volatility (LOW|MEDIUM|HIGH), "
            "indicators, verdict (BULLISH|NEUTRAL|BEARISH), summary."
        ),
        json.dumps({"ticker": state["ticker"], "data": data}, default=str),
        TechnicalsReport,
    )
    report.model_id = model_id
    report.indicators = {**(data.get("indicators") or {}), **report.indicators}
    nodes = _mark(nodes, "technical", NodeStatus.DONE)
    return {
        "nodes": nodes,
        "technicals_report": report.model_dump(mode="json"),
        "model_ids": {"technical": model_id},
        "citation_ids": citation_ids,
    }


def sentiment_node(state: ICState) -> dict[str, Any]:
    nodes = _mark(state.get("nodes", {}), "sentiment", NodeStatus.RUNNING)
    tools = get_market_data_tools()
    data = tools.get_news(state["ticker"])
    citation_ids = []
    if data.get("citation"):
        citation_ids.append(data["citation"]["source_id"])
    report, model_id = _invoke_structured(
        "sentiment",
        (
            "You are a market sentiment analyst. Use ONLY the provided headlines. "
            "Return JSON for SentimentReport with verdict one of "
            "STRONGLY_BULLISH|BULLISH|NEUTRAL|BEARISH|STRONGLY_BEARISH."
        ),
        json.dumps({"ticker": state["ticker"], "data": data}, default=str),
        SentimentReport,
    )
    report.model_id = model_id
    nodes = _mark(nodes, "sentiment", NodeStatus.DONE)
    return {
        "nodes": nodes,
        "sentiment_report": report.model_dump(mode="json"),
        "model_ids": {"sentiment": model_id},
        "citation_ids": citation_ids,
    }


def macro_node(state: ICState) -> dict[str, Any]:
    nodes = _mark(state.get("nodes", {}), "macro", NodeStatus.RUNNING)
    tools = get_market_data_tools()
    data = tools.get_macro_snapshot()
    citation_ids = []
    if data.get("citation"):
        citation_ids.append(data["citation"]["source_id"])
    report, model_id = _invoke_structured(
        "macro",
        (
            "You are a macro strategist. Use ONLY the provided snapshot. "
            "Return JSON for MacroReport with regime RISK_ON|RISK_OFF|NEUTRAL."
        ),
        json.dumps({"ticker": state["ticker"], "data": data}, default=str),
        MacroReport,
    )
    report.model_id = model_id
    report.vix = data.get("vix")
    nodes = _mark(nodes, "macro", NodeStatus.DONE)
    return {
        "nodes": nodes,
        "macro_report": report.model_dump(mode="json"),
        "model_ids": {"macro": model_id},
        "citation_ids": citation_ids,
    }


def pm_node(state: ICState) -> dict[str, Any]:
    nodes = _mark(state.get("nodes", {}), "portfolio_manager", NodeStatus.RUNNING)
    packet = {
        "ticker": state["ticker"],
        "fundamentals": state.get("fundamentals_report"),
        "technicals": state.get("technicals_report"),
        "sentiment": state.get("sentiment_report"),
        "macro": state.get("macro_report"),
    }
    thesis, model_id = _invoke_structured(
        "portfolio_manager",
        (
            "You are the portfolio manager. Synthesize the four specialist reports. "
            "Return JSON for PmThesis: ticker, side (BUY|SELL|HOLD), conviction "
            "(LOW|MEDIUM|HIGH), horizon_days, size_pct_nav (0-1, suggest <=0.1), "
            "thesis, key_risks, disagreements. Long/flat only — SELL means flatten."
        ),
        json.dumps(packet, default=str),
        PmThesis,
    )
    thesis.model_id = model_id
    thesis.ticker = state["ticker"].upper()
    nodes = _mark(nodes, "portfolio_manager", NodeStatus.DONE)
    return {
        "nodes": nodes,
        "pm_thesis": thesis.model_dump(mode="json"),
        "model_ids": {"portfolio_manager": model_id},
    }


def risk_node(state: ICState) -> dict[str, Any]:
    nodes = _mark(state.get("nodes", {}), "risk", NodeStatus.RUNNING)
    packet = {
        "ticker": state["ticker"],
        "pm_thesis": state.get("pm_thesis"),
        "fundamentals": state.get("fundamentals_report"),
        "sentiment": state.get("sentiment_report"),
        "macro": state.get("macro_report"),
    }
    narrative, model_id = _invoke_structured(
        "risk",
        (
            "You are the risk officer writing a qualitative narrative. "
            "Return JSON for RiskNarrative: ticker, narrative, flags, "
            "recommend_proceed (bool). Hard limits are enforced elsewhere in code."
        ),
        json.dumps(packet, default=str),
        RiskNarrative,
    )
    narrative.model_id = model_id
    nodes = _mark(nodes, "risk", NodeStatus.DONE)
    return {
        "nodes": nodes,
        "risk_narrative": narrative.model_dump(mode="json"),
        "model_ids": {"risk": model_id},
    }


def policy_node(state: ICState) -> dict[str, Any]:
    nodes = _mark(state.get("nodes", {}), "policy", NodeStatus.RUNNING)
    thesis = PmThesis.model_validate(state["pm_thesis"])
    risk = RiskNarrative.model_validate(state["risk_narrative"])
    broker = get_broker()
    book = broker.get_book()
    try:
        reference_price = broker.get_quote(state["ticker"])
    except Exception:  # noqa: BLE001
        indicators = get_market_data_tools().compute_indicators(state["ticker"])
        reference_price = float(
            (indicators.get("indicators") or {}).get("last_close") or 0
        )

    try:
        proposal = build_trade_proposal(
            run_id=state["run_id"],
            thesis=thesis,
            risk=risk,
            book=book,
            reference_price=reference_price,
            model_ids=state.get("model_ids") or {},
            citation_ids=list(dict.fromkeys(state.get("citation_ids") or [])),
            open_proposal_tickers=list_open_proposal_tickers_sync(),
        )
        nodes = _mark(nodes, "policy", NodeStatus.DONE)
        return {
            "nodes": nodes,
            "trade_proposal": proposal.model_dump(mode="json"),
            "policy_rejection": None,
            "status": RunStatus.AWAITING_HUMAN.value,
        }
    except PolicyRejection as exc:
        nodes = _mark(nodes, "policy", NodeStatus.DONE, error=exc.reason)
        return {
            "nodes": nodes,
            "trade_proposal": None,
            "policy_rejection": exc.reason,
            "status": RunStatus.REJECTED.value,
        }


def human_gate_node(state: ICState) -> dict[str, Any]:
    if state.get("policy_rejection") or not state.get("trade_proposal"):
        return {"status": RunStatus.REJECTED.value}

    nodes = _mark(state.get("nodes", {}), "human_gate", NodeStatus.RUNNING)
    proposal = state["trade_proposal"]
    decision = interrupt(
        {
            "type": "trade_proposal",
            "proposal": proposal,
            "message": "Approve, edit, or reject this trade proposal",
        }
    )
    nodes = _mark(nodes, "human_gate", NodeStatus.DONE)
    return {
        "nodes": nodes,
        "human_decision": decision,
        "status": RunStatus.APPROVED.value
        if (decision or {}).get("action", "").startswith("approve")
        else RunStatus.REJECTED.value,
    }


def execute_node(state: ICState) -> dict[str, Any]:
    decision = state.get("human_decision") or {}
    action = decision.get("action")
    if action not in {"approve", "approve_with_edits"}:
        return {
            "status": RunStatus.REJECTED.value,
            "nodes": _mark(state.get("nodes", {}), "execute", NodeStatus.SKIPPED),
        }

    nodes = _mark(state.get("nodes", {}), "execute", NodeStatus.RUNNING)
    proposal = TradeProposal.model_validate(state["trade_proposal"])
    if action == "approve_with_edits":
        if decision.get("quantity") is not None:
            proposal.quantity = float(decision["quantity"])
        if decision.get("limit_price") is not None:
            proposal.limit_price = float(decision["limit_price"])
        proposal = proposal.with_hash()

    from trader.schemas import ExecutionIntent

    intent = ExecutionIntent(
        proposal_id=proposal.proposal_id,
        run_id=state["run_id"],
        approval_id=decision.get("approval_id") or str(uuid4()),
        content_hash=proposal.content_hash,
        ticker=proposal.ticker,
        side=proposal.side,
        quantity=proposal.quantity,
        order_type=proposal.order_type,
        limit_price=proposal.limit_price,
    )
    try:
        fill = get_broker().submit(
            proposal=proposal,
            intent=intent,
            content_hash=proposal.content_hash,
        )
        nodes = _mark(nodes, "execute", NodeStatus.DONE)
        return {
            "nodes": nodes,
            "trade_proposal": proposal.model_dump(mode="json"),
            "execution_intent": intent.model_dump(mode="json"),
            "fill": fill.model_dump(mode="json"),
            "status": RunStatus.FILLED.value,
        }
    except Exception as exc:  # noqa: BLE001
        nodes = _mark(nodes, "execute", NodeStatus.FAILED, error=str(exc))
        return {
            "nodes": nodes,
            "error": str(exc),
            "status": RunStatus.FAILED.value,
        }


def route_after_policy(state: ICState) -> str:
    if state.get("trade_proposal"):
        return "human_gate"
    return END


def route_after_human(state: ICState) -> str:
    decision = state.get("human_decision") or {}
    if decision.get("action") in {"approve", "approve_with_edits"}:
        return "execute"
    return END


def build_graph(checkpointer=None):
    graph = StateGraph(ICState)
    graph.add_node("fundamental", fundamental_node)
    graph.add_node("technical", technical_node)
    graph.add_node("sentiment", sentiment_node)
    graph.add_node("macro", macro_node)
    # defer=True so the PM waits for all four specialist branches
    graph.add_node("portfolio_manager", pm_node, defer=True)
    graph.add_node("risk", risk_node)
    graph.add_node("policy", policy_node)
    graph.add_node("human_gate", human_gate_node)
    graph.add_node("execute", execute_node)

    graph.add_edge(START, "fundamental")
    graph.add_edge(START, "technical")
    graph.add_edge(START, "sentiment")
    graph.add_edge(START, "macro")
    graph.add_edge("fundamental", "portfolio_manager")
    graph.add_edge("technical", "portfolio_manager")
    graph.add_edge("sentiment", "portfolio_manager")
    graph.add_edge("macro", "portfolio_manager")
    graph.add_edge("portfolio_manager", "risk")
    graph.add_edge("risk", "policy")
    graph.add_conditional_edges("policy", route_after_policy, ["human_gate", END])
    graph.add_conditional_edges("human_gate", route_after_human, ["execute", END])
    graph.add_edge("execute", END)

    return graph.compile(checkpointer=checkpointer or MemorySaver())


_compiled = None
_pg_pool = None
_graph_lock = threading.RLock()


def get_compiled_graph():
    global _compiled, _pg_pool
    if _compiled is not None:
        return _compiled

    settings = get_settings()
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg_pool import ConnectionPool

        _pg_pool = ConnectionPool(
            conninfo=settings.database_url_sync,
            min_size=1,
            max_size=10,
            timeout=10,
            kwargs={"autocommit": True, "prepare_threshold": 0},
        )
        checkpointer = PostgresSaver(_pg_pool)
        checkpointer.setup()
        _compiled = build_graph(checkpointer)
        logger.info("Using Postgres checkpointer")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Postgres checkpointer unavailable (%s); using memory", exc)
        _compiled = build_graph(MemorySaver())
    return _compiled


def invoke_graph_locked(graph, payload, config) -> tuple[dict[str, Any], Any, Exception | None]:
    """Serialize invoke + get_state so concurrent runs cannot interleave the singleton graph."""
    with _graph_lock:
        interrupt_exc: Exception | None = None
        result: dict[str, Any] = {}
        try:
            result = graph.invoke(payload, config=config)
        except GraphInterrupt as exc:
            interrupt_exc = exc
            result = {}
        snapshot = graph.get_state(config)
        return result, snapshot, interrupt_exc


def initial_state(run_id: str, ticker: str) -> ICState:
    node_names = [
        "fundamental",
        "technical",
        "sentiment",
        "macro",
        "portfolio_manager",
        "risk",
        "policy",
        "human_gate",
        "execute",
    ]
    return {
        "run_id": run_id,
        "ticker": ticker.upper(),
        "nodes": {
            name: NodeState(name=name).model_dump(mode="json") for name in node_names
        },
        "fundamentals_report": None,
        "technicals_report": None,
        "sentiment_report": None,
        "macro_report": None,
        "pm_thesis": None,
        "risk_narrative": None,
        "trade_proposal": None,
        "policy_rejection": None,
        "human_decision": None,
        "fill": None,
        "execution_intent": None,
        "model_ids": {},
        "citation_ids": [],
        "error": None,
        "status": RunStatus.RUNNING.value,
    }
