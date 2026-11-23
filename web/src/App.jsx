import React, { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Link, Navigate, Route, Routes, useParams } from "react-router-dom";
import { MarketProvider, useMarket, withMarketQuery } from "./market.jsx";

const TABS = ["Runs", "Scan", "Inbox", "Book", "Audit"];

function sectorLabel(code, labels) {
  if (!code) return "";
  const map = labels || {};
  return map[String(code).toUpperCase()] || String(code);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const text = await res.text();
  let data;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { detail: text };
  }
  if (!res.ok) {
    throw new Error(data?.detail || res.statusText);
  }
  return data;
}

function Badge({ status }) {
  return <span className={`badge ${status || ""}`}>{status || "—"}</span>;
}

function Hint({ hint, children, className = "" }) {
  const [pos, setPos] = useState(null);

  if (!hint) {
    return className ? <span className={className}>{children}</span> : children;
  }

  function show(event) {
    const rect = event.currentTarget.getBoundingClientRect();
    const pad = 8;
    const approxWidth = 280;
    const left = Math.min(
      Math.max(pad, Math.round(rect.left)),
      Math.max(pad, window.innerWidth - approxWidth - pad)
    );
    const top = Math.round(rect.bottom + 6);
    setPos({ left, top });
  }

  function hide() {
    setPos(null);
  }

  return (
    <span
      className={`hint ${className}`.trim()}
      data-hint={hint}
      tabIndex={0}
      onMouseEnter={show}
      onMouseLeave={hide}
      onFocus={show}
      onBlur={hide}
    >
      {children}
      {pos
        ? createPortal(
            <span
              className="hint-bubble"
              role="tooltip"
              style={{
                position: "fixed",
                left: `${pos.left}px`,
                top: `${pos.top}px`,
                zIndex: 1000,
              }}
            >
              {hint}
            </span>,
            document.body
          )
        : null}
    </span>
  );
}

function explain(error) {
  const message = String(error?.message || error || "");
  if (message === "Failed to fetch") {
    return "The API is not reachable.";
  }
  return message;
}

function RunsPanel({ onSelectRun }) {
  const market = useMarket();
  const [ticker, setTicker] = useState(market.defaultTicker);
  const [runs, setRuns] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState(null);

  useEffect(() => {
    setTicker(market.defaultTicker);
    setSelected(null);
  }, [market.id, market.defaultTicker]);

  const refresh = useCallback(async () => {
    const data = await api(withMarketQuery("/api/runs", market.id));
    setRuns(data);
    setError("");
  }, [market.id]);

  useEffect(() => {
    refresh().catch((e) => setError(explain(e)));
    const id = setInterval(() => {
      refresh().catch((e) => setError(explain(e)));
    }, 4000);
    return () => clearInterval(id);
  }, [refresh]);

  async function startRun() {
    setBusy(true);
    setError("");
    try {
      const result = await api("/api/runs", {
        method: "POST",
        body: JSON.stringify({ ticker, market: market.id }),
      });
      await refresh();
      const detail = await api(`/api/runs/${result.run_id}`);
      setSelected(detail);
      onSelectRun?.(detail);
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  async function openRun(id) {
    setError("");
    try {
      const detail = await api(`/api/runs/${id}`);
      setSelected(detail);
      onSelectRun?.(detail);
    } catch (e) {
      setError(explain(e));
    }
  }

  const nodes = selected?.nodes || {};

  return (
    <div className="grid-2">
      <div>
        <div className="panel">
          <div className="row">
            <input
              type="text"
              value={ticker}
              onChange={(e) => setTicker(e.target.value.toUpperCase())}
              placeholder="Ticker"
            />
            <button className="btn" disabled={busy} onClick={startRun}>
              {busy ? "Running…" : "Start research"}
            </button>
            <button className="btn secondary" onClick={() => refresh()}>
              Refresh
            </button>
          </div>
          {error ? <div className="error">{error}</div> : null}
        </div>

        <div className="panel">
          <table className="table">
            <thead>
              <tr>
                <th>Ticker</th>
                <th>Status</th>
                <th>Updated</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id}>
                  <td>
                    <Hint hint={r.name}>{r.ticker}</Hint>
                  </td>
                  <td>
                    <Badge status={r.status} />
                  </td>
                  <td className="muted">
                    {r.updated_at ? new Date(r.updated_at).toLocaleString() : "—"}
                  </td>
                  <td>
                    <button className="btn secondary" onClick={() => openRun(r.id)}>
                      Open
                    </button>
                  </td>
                </tr>
              ))}
              {!runs.length ? (
                <tr>
                  <td colSpan={4} className="muted">
                    No runs yet.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Run detail</h3>
        {!selected ? (
          <p className="muted">Select a run to inspect node status and reports.</p>
        ) : (
          <>
            <p>
              <Hint hint={selected.name}>
                <strong>{selected.ticker}</strong>
              </Hint>{" "}
              · <Badge status={selected.status} />
            </p>
            {selected.error ? <div className="error">{selected.error}</div> : null}
            <div className="nodes" style={{ marginBottom: 12 }}>
              {Object.values(nodes).map((n) => (
                <div className="node" key={n.name}>
                  <div className="name">{n.name}</div>
                  <div className="status">{n.status}</div>
                </div>
              ))}
            </div>
            <div className="pre">{JSON.stringify(selected.state || {}, null, 2)}</div>
          </>
        )}
      </div>
    </div>
  );
}

