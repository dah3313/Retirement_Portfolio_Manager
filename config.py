# config.py — Retirement Portfolio Manager (RPM)
# ===============================================
# This file contains all tunable parameters for the RPM engine. 
# 
# ⚠️ ARCHITECTURAL PHILOSOPHY ⚠️
# This system is NOT designed for standard, low-yield index funds. It is a 
# specialized decumulation engine built to safely sustain high withdrawal 
# rates (8%+) by pairing high-volatility/high-growth equities with high-yield 
# multi-sector bonds. It uses mechanical circuit breakers and an isolated 
# 18-month Treasury buffer to mathematically survive the crashes inherent 
# to these aggressive asset classes.
#
# ⚠️ TAX WARNING ⚠️
# Explicitly designed for TAX-ADVANTAGED ACCOUNTS (Roth IRA, Traditional IRA). 
# Running this in a taxable account will generate massive tax liabilities.

import os

# ------------------------------------------------------------------
# 1. System & Brokerage Settings
# ------------------------------------------------------------------
STATE_FILE = os.path.join(os.path.dirname(__file__), 'rpm_state.json')
LOG_DIR = os.path.join(os.path.dirname(__file__), 'logs')
LOG_FILE = 'rpm.log'
AUDIT_FILE = 'rpm_audit.jsonl'

# IBKR Local Gateway settings. 
IBKR_HOST = '127.0.0.1'
IBKR_PORT = 4001        
IBKR_CLIENT_ID = 10     

# ------------------------------------------------------------------
# 2. Dynamic Initialization (Day 1 Setup)
# ------------------------------------------------------------------
# Used ONLY ONCE by setup.py to calculate permanent dollar baselines.
INITIAL_WITHDRAWAL_RATE = 0.085  # 8.5% annual target (requires high-yield/growth core)
INITIAL_BUFFER_MONTHS = 18       # 18 months of strict SGOV crisis protection

# ------------------------------------------------------------------
# 3. Portfolio Definition (The High-Velocity Core)
# ------------------------------------------------------------------
# Growth Bucket: High Volatility / High Appreciation
# Default: Blue Chip Growth (FBCG) + Small Cap Value (AVUV)
TICKERS_GROWTH = ['FBCG', 'AVUV']

# Fixed Income Bucket: High Yield / Multi-Sector Bonds
# Default: Active Multi-Sector Income (PYLD) + Active Preferred/Income (JPIE)
TICKERS_FI = ['PYLD', 'JPIE']      

# Target Allocation (Must sum to 1.0)
TARGET_ALLOCATION_GROWTH = 0.50
TARGET_ALLOCATION_FI = 0.50

CORE_TICKERS = TICKERS_GROWTH + TICKERS_FI

# The Crisis Buffer: The blast shield. Held completely outside core math.
TICKER_BUFFER = 'SGOV'

# The Cash Buffer: Settled USD used to fund ACH pulls and friction.
CASH_TICKER = 'USD'
CASH_TRANSACTION_BUFFER = 1000.0

# ------------------------------------------------------------------
# 4. Circuit Breakers & Crisis Detection
# ------------------------------------------------------------------
# The RPM monitors a "Synthetic Index" of the highly volatile Growth
# assets to evaluate true portfolio stress and trigger the blast
# shields.  The index is built by chaining EQUAL-WEIGHTED RETURNS
# across the underlying tickers (see ibkr_client.get_synthetic_index_and_sma);
# do NOT confuse this with a price-blended index.  The pre-Session-4
# implementation averaged share prices across symbols, which silently
# made the index price-weighted (a $500 fund dominated a $50 fund
# regardless of allocation).  Equal-weighting the per-bar returns
# instead matches the spec from Newruleset.txt: "Growth synthetic
# price index 200-day SMA".
SYNTHETIC_INDEX_TICKERS = TICKERS_GROWTH

# Trend-following Circuit Breaker Lookback
# ----------------------------------------
# Pre-Session-4 the lookback was 200 calendar days of daily bars.
# Daily bars made the SMA noisy and whipsawy: a single sharp dip
# would push the index across -5% / -7.5% thresholds and back
# inside a week, causing rebalancing to halt and resume too
# frequently.  The post-Session-4 lookback is 10 monthly bars,
# which gives an explicit ~10-month long-term trend signal that
# tracks regime shifts (think 2008, 2020, 2022) but ignores
# week-scale noise.  We accept the cost of a slower entry into
# crisis mode (one full month of additional drawdown) in exchange
# for materially fewer false alarms and less SGOV churn.
#
# IB Gateway accepts these duration / bar-size strings; see
# https://interactivebrokers.github.io/tws-api/historical_bars.html
TREND_SMA_PERIOD = '10 M'
TREND_SMA_BAR = '1 month'

# Backwards-compatibility aliases for any operator scripts still
# importing the old names.  Removed in a future release.
SMA_200_PERIOD = TREND_SMA_PERIOD   # DEPRECATED: misnomer post-Session-4
SMA_200_BAR = TREND_SMA_BAR         # DEPRECATED: misnomer post-Session-4

# Thresholds are relative to the proxy index's own trend SMA.  Both
# the level and the SMA are dimensionless index values (anchored at
# 100 at the start of each fetch window) so the percentages
# compose cleanly.
HALT_REBALANCE_THRESHOLD = -0.05
SHY_TRANSITION_THRESHOLD = -0.075
RECOVERY_ABOVE_TRANSITION = 0.03

