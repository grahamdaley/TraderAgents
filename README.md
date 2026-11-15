# Research desk with human-confirmed paper trading

LangGraph investment committee that researches US equities, pauses for human approval in a local operator console, and submits approved orders to an IBKR paper account via IB Gateway.

## Architecture

- **Research**: yfinance for statements, ratios, news, and daily bars
- **Decision**: four specialist agents → portfolio manager → risk narrative → deterministic policy
- **HITL**: operator console (approve / edit / reject)
- **Execution**: IBKR paper Gateway only in phase 1 (`IBKR_LIVE_ORDERS_ENABLED=false`)
- **Models**: Nous Portal OpenAI-compatible API

## Quick start

1. Copy `.env.example` to `.env` and set `NOUS_API_KEY`, `TWS_USERID`, `TWS_PASSWORD`, and `IBKR_ACCOUNT_ID=DU…`.
2. Full stack (API, web, Postgres, IB Gateway paper):

```bash
docker compose up --build
```

3. Or run the API locally against Compose Postgres + Gateway:

```bash
docker compose up -d postgres ib-gateway
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
# Point IBKR_HOST=127.0.0.1 when the API runs on the host
uvicorn trader.api:app --reload --app-dir src
```

4. Operator console (dev):

```bash
cd web && npm install && npm run dev
```

Open the console at `http://localhost:5173` (dev) or `http://localhost:8080` (Caddy). `docker compose down` stops Gateway with the rest of the stack.

## Safety

- Research agents never receive order-submit tools.
- Live trading requires both `IBKR_TRADING_MODE=live` and `IBKR_LIVE_ORDERS_ENABLED=true`.
- Paper mode refuses any account id that does not start with `DU`.
