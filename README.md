# Research desk with human-confirmed paper trading

LangGraph investment committee that researches US equities, pauses for human approval in a local operator console, and submits approved orders to an IBKR paper account via IB Gateway.

## Architecture

- **Research**: yfinance for statements, ratios, news, and daily bars
- **Decision**: four specialist agents → portfolio manager → risk narrative → deterministic policy
- **HITL**: operator console (approve / edit / reject)
- **Execution**: IBKR paper Gateway only in phase 1 (`IBKR_LIVE_ORDERS_ENABLED=false`)
- **Models**: Nous Portal OpenAI-compatible API

## Quick start

1. Copy `.env.example` to `.env` and set `NOUS_API_KEY`. IBKR credentials are optional until you enable the `ibkr` Compose profile.
2. Install and run the API locally (Postgres via Compose):

```bash
docker compose up -d postgres
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn trader.api:app --reload --app-dir src
```

3. Operator console:

```bash
cd web && npm install && npm run dev
```

4. Full stack (without IB Gateway):

```bash
docker compose up --build
```

5. With IB Gateway paper (requires IBKR username/password in `.env`):

```bash
docker compose --profile ibkr up --build
```

Open the console at `http://localhost:5173` (dev) or `http://localhost:8080` (Caddy).

## Safety

- Research agents never receive order-submit tools.
- Live trading requires both `IBKR_TRADING_MODE=live` and `IBKR_LIVE_ORDERS_ENABLED=true`.
- Paper mode refuses any account id that does not start with `DU`.