function ScanPanel({ onStarted }) {
  const market = useMarket();
  const [scan, setScan] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState("");

  const refresh = useCallback(async () => {
    setScan(await api(withMarketQuery("/api/scans/latest", market.id)));
  }, [market.id]);

  useEffect(() => {
    refresh().catch((e) => setError(String(e.message || e)));
  }, [refresh]);

  async function runScan() {
    setBusy(true);
    setError("");
    try {
      setScan(
        await api("/api/scans", {
          method: "POST",
          body: JSON.stringify({ market: market.id }),
        })
      );
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  async function research(ticker) {
    setStarting(ticker);
    setError("");
    try {
      await api("/api/runs", {
        method: "POST",
        body: JSON.stringify({ ticker, market: market.id }),
      });
      onStarted?.();
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setStarting("");
    }
  }

  const candidates = scan?.candidates || [];

  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 12 }}>
        <div>
          <h3 style={{ margin: 0 }}>Scan</h3>
          <p className="muted" style={{ margin: "4px 0 0" }}>
            {scan?.id
              ? `${scan.regime || "—"} · ${scan.universe_count || 0} names · ${
                  scan.created_at ? new Date(scan.created_at).toLocaleString() : ""
                }`
              : "No scan yet."}
            {scan?.hot_sectors?.length ? ` · leading ${scan.hot_sectors.join(", ")}` : ""}
          </p>
        </div>
        <button className="btn" disabled={busy} onClick={runScan}>
          {busy ? "Scanning…" : "Run scan"}
        </button>
      </div>
      {error ? <div className="error">{error}</div> : null}
      <table className="table">
        <thead>
          <tr>
            <th>#</th>
            <th>Ticker</th>
            <th>Sector</th>
            <th>Score</th>
            <th>Signals</th>
            <th>Why</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {candidates.map((row) => (
            <tr key={`${row.ticker}-${row.rank}`}>
              <td>{row.rank}</td>
              <td>
                <Hint hint={row.name}>{row.ticker}</Hint>
              </td>
              <td className="muted">
                <Hint hint={sectorLabel(row.sector, market.sectorLabels)}>
                  {row.sector}
                </Hint>
              </td>
              <td>{row.score}</td>
              <td className="muted">{(row.signals || []).join(", ")}</td>
              <td>{row.reason}</td>
              <td>
                <button
                  className="btn secondary"
                  disabled={starting === row.ticker}
                  onClick={() => research(row.ticker)}
                >
                  {starting === row.ticker ? "Starting…" : "Research"}
                </button>
              </td>
            </tr>
          ))}
          {!candidates.length ? (
            <tr>
              <td colSpan={7} className="muted">
                Run a scan to rank the universe. Research sends one name to the committee.
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>
    </div>
  );
}

function InboxPanel() {
  const market = useMarket();
  const [items, setItems] = useState([]);
  const [selected, setSelected] = useState(null);
  const [qty, setQty] = useState("");
  const [limit, setLimit] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    const data = await api(withMarketQuery("/api/proposals", market.id));
    setItems(data);
  }, [market.id]);

  useEffect(() => {
    refresh().catch((e) => setError(String(e.message || e)));
    const id = setInterval(() => refresh().catch(() => {}), 4000);
    return () => clearInterval(id);
  }, [refresh]);

  function choose(item) {
    setSelected(item);
    setQty(String(item.payload?.quantity ?? ""));
    setLimit(String(item.payload?.limit_price ?? ""));
    setError("");
    setNotice("");
  }

  async function decide(action) {
    if (!selected) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const body = {
        action,
        operator: "operator",
        note: null,
      };
      if (action === "approve_with_edits") {
        body.quantity = qty === "" ? null : Number(qty);
        body.limit_price = limit === "" ? null : Number(limit);
      }
      const result = await api(`/api/runs/${selected.run_id}/decision`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      const status = result?.status;
      const note =
        result?.state?.submit_result?.message ||
        result?.state?.error ||
        null;
      setSelected(null);
      await refresh();
      if (status === "failed") {
        setError(note || "Order failed — see Runs / Audit for details.");
      } else if (status === "submitted") {
        setNotice(
          note ||
            `Limit order submitted to IBKR and will work when the ${market.sessionName} is open.`
        );
      } else if (status === "filled") {
        setNotice("Order filled.");
      }
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  const p = selected?.payload;

  return (
    <div className="grid-2">
      <div className="panel">
        <div className="row" style={{ marginBottom: 12 }}>
          <button className="btn secondary" onClick={() => refresh()}>
            Refresh inbox
          </button>
        </div>
        <table className="table">
          <thead>
            <tr>
              <th>Ticker</th>
              <th>Side</th>
              <th>Qty</th>
              <th>Expires</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.id}>
                <td>{item.ticker}</td>
                <td>{item.payload?.side}</td>
                <td>{item.payload?.quantity}</td>
                <td className="muted">
                  {item.expires_at ? new Date(item.expires_at).toLocaleString() : "—"}
                </td>
                <td>
                  <button className="btn secondary" onClick={() => choose(item)}>
                    Review
                  </button>
                </td>
              </tr>
            ))}
            {!items.length ? (
              <tr>
                <td colSpan={5} className="muted">
                  No proposals awaiting approval.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Proposal</h3>
        {!selected ? (
          <p className="muted">Pick a proposal to approve, edit, or reject.</p>
        ) : (
          <>
            <p>
              <strong>{p?.ticker}</strong> {p?.side} {p?.quantity} @ limit {p?.limit_price}
            </p>
            <p className="muted">Ref price {p?.reference_price} · hash {selected.content_hash?.slice(0, 12)}…</p>
            <p>{p?.thesis_summary}</p>
            <div className="pre" style={{ maxHeight: 220 }}>
              {JSON.stringify(p?.policy_checks || [], null, 2)}
            </div>
            <div className="row" style={{ marginTop: 12 }}>
              <label className="muted">
                Qty
                <input type="number" value={qty} onChange={(e) => setQty(e.target.value)} />
              </label>
              <label className="muted">
                Limit
                <input type="number" value={limit} onChange={(e) => setLimit(e.target.value)} />
              </label>
            </div>
            <div className="row" style={{ marginTop: 12 }}>
              <button className="btn good" disabled={busy} onClick={() => decide("approve")}>
                Approve
              </button>
              <button
                className="btn warn"
                disabled={busy}
                onClick={() => decide("approve_with_edits")}
              >
                Approve with edits
              </button>
              <button className="btn bad" disabled={busy} onClick={() => decide("reject")}>
                Reject
              </button>
              <button
                className="btn secondary"
                disabled={busy}
                onClick={() => decide("request_research")}
              >
                Request research
              </button>
            </div>
            {error ? <div className="error">{error}</div> : null}
            {notice ? <p className="muted">{notice}</p> : null}
          </>
        )}
      </div>
      {!selected && notice ? <p className="muted">{notice}</p> : null}
      {!selected && error ? <div className="error">{error}</div> : null}
    </div>
  );
}