# ------------------------------------------------------------------
# 5. Recovery & Refill Mechanics
# ------------------------------------------------------------------
BUFFER_REFILL_DELAY_DAYS = 60
# Siphons core assets at 8.33% per month to rebuild a depleted SGOV buffer in 12 months.
BUFFER_REFILL_MONTHLY_RATE = 0.0833

# ------------------------------------------------------------------
# 6. Annual Inflation Guardrails (November Review)
# ------------------------------------------------------------------
ANNUAL_INFLATION_RATE = 0.03
SMA_12MO_PERIOD = '12 M'
SMA_12MO_BAR = '1 month'
INFLATION_FREEZE_THRESHOLD = -0.05

# ------------------------------------------------------------------
# 7. November Special Dividend (Bull Market Extraction)
# ------------------------------------------------------------------
BONUS_EVAL_MONTH = 11
BONUS_GROWTH_YOY_THRESHOLD = 0.25   # Trigger: Growth bucket exceeds 25% YoY return.
BONUS_EXCESS_TAKE_RATE = 0.05       # Action: Extract 5% of excess gains as a cash bonus.

# ------------------------------------------------------------------
# 8. Execution Safety Limits
# ------------------------------------------------------------------
MAX_SINGLE_TRADE_DOLLARS = 15_000.0

# ------------------------------------------------------------------
# 8b. Withdrawal Cascade Per-Position Residual (Session 5)
# ------------------------------------------------------------------
# When the monthly cash-raising routine cascades through SGOV → FI
# → Growth, no INDIVIDUAL position is allowed to be drawn below
# this dollar floor.  Pre-Session-5 the cascade would happily drain
# a position to zero, which had two bad properties:
#
#   1. Liquidating a position completely "forgets" the cost basis
#      lot information IBKR was carrying — fine in a Roth, but
#      removes any chance of recovering through future rebalancing
#      buys without re-establishing the lot.
#   2. A zero balance creates downstream UX pain: the per-position
#      proportional-weight math elsewhere does `bal / bucket_total`
#      and a zero in the numerator gets that ticker permanently
#      excluded from rebalancing buys until cash redeploys.
#
# $1,500 is chosen to match IPM's withdrawal_buffer.per_position_residual_usd
# (ruleset 1.5.1).  Rationale captured in the IPM design spec at
# C:\portfolio\docs\WITHDRAWAL_BUFFER_DESIGN.md — it's small enough
# not to materially constrain withdrawals (against a typical $XX,000
# bucket it's a rounding error on any single month) but large enough
# that the position survives as a non-trivial holding.
#
# When a tier's available capacity (sum of (bal - residual) over
# tier members) is exhausted, the cascade SPILLS to the next tier.
# This is the same "cascade exhausted" path that fires the
# 'EMPTY_MOVING_TO_FI' / 'FI_EMPTY_MOVING_TO_GROWTH' alerts.
PER_POSITION_RESIDUAL_USD = 1500.0

# ------------------------------------------------------------------
# 9. 5/25 Rebalancing Bands (Drift Detection)
# ------------------------------------------------------------------
# Per the README and Newruleset.txt: the 50/50 Growth/FI core gets
# rebalanced when the Growth share drifts beyond 5% absolute OR 25%
# relative to its 0.50 target.  These constants are read by
# portfolio.Portfolio.get_drift().  Pre-fix portfolio.py referenced
# both names but config.py never defined them — calling get_drift()
# raised AttributeError at the first weekly run.
#
#   Absolute: |current_growth_pct - 0.50| > 0.05
#             i.e. growth bucket has drifted to <45% or >55% of core
#   Relative: |current - target| / target > 0.25
#             i.e. growth bucket has drifted by >25% of its own target
#             (so growth pct outside [0.375, 0.625]).
# In a 50/50 portfolio the absolute band fires first; the relative
# band exists for parity with non-50/50 allocations someone might
# configure later.
REBALANCE_BAND_ABSOLUTE = 0.05    # 5 percent absolute drift trigger
REBALANCE_BAND_RELATIVE = 0.25    # 25 percent relative drift trigger

# ------------------------------------------------------------------
# 10. Dynamic-Config Initial Defaults
# ------------------------------------------------------------------
# main.apply_dynamic_config() OVERRIDES this at runtime to
# (current_monthly_withdrawal + CASH_TRANSACTION_BUFFER), which is
# the value all downstream modules actually consume.  The default
# below exists so that direct imports of portfolio.py (e.g. from a
# test or from the --heartbeat path) do not raise AttributeError if
# they happen to call generate_rebalance_trades or
# route_buffer_refill_sells before main has initialized the
# override.  Heartbeat itself does not call those methods, but
# defensive default = cheap insurance.
CASH_BUFFER_TARGET = CASH_TRANSACTION_BUFFER   # placeholder; overridden at runtime

# ------------------------------------------------------------------
# 11. Persistent-State Schema Version
# ------------------------------------------------------------------
# Stamped onto every rpm_state.json save and checked on every load.
# Bumped to 2 in Session 4 because the synthetic index switched
# from a price blend (transition_price was a dollar share price,
# ~$45) to a returns-chained level series anchored at 100.  Pre-
# Session-4 state files with in_buffer_transition=True therefore
# carry a transition_price that's incompatible with the new
# circuit breaker math, and main.load_state() refuses to load them
# while crisis is active.  See main.py for the migration logic.
STATE_SCHEMA_VERSION = 2