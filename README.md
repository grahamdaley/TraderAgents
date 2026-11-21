# Research desk with human-confirmed paper trading

LangGraph investment committee that researches **US and Hong Kong** equities, pauses for human approval in a local operator console, and submits approved orders to an IBKR paper account via IB Gateway.

## Architecture

- **Markets**: separate US (`/us`) and Hong Kong (`/hk`) desks — SMART/USD and SEHK/HKD
- **Research**: yfinance for statements, ratios, news, and daily bars
- **Universes**: S&P 100 (`config/universe.csv`); HSI + Hang Seng TECH (`config/universe-hk.csv`)
- **Decision**: four specialist agents → portfolio manager → risk narrative → deterministic policy
- **HITL**: operator console (approve / edit / reject) with a market switcher
- **Execution**: IBKR paper Gateway only in phase 1 (`IBKR_LIVE_ORDERS_ENABLED=false`)
- **Autopilot**: optional paper pass after each venue’s open (`America/New_York` and `Asia/Hong_Kong`)
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

Operator details: [docs/user-guide.md](docs/user-guide.md).

## Deploy on a Linux box (internal network)

Use this path for a LAN-only test host that pulls pre-built API/web images from AWS ECR. Keep the box off the public internet — Caddy currently has no auth.

### Prerequisites

- Docker Engine + Compose plugin
- AWS CLI v2
- Outbound HTTPS to AWS (ECR), Nous Portal, and IBKR as needed
- Pulumi CLI + Node.js 20+ on the machine that provisions AWS (can be your laptop)

### 1. Provision ECR (once)

From a machine with admin AWS credentials:

```bash
cd infra
npm install
pulumi stack select dev   # or: pulumi stack init dev
pulumi up
```

Capture outputs (see [infra/README.md](infra/README.md)):

```bash
pulumi stack output apiRepositoryUrl
pulumi stack output webRepositoryUrl
pulumi stack output awsRegion
pulumi stack output accessKeyId
pulumi stack output --show-secrets secretAccessKey
```

### 2. Push images from your build machine

Configure an AWS CLI profile with the Pulumi-issued access key, then:

```bash
export AWS_PROFILE=trader-ecr
./scripts/push-ecr.sh
```

The script reads `awsRegion`, `apiRepositoryUrl`, and `webRepositoryUrl` from the Pulumi stack in `infra/`, and builds for **linux/amd64** (override with `DOCKER_PLATFORM` if needed).

### 3. Configure the Linux box

1. Install Docker + AWS CLI, then create `~/.aws/credentials` with the same access key under profile `trader-ecr`.
2. Clone this repo (or copy `docker-compose.yml`, `docker-compose.deploy.yml`, `deploy/Caddyfile`, and `.env.example`).
3. Copy env and fill secrets plus image URIs:

```bash
cp .env.example .env
# Set NOUS_API_KEY, TWS_USERID, TWS_PASSWORD, IBKR_ACCOUNT_ID=DU…
# Set:
#   ECR_API_IMAGE=<apiRepositoryUrl>:latest
#   ECR_WEB_IMAGE=<webRepositoryUrl>:latest
```

4. Log in to ECR, pull, and start (no local image build):

```bash
export AWS_PROFILE=trader-ecr
export AWS_REGION=ap-southeast-1   # match: pulumi stack output awsRegion
# Registry host is the part before the first slash of apiRepositoryUrl, e.g.:
#   123456789012.dkr.ecr.ap-southeast-1.amazonaws.com
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin 123456789012.dkr.ecr.ap-southeast-1.amazonaws.com

docker compose -f docker-compose.yml -f docker-compose.deploy.yml pull
docker compose -f docker-compose.yml -f docker-compose.deploy.yml up -d
```

Open the console at `http://<linux-box-lan-ip>:8080`.

Stop with:

```bash
docker compose -f docker-compose.yml -f docker-compose.deploy.yml down
```

A future AWS VM can reuse the same ECR images and compose override; VM/host IaC is not included yet.

## Safety

- Research agents never receive order-submit tools.
- Live trading requires both `IBKR_TRADING_MODE=live` and `IBKR_LIVE_ORDERS_ENABLED=true`.
- Paper mode refuses any account id that does not start with `DU`.
