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
# The RPM monitors a "Synthetic Index" of your highly volatile Growth assets
# to evaluate true portfolio stress and trigger the blast shields.
SYNTHETIC_INDEX_TICKERS = TICKERS_GROWTH

# Weekly Circuit Breakers (Lookback: 200 Days)
SMA_200_PERIOD = '200 D'
SMA_200_BAR = '1 day'

# Thresholds are relative to the proxy index's own 200-day SMA.
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