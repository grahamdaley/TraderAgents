# Research Desk — User Guide

Operator guide for the investment-committee research desk: start a run, review agent output, approve or reject a trade, and track paper fills.

---

## What this system does

1. You enter a US equity ticker.
2. Four specialist agents research it in parallel (fundamentals, technicals, sentiment, macro).
3. A portfolio manager synthesizes a thesis; a risk narrative and a **code** policy engine decide whether a trade may be proposed.
4. The run **pauses** for your decision in the operator console.
5. If you approve, the system submits to **IBKR paper** (or a mock broker in local smoke tests).

Research agents never place orders. Only the post-approval execution path can submit.

---

## Prerequisites

- Docker and Docker Compose
- Python 3.11+ (for local API development)
- Node.js 20+ (for the Vite console in dev mode)
- A [Nous Portal](https://portal.nousresearch.com) API key (unless you use mock LLM mode)
- Optional: Interactive Brokers username/password and a **paper** account id (`DU…`)

---

## First-time setup

1. Clone the repo and enter the project directory.

2. Copy the environment template:

```bash
cp .env.example .env
```

3. Edit `.env`:

| Variable | Purpose |
|---|---|
| `NOUS_API_KEY` | Nous Portal key for models |
| `IBKR_ACCOUNT_ID` | Paper account (`DU…`) |
| `TWS_USERID` / `TWS_PASSWORD` | IB Gateway login (required for Compose) |
| `ALLOW_MOCK_LLM=true` | Skip real model calls (local smoke tests) |
| `ALLOW_MOCK_BROKER=true` | In-memory book instead of IBKR (off by default) |
| `SKIP_NOUS_CATALOG_CHECK=true` | Skip Portal model-id verification |

Paper trading expects real Gateway. Only enable mocks for offline LLM smoke tests:

```bash
ALLOW_MOCK_LLM=true
SKIP_NOUS_CATALOG_CHECK=true
# ALLOW_MOCK_BROKER=true   # optional; prefer real Gateway
```

4. Start Postgres + Gateway (or the full stack below):

```bash
docker compose up -d postgres ib-gateway
```

---

## Running the stack

### Option A — Full Docker Compose (recommended)

Ensure `.env` has `TWS_USERID`, `TWS_PASSWORD`, and `IBKR_ACCOUNT_ID=DU…`, then:

```bash
docker compose up --build
```

- Console via Caddy: [http://localhost:8080](http://localhost:8080)  
- Direct web container: [http://localhost:5173](http://localhost:5173)  
- API: [http://localhost:8000](http://localhost:8000)  
- Gateway API: host port **4004**; VNC on **5900**

`docker compose down` stops API, web, Postgres, and IB Gateway together.

### Option B — Local API + console (Gateway still in Docker)

```bash
docker compose up -d postgres ib-gateway

python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# When the API runs on the host, use the published Gateway port:
# IBKR_HOST=127.0.0.1
# IBKR_PORT=4004

uvicorn trader.api:app --reload --app-dir src --host 127.0.0.1 --port 8000
```

In another terminal:

```bash
cd web
npm install
npm run dev
```

- Console: [http://127.0.0.1:5173](http://127.0.0.1:5173)  
- API: [http://127.0.0.1:8000/api/health](http://127.0.0.1:8000/api/health)

---

## Operator console

The header shows trading mode and whether mock LLM / mock broker are active.

### Runs

1. Enter a ticker (e.g. `AAPL`).
2. Click **Start research**.
3. Wait until the run status is `awaiting_human` (or `rejected` if policy blocked a trade).
4. Click **Open** to inspect node status and the full state JSON (specialist reports, PM thesis, policy checks).

Nodes you will typically see: `fundamental`, `technical`, `sentiment`, `macro`, `portfolio_manager`, `risk`, `policy`, `human_gate`, `execute`.

### Inbox

Proposals waiting for you appear here.

For each proposal you can:

| Action | Effect |
|---|---|
| **Approve** | Submit as proposed (after policy + price-band re-check) |
| **Approve with edits** | Change quantity and/or limit, then submit (new content hash) |
| **Reject** | End the run; no order |
| **Request research** | End without submitting (same as reject for execution) |

Approvals are **idempotent**: a second decision on the same run returns HTTP 409. A proposal expires after `PROPOSAL_TTL_MINUTES` (default 30). If the live quote has moved more than `PRICE_BAND_PCT` (default 2%) from the proposal reference price, submission is blocked.

### Book

Shows cash, positions, and fills for the configured account (IBKR paper or mock). Refresh if the panel looks empty after a fill.

### Audit

Immutable-style event log: `run_started`, `proposal_created`, `human_decision`, `order_filled`, failures, etc.

---

## End-to-end workflow

```text
Ticker → specialists (parallel) → PM → risk → policy
      → Inbox (you decide)
      → IBKR paper / mock fill → Book + Audit
```

**Sides:** `BUY`, `SELL` (flatten only — no shorting), or `HOLD` (no proposal).

**Data sources:**

- Research (statements, ratios, news, daily bars): Yahoo Finance via yfinance  
- Approval quote, positions, cash, fills: IBKR (or mock broker)

---

## Configuration reference

### Models (`config/models.yaml` or env overrides)

| Role | Default Portal model id |
|---|---|
| Fundamental / technical / macro | `anthropic/claude-sonnet-5.5` |
| Sentiment | `anthropic/claude-haiku-5.5` |
| Portfolio manager / risk | `anthropic/claude-opus-5.5` |

IDs must match [Nous Portal models](https://portal.nousresearch.com/models) (provider-prefixed, e.g. `anthropic/claude-sonnet-5.5`). Startup verifies the catalog unless `SKIP_NOUS_CATALOG_CHECK=true`.

### Policy

| Variable | Default | Meaning |
|---|---|---|
| `MAX_POSITION_PCT` | `0.10` | Cap size as a fraction of NAV |
| `PRICE_BAND_PCT` | `0.02` | Max price move vs proposal reference at submit |
| `PROPOSAL_TTL_MINUTES` | `30` | Proposal expiry |
| `RESTRICTED_TICKERS` | empty | Comma-separated tickers that cannot trade |
| `STARTING_CASH` | `100000` | Mock broker starting cash |

### IBKR safety

| Setting | Phase 1 expectation |
|---|---|
| `IBKR_TRADING_MODE` | `paper` |
| `IBKR_LIVE_ORDERS_ENABLED` | `false` |
| Paper account id | Must start with `DU` |

Live trading requires **both** `IBKR_TRADING_MODE=live` and `IBKR_LIVE_ORDERS_ENABLED=true`. Do not enable that until you intentionally move to phase 3.

---

## API cheat sheet

Useful for scripts or debugging (base URL `http://127.0.0.1:8000`):

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Mode flags, mock status |
| `POST` | `/api/runs` | Body: `{"ticker":"AAPL"}` |
| `GET` | `/api/runs` | List runs |
| `GET` | `/api/runs/{id}` | Run detail + state |
| `GET` | `/api/proposals` | Open inbox items |
| `POST` | `/api/runs/{id}/decision` | Approve / reject (see below) |
| `GET` | `/api/book` | Cash and positions |
| `GET` | `/api/fills` | Fills |
| `GET` | `/api/audit` | Audit events |

Example decision:

```bash
curl -X POST http://127.0.0.1:8000/api/runs/<run_id>/decision \
  -H 'Content-Type: application/json' \
  -d '{"action":"approve","operator":"operator"}'
```

`action` values: `approve`, `approve_with_edits`, `reject`, `request_research`.  
For edits, include `"quantity"` and/or `"limit_price"`.

---

## Troubleshooting

| Symptom | What to check |
|---|---|
| Console shows “connecting…” | API not running; open `/api/health` |
| Catalog / model errors on startup | `NOUS_API_KEY`, model ids on Portal, or set `SKIP_NOUS_CATALOG_CHECK=true` for mocks |
| Run stuck or failed | Runs → Open → `error` field and Audit tab |
| Inbox empty after research | Policy may have rejected (`HOLD`, restricted ticker, cash); check run status |
| Approve returns 409 | Run already decided; open Book/Audit for the prior outcome |
| Approve fails on price band | Quote moved > `PRICE_BAND_PCT`; start a new research run |
| IBKR connect warnings | Gateway not up yet, wrong `IBKR_HOST`/`IBKR_PORT`, or missing TWS credentials |
| Paper account rejected | Account id must start with `DU` when `IBKR_TRADING_MODE=paper` |

---

## Safety reminders

- Never put secrets in prompts, logs, or the audit payload (account id and order ids are logged; passwords are not).
- Keep live orders disabled until you deliberately enable both live flags.
- Prefer limit orders and human review for every proposal in phase 1.
- Mock broker is off by default; only enable it for offline development.

---

## Related docs

- Project overview: [README.md](../README.md)
- Environment template: [.env.example](../.env.example)
- Model map: [config/models.yaml](../config/models.yaml)
