# Polymarket Reward-Aware Conservative Liquidity Bot

A conservative market-making bot for **Polymarket CLOB Liquidity
Rewards**.

This project is designed to optimize for **reward capture**, not
directional trading profit.\
The current implementation prioritizes:

1.  Maximizing Polymarket liquidity rewards
2.  Minimizing directional exposure and fill risk
3.  Operating conservatively with bounded capital and risk controls
4.  Providing a local dashboard for monitoring and runtime controls

> **Important:** This is still an experimental bot under active
> development.\
> No profit is promised or implied.

------------------------------------------------------------------------

## Table of Contents

-   Project Status
-   Architecture
-   Repository Structure
-   Core Strategy
-   Reward Methodology
-   Risk State Machine
-   Dashboard
-   Configuration
-   Installation
-   Running the Bot
-   Runtime Controls
-   Polymarket Integration
-   Limitations and Approximations
-   Known Issues
-   Roadmap
-   Disclaimer

------------------------------------------------------------------------

## Project Status

Current status of the project:

-   Modular bot architecture implemented
-   Reward-aware market selection implemented
-   Reward scoring approximation implemented
-   Order placement and cancellation flow implemented
-   Risk state machine implemented
-   Local FastAPI dashboard implemented
-   Runtime overrides implemented
-   Bot still under validation for stable live operation

This repository should be treated as an **alpha-stage research and
engineering project**, not a production-ready trading system.

------------------------------------------------------------------------

## Architecture

The bot is split into focused modules:

  Module           Responsibility
  ---------------- --------------------------------------
  main.py          Top-level async orchestrator
  config.py        Loads `.env`, validates parameters
  poly_client.py   Polymarket API wrapper
  selector.py      Discovers and ranks rewards markets
  rewards.py       Reward scoring approximation
  orders.py        Desired vs live order reconciliation
  risk.py          Risk management state machine
  dashboard.py     FastAPI dashboard
  models.py        Shared Pydantic models
  utils.py         Logging, helpers, rate limiting

High-level flow:

Dashboard → main → selector / orders / risk → poly_client → Polymarket
API

------------------------------------------------------------------------

## Core Strategy

The bot maximizes:

reward_per_capital = estimated_daily_reward / capital_required

The goal is to deploy liquidity where reward efficiency is highest.

Two quoting modes are supported:

  Mode                 Description
  -------------------- ----------------------------------
  SINGLE_SIDED_SAFE    Conservative quoting on one side
  TWO_SIDED_BALANCED   Quotes both YES and NO tokens

------------------------------------------------------------------------

## Reward Methodology

Quadratic scoring formula:

S(v,s) = ((v − s) / v)² × b

Where

v = max incentive spread\
s = distance from midpoint\
b = multiplier (usually 1)

Estimated share:

share_est ≈ Q_min / (competition_depth + Q_min)

Daily reward:

daily_reward_est = share_est × reward_epoch_daily_budget

------------------------------------------------------------------------

## Risk State Machine

SELECTING → RUNNING → COOLDOWN → SELECTING ↓ UNHEDGED ↓ PAUSED

  State       Meaning
  ----------- --------------------------------
  SELECTING   Searching best market
  RUNNING     Active quoting
  COOLDOWN    Waiting after risk event
  UNHEDGED    Fill occurred, trying to hedge
  PAUSED      Manual pause or kill switch

------------------------------------------------------------------------

## Dashboard

Default dashboard URL:

http://localhost:8090

The dashboard allows:

-   Start / Pause / Stop the bot
-   View selected market
-   Inspect reward estimates
-   Inspect orders and positions
-   Inspect logs
-   Apply runtime configuration overrides

------------------------------------------------------------------------

## Configuration

Configuration is loaded from `.env`.

Minimal example:

PRIVATE_KEY=\
POLY_L2_API_KEY=\
POLY_L2_SECRET=\
POLY_L2_PASSPHRASE=

BANKROLL_USDC=100\
MAX_POSITION_USD=20\
MAX_CAPITAL_PER_MARKET=40

MODE=SINGLE_SIDED_SAFE\
TARGET_MIN_DISTANCE=0.01\
TARGET_MAX_DISTANCE=0.02

------------------------------------------------------------------------

## Installation

Clone repository:

git clone https://github.com/your-org/polymarket-reward-bot.git

Create environment:

python -m venv .venv

Activate:

source .venv/bin/activate

Install dependencies:

pip install -r requirements.txt

------------------------------------------------------------------------

## Running the Bot

Run from the root folder:

python src/main.py

Dashboard will be available at:

http://localhost:8090

------------------------------------------------------------------------

## Runtime Controls

The dashboard exposes controls:

START → bot begins selecting markets\
PAUSE → cancels orders and pauses activity\
STOP → cancels orders and resets state

------------------------------------------------------------------------

## Polymarket Integration

The bot integrates with:

-   py-clob-client
-   Polymarket rewards markets API
-   CLOB orderbooks
-   Open orders
-   Positions
-   Trades
-   WebSocket feeds

------------------------------------------------------------------------

## Limitations and Approximations

Adjusted midpoint uses simple midpoint approximation.

Competition is estimated from public orderbook depth.

Real reward share may differ from estimates.

Bot state is currently in-memory only.

------------------------------------------------------------------------

## Known Issues

-   START / PAUSE / STOP control still being hardened
-   Order cancellation robustness still being validated
-   Local state may temporarily diverge from exchange state

------------------------------------------------------------------------

## Roadmap

Short term:

-   Improve control reliability
-   Improve order reconciliation
-   Improve monitoring and logging

Mid term:

-   Persistence layer
-   Telegram alerts
-   Multi-bot orchestration

Long term:

-   Web platform
-   Copy trading interface
-   Strategy marketplace

------------------------------------------------------------------------

## Disclaimer

This software is experimental.

It is not financial advice and not guaranteed to be profitable.

Always test with small capital first.
