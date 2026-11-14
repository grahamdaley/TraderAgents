import React, { useCallback, useEffect, useState } from "react";

const TABS = ["Runs", "Inbox", "Book", "Audit"];

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

function RunsPanel({ onSelectRun }) {
  const [ticker, setTicker] = useState("AAPL");
  const [runs, setRuns] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState(null);

  const refresh = useCallback(async () => {
    const data = await api("/api/runs");
    setRuns(data);
  }, []);

  useEffect(() => {
    refresh().catch((e) => setError(String(e.message || e)));
    const id = setInterval(() => refresh().catch(() => {}), 4000);
    return () => clearInterval(id);
  }, [refresh]);

  async function startRun() {
    setBusy(true);
    setError("");
    try {
      const result = await api("/api/runs", {
        method: "POST",
        body: JSON.stringify({ ticker }),
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
    const detail = await api(`/api/runs/${id}`);
    setSelected(detail);
    onSelectRun?.(detail);
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
                  <td>{r.ticker}</td>
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
              <strong>{selected.ticker}</strong> · <Badge status={selected.status} />
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

function InboxPanel() {
  const [items, setItems] = useState([]);
  const [selected, setSelected] = useState(null);
  const [qty, setQty] = useState("");
  const [limit, setLimit] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    const data = await api("/api/proposals");
    setItems(data);
  }, []);

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
  }

  async function decide(action) {
    if (!selected) return;
    setBusy(true);
    setError("");
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
      await api(`/api/runs/${selected.run_id}/decision`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      setSelected(null);
      await refresh();
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
          </>
        )}
      </div>
    </div>
  );
}

function BookPanel() {
  const [book, setBook] = useState(null);
  const [fills, setFills] = useState([]);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    const [b, f] = await Promise.all([api("/api/book"), api("/api/fills")]);
    setBook(b);
    setFills(f);
  }, []);

  useEffect(() => {
    refresh().catch((e) => setError(String(e.message || e)));
    const id = setInterval(() => refresh().catch(() => {}), 5000);
    return () => clearInterval(id);
  }, [refresh]);

  return (
    <div>
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
          Cash <strong>{book ? book.cash.toFixed(2) : "—"}</strong>
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
                <td>{p.ticker}</td>
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
      </div>

      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Fills</h3>
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
                <td>{f.ticker}</td>
                <td>{f.side}</td>
                <td>{f.quantity}</td>
                <td>{f.fill_price}</td>
                <td className="muted">{f.broker_order_id}</td>
              </tr>
            ))}
            {!fills.length ? (
              <tr>
                <td colSpan={6} className="muted">
                  No fills yet.
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
  const [events, setEvents] = useState([]);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    setEvents(await api("/api/audit"));
  }, []);

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

export default function App() {
  const [tab, setTab] = useState("Runs");
  const [health, setHealth] = useState(null);

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
          <p>Human-confirmed IBKR paper trading</p>
        </div>
        <div className="muted" style={{ textAlign: "right" }}>
          {health ? (
            <>
              mode <strong>{health.trading_mode}</strong>
              {health.mock_broker ? " · mock broker" : " · IBKR connected"}
              {health.mock_llm ? " · mock LLM" : " · Nous"}
            </>
          ) : (
            "connecting…"
          )}
        </div>
      </header>

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
      {tab === "Inbox" ? <InboxPanel /> : null}
      {tab === "Book" ? <BookPanel /> : null}
      {tab === "Audit" ? <AuditPanel /> : null}
    </div>
  );
}