function BookPanel() {
  const market = useMarket();
  const [book, setBook] = useState(null);
  const [fills, setFills] = useState([]);
  const [plans, setPlans] = useState({ plans: [], unplanned: [] });
  const [error, setError] = useState("");
  const [reviewNote, setReviewNote] = useState("");
  const [reviewing, setReviewing] = useState(false);
  const [reconciling, setReconciling] = useState(false);
  const [reconcileNote, setReconcileNote] = useState("");

  const refresh = useCallback(async () => {
    const [b, f, p] = await Promise.all([
      api(withMarketQuery("/api/book", market.id)),
      api(withMarketQuery("/api/fills", market.id)),
      api(withMarketQuery("/api/position-plans", market.id)),
    ]);
    setBook(b);
    setFills(f);
    setPlans(p);
  }, [market.id]);

  useEffect(() => {
    refresh().catch((e) => setError(String(e.message || e)));
    const id = setInterval(() => refresh().catch(() => {}), 5000);
    return () => clearInterval(id);
  }, [refresh]);

  async function reviewExits() {
    setReviewing(true);
    setReviewNote("");
    setError("");
    try {
      const result = await api(
        withMarketQuery("/api/exits/review", market.id),
        { method: "POST" }
      );
      const count = (result.actions || []).length;
      setReviewNote(
        result.skipped
          ? "Review skipped."
          : `Reviewed ${count} plan${count === 1 ? "" : "s"}.`
      );
      await refresh();
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setReviewing(false);
    }
  }

  async function reconcileFills() {
    setReconciling(true);
    setReconcileNote("");
    setError("");
    try {
      const result = await api("/api/fills/reconcile", { method: "POST" });
      const actions = result.actions || [];
      const filled = actions.filter((a) => a.action === "filled").length;
      const working = actions.filter((a) => a.action === "working" || a.action === "pending").length;
      const cancelled = actions.filter((a) => a.action === "cancelled").length;
      setReconcileNote(
        result.error
          ? `Reconcile error: ${result.error}`
          : `Checked ${result.checked || 0}: ${filled} filled, ${working} still working, ${cancelled} cancelled.`
      );
      await refresh();
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setReconciling(false);
    }
  }

  return (
    <div>
      <div className="panel">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div>
            <h3 style={{ margin: 0 }}>Position plans</h3>
            <p className="muted" style={{ margin: "4px 0 0" }}>
              Loss and trend breaks propose a flatten. An elapsed horizon re-runs research.
            </p>
          </div>
          <button className="btn" disabled={reviewing} onClick={reviewExits}>
            {reviewing ? "Reviewing…" : "Review exits"}
          </button>
        </div>
        {reviewNote ? <p className="muted">{reviewNote}</p> : null}
        <table className="table">
          <thead>
            <tr>
              <th>Ticker</th>
              <th>Qty</th>
              <th>Avg</th>
              <th>Review after</th>
              <th>Nearest rule</th>
              <th>Last close</th>
            </tr>
          </thead>
          <tbody>
            {(plans.plans || []).map((plan) => (
              <tr key={plan.id}>
                <td>
                  <Hint hint={plan.name}>{plan.ticker}</Hint>
                </td>
                <td>{plan.quantity}</td>
                <td>{plan.avg_price != null ? Number(plan.avg_price).toFixed(2) : "—"}</td>
                <td className="muted">
                  {plan.review_after ? new Date(plan.review_after).toLocaleDateString() : "—"}
                </td>
                <td>{plan.nearest_rule || "unchecked"}</td>
                <td>{plan.last_close != null ? Number(plan.last_close).toFixed(2) : "—"}</td>
              </tr>
            ))}
            {!(plans.plans || []).length ? (
              <tr>
                <td colSpan={6} className="muted">
                  No open plans. A plan is written when a buy fills.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
        {(plans.unplanned || []).length ? (
          <p className="muted">
            Unplanned broker positions:{" "}
            {plans.unplanned.map((p, i) => (
              <React.Fragment key={p.ticker}>
                {i > 0 ? ", " : null}
                <Hint hint={p.name}>{p.ticker}</Hint>
                {` (${p.quantity})`}
              </React.Fragment>
            ))}
            . These are not auto-sold.
          </p>
        ) : null}
      </div>

      <div className="panel">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div>
            <h3 style={{ margin: 0 }}>IBKR book</h3>
            <p className="muted" style={{ margin: "4px 0 0" }}>
              Account {book?.account_id || "—"} · source {book?.source || "—"}
            </p>
          </div>
          <button className="btn secondary" onClick={() => refresh()}>
            Refresh
          </button>
        </div>
        {error ? <div className="error">{error}</div> : null}
        <p>
          Cash ({market.currency}){" "}
          <strong>{book ? book.cash.toFixed(2) : "—"}</strong>
          {book?.buying_power != null ? (
            <>
              {" "}
              · Buying power <strong>{Number(book.buying_power).toFixed(2)}</strong>
            </>
          ) : null}
        </p>
        <table className="table">
          <thead>
            <tr>
              <th>Ticker</th>
              <th>Qty</th>
              <th>Avg cost</th>
            </tr>
          </thead>
          <tbody>
            {(book?.positions || []).map((p) => (
              <tr key={p.ticker}>
                <td>
                  <Hint hint={p.name}>{p.ticker}</Hint>
                </td>
                <td>{p.quantity}</td>
                <td>{p.avg_cost ?? "—"}</td>
              </tr>
            ))}
            {!book?.positions?.length ? (
              <tr>
                <td colSpan={3} className="muted">
                  No positions.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>

        <h4 style={{ margin: "20px 0 8px" }}>Open orders</h4>
        <p className="muted" style={{ margin: "0 0 8px" }}>
          Live working orders at IBKR (limits rest until filled or DAY expires at the{" "}
          {market.closeName}).
        </p>
        <table className="table">
          <thead>
            <tr>
              <th>Id</th>
              <th>Ticker</th>
              <th>Side</th>
              <th>Qty</th>
              <th>Type</th>
              <th>Limit</th>
              <th>TIF</th>
              <th>Status</th>
              <th>Filled</th>
            </tr>
          </thead>
          <tbody>
            {(book?.open_orders || []).map((o) => (
              <tr key={o.order_id}>
                <td className="muted">{o.order_id}</td>
                <td>
                  <Hint hint={o.name}>{o.ticker}</Hint>
                </td>
                <td>{o.side}</td>
                <td>{o.quantity}</td>
                <td>{o.order_type || "—"}</td>
                <td>{o.limit_price != null ? Number(o.limit_price).toFixed(2) : "—"}</td>
                <td>{o.tif || "—"}</td>
                <td>
                  <Badge status={o.status} />
                </td>
                <td>
                  {o.filled != null ? o.filled : "—"}
                  {o.remaining != null ? ` / ${o.remaining} left` : ""}
                </td>
              </tr>
            ))}
            {!(book?.open_orders || []).length ? (
              <tr>
                <td colSpan={9} className="muted">
                  No open orders.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div>
            <h3 style={{ margin: 0 }}>Fills</h3>
            <p className="muted" style={{ margin: "4px 0 0" }}>
              Desk fill records. Resting orders are picked up by reconcile (also runs in the background).
            </p>
          </div>
          <button className="btn secondary" disabled={reconciling} onClick={reconcileFills}>
            {reconciling ? "Reconciling…" : "Reconcile fills"}
          </button>
        </div>
        {reconcileNote ? <p className="muted">{reconcileNote}</p> : null}
        <table className="table">
          <thead>
            <tr>
              <th>Time</th>
              <th>Ticker</th>
              <th>Side</th>
              <th>Qty</th>
              <th>Price</th>
              <th>Broker id</th>
            </tr>
          </thead>
          <tbody>
            {fills.map((f) => (
              <tr key={f.id}>
                <td className="muted">
                  {f.filled_at ? new Date(f.filled_at).toLocaleString() : "—"}
                </td>
                <td>
                  <Hint hint={f.name}>{f.ticker}</Hint>
                </td>
                <td>{f.side}</td>
                <td>{f.quantity}</td>
                <td>{f.fill_price}</td>
                <td className="muted">{f.broker_order_id}</td>
              </tr>
            ))}
            {!fills.length ? (
              <tr>
                <td colSpan={6} className="muted">
                  No fills recorded yet. Use Reconcile fills if IBKR already filled resting orders.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function AuditPanel() {
  const market = useMarket();
  const [events, setEvents] = useState([]);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    setEvents(await api(withMarketQuery("/api/audit", market.id)));
  }, [market.id]);

  useEffect(() => {
    refresh().catch((e) => setError(String(e.message || e)));
    const id = setInterval(() => refresh().catch(() => {}), 5000);
    return () => clearInterval(id);
  }, [refresh]);

  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>Audit</h3>
        <button className="btn secondary" onClick={() => refresh()}>
          Refresh
        </button>
      </div>
      {error ? <div className="error">{error}</div> : null}
      <table className="table">
        <thead>
          <tr>
            <th>Time</th>
            <th>Type</th>
            <th>Actor</th>
            <th>Run</th>
            <th>Payload</th>
          </tr>
        </thead>
        <tbody>
          {events.map((e) => (
            <tr key={e.id}>
              <td className="muted">
                {e.created_at ? new Date(e.created_at).toLocaleString() : "—"}
              </td>
              <td>{e.event_type}</td>
              <td>{e.actor}</td>
              <td className="muted">{e.run_id ? e.run_id.slice(0, 8) : "—"}</td>
              <td>
                <code>{JSON.stringify(e.payload)}</code>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DeskApp({ marketId }) {
  const [tab, setTab] = useState("Runs");
  const [health, setHealth] = useState(null);
  const market = useMarket();

  useEffect(() => {
    setTab("Runs");
  }, [marketId]);

  useEffect(() => {
    api("/api/health")
      .then(setHealth)
      .catch(() => setHealth({ ok: false }));
  }, []);

  return (
    <div className="app">
      <header>
        <div>
          <h1>Research Desk</h1>
          <p>
            Human-confirmed IBKR paper trading · {market.label} ({market.currency})
          </p>
        </div>
        <div className="muted" style={{ textAlign: "right" }}>
          {health?.trading_mode ? (
            <>
              mode <strong>{health.trading_mode}</strong>
              {health.mock_broker ? " · mock broker" : " · IBKR connected"}
              {health.mock_llm ? " · mock LLM" : " · Nous"}
              {health.autopilot_armed ? " · Autopilot armed" : " · Autopilot off"}
            </>
          ) : (
            "connecting…"
          )}
        </div>
      </header>

      <div className="tabs" style={{ marginBottom: 8 }}>
        <Link
          className={`tab ${marketId === "us" ? "active" : ""}`}
          to="/us"
          style={{ textDecoration: "none" }}
        >
          US
        </Link>
        <Link
          className={`tab ${marketId === "hk" ? "active" : ""}`}
          to="/hk"
          style={{ textDecoration: "none" }}
        >
          Hong Kong
        </Link>
      </div>

      <div className="tabs">
        {TABS.map((name) => (
          <button
            key={name}
            className={`tab ${tab === name ? "active" : ""}`}
            onClick={() => setTab(name)}
          >
            {name}
          </button>
        ))}
      </div>

      {tab === "Runs" ? <RunsPanel /> : null}
      {tab === "Scan" ? <ScanPanel onStarted={() => setTab("Runs")} /> : null}
      {tab === "Inbox" ? <InboxPanel /> : null}
      {tab === "Book" ? <BookPanel /> : null}
      {tab === "Audit" ? <AuditPanel /> : null}
    </div>
  );
}

function MarketRoute() {
  const { marketId } = useParams();
  if (marketId !== "us" && marketId !== "hk") {
    return <Navigate to="/us" replace />;
  }
  return (
    <MarketProvider market={marketId}>
      <DeskApp marketId={marketId} />
    </MarketProvider>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/us" replace />} />
      <Route path="/:marketId/*" element={<MarketRoute />} />
    </Routes>
  );
}
