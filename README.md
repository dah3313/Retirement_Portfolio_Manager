##Retirement Portfolio Manager (RPM)

-- _Read this in its entirety before embarking on this project!_

- This whole project from conception on was predicated on a high-yield multi sector bonds backend bolted to a high volatility high yield front end.  Bonds stay stable & volatile Growth is harvested when up and left alone when down.  It's designed to take _advantage_ of Growth risk but not actually _BE_ risky.
- The logic structure is designed to provide reliable income at via a high withdrawal rate up to 8.5% relative to the starting portfolio total balance while preventing damage to Growth assets in a market downturn and to harvest the high volatility growth assets while providing for a thick buffer (SGOV+FI) thus de-risking the withdrawal schema to ride out extended market rough patches.  I can't claim any credit for the code itself, Gemini and Claude did all the coding.  The ideas are my own.

The RPM is a stateless, rules-based, high-velocity decumulation appliance designed to run autonomously on a headless Linux server. It connects directly to an Interactive Brokers (IBKR) account to manage a specialized retirement portfolio.

⚠️ CRITICAL WARNINGS: READ BEFORE CLONING ⚠️

**1. DAY-1 INTERACTIVE LEGACY-ASSET PICKER**

When you run `setup.py`, the RPM will scan your account for any positions that are NOT in the config.py core holdings (FBCG, AVUV, PYLD, JPIE, SGOV).  For each one, the script presents a per-position prompt:

```
  [1] SPY    100.0000 shares  (~$54,000.00)  [L/K/A/Q] (default K):
```

You choose, for each position individually:

- **K (or just press Enter)** — Keep this position.  It remains in the account, untouched.  The RPM will silently ignore it from then on; rebalancing, withdrawals, and circuit-breaker logic only ever touch the four core tickers + SGOV.
- **L** — Liquidate this position now to fund the RPM foundation buys.
- **A** — Liquidate THIS position AND all remaining legacy positions (explicitly opt-in and it asks for a y/N confirmation).
- **Q** — Quit setup entirely.  No trades fire, no state file is written.

If you keep enough legacy assets that the remaining cash can't fund the RPM core at $5,000+ per ticker, setup aborts with a detailed shortfall message before any trades fire — your account is never left in a half-funded state.

Kept positions are completely invisible to the RPM thereafter.  This means you can run the RPM alongside, e.g., a long-term FZROX position you don't want to liquidate, without confusing the rebalancer.

**2. STRICTLY FOR TAX-ADVANTAGED ACCOUNTS**

Because of the Day 1 liquidations, the 5/25 weekly rebalancing bands, and the automated buffer siphoning logic, this system generates a high volume of internal trades. You must only run this inside a tax-advantaged account (e.g., Roth IRA or Traditional IRA). Running this in a standard taxable brokerage account will generate an immediate and continuous nightmare of short-term capital gains and complex tax liabilities.

The RPM is not designed to manage a standard, low-yield 60/40 Boglehead portfolio. If you want to withdraw a standard 4% a year, buy VTI and BND.

The RPM is a specialized engine designed to mathematically sustain exceptionally high withdrawal rates (e.g., 8.0% to 8.5%). To achieve this, it relies on a specific structural architecture that pairs highly volatile growth assets with high-yield active bonds, protected by a massive, isolated cash-equivalent blast shield.  

## Adjusting the initial withdrawal rate

If you want to adjust the initial withdrawal rate up or down, edit `INITIAL_WITHDRAWAL_RATE` in `config.py` (defaults to `0.085`, i.e. 8.5%).

- NOTE:  YOU WILL NEED TO SET UP IBKR TO PULL YOUR DESIRED DRAW VIA _IBKRs_ AUTO ACH BANK TRANSFER-- All RPM will do is free up the cash so you can have a payday, you yourself will need to arrange to get the cash into your bank.
 
The Three-Tier Architecture

By default, the RPM expects to manage six specific assets:

The Blast Shield (Dynamic Calculation)

SGOV (0-3 Month US Treasuries): Sized to exactly 18 months of your target monthly withdrawal. This acts as an isolated battery. During a market crash, the RPM halts all internal trading and pulls your monthly income exclusively from this buffer, giving the core portfolio up to 1.5 years to recover without selling at a loss.

USD (Settled Cash): Sized to 1 month of withdrawal + $1,000 to handle ACH pulls and transaction friction.

The Growth Engine (50% of Core)

FBCG (Blue Chip Growth) & AVUV (Small Cap Value): Highly volatile, massive appreciation potential.

The Income Engine (50% of Core)

PYLD (Active Multi-Sector) & JPIE (Active Preferred/Income): High-yield fixed income to fund the baseline withdrawals during peacetime.

Core Features & Mechanics

5/25 Drift Rebalancing: During peacetime, if any core asset drifts 5% absolute or 25% relative to its target allocation, the system sweeps the excess profits into the underweight assets to maintain the 50/50 equilibrium (T+1 Settlement).

Trend Simple Moving Average (SMA) Circuit Breakers (10-month monthly-bar lookback): The RPM monitors a synthetic equal-weighted index of your Growth assets, built by chaining per-bar percentage returns across the underlying tickers (so a $50 fund and a $500 fund contribute equally per percent moved, regardless of share-price magnitude).  If the index falls 5% below its trend SMA, all rebalancing is halted to prevent selling low.  If it falls 7.5% below, Crisis Mode is triggered, and withdrawals are routed exclusively to the SGOV buffer.

Crisis Mode Cascading Withdrawals: If the 18-month SGOV buffer is exhausted, the cash-raising cascade transitions to pulling from the Fixed Income tier. Because the FI assets (PYLD, JPIE) are untouched during the 1.5-year SGOV drain, their continuous high-yield generation compounds and slows their eventual depletion. Mathematically, a conservative 6% yield allows the FI tier to sustain an 8.5% withdrawal rate for an additional 6.5 years. _Only after an estimated 8 total years of continuous crisis would the system exhaust the FI tier and be forced to cannibalize Growth assets as a last resort._

Per-Position Residual Floor ($1,500): No individual position is ever drawn below $1,500 by the cash-raising cascade.  When a tier hits the residual floor, withdrawals spill to the next tier rather than continuing to drain the position to zero.  This protects cost-basis lots and keeps every position viable for future rebalancing buys.

Buffer Refill Routing: Once the market recovers from a crisis, the system initiates a 12-month automated siphoning sequence, bleeding off core assets to rebuild the 18-month SGOV blast shield.

November Annual Review: Every November, the system evaluates the portfolio against a 12-month SMA. If the market is healthy, it applies a 3% inflation raise to your monthly withdrawal. It also calculates a "Bull Market Bonus"—if the Growth bucket exceeds a 25% YoY return, it extracts 5% of the excess gains as a special cash dividend.

Weekly and Crisis Alerts:  Once a week the user will get a SMS Text and an email showing balances and days to payday.  It will also notify you if there is a crisis.

Annual Inflation Adjustments & The 12-Month Guardrail

To ensure your income keeps pace with the cost of living without jeopardizing the portfolio during extended bear markets, the RPM employs a conditionally gated inflation adjustment:

The Baseline Raise: Every November, the system evaluates your monthly withdrawal target and applies a standard 3% inflation increase, this number will need to be edited if you want more or less of an inflation increase.  You will need to edit the _config.py_ file.
                                               #Annual Inflation Guardrails (November Review)#
                                                   #ANNUAL_INFLATION_RATE = 0.03# 

The Freeze Guardrail: Before applying the raise, the system compares the current price of your Growth proxy index against its 12-month SMA. If the proxy index is down 5% or more relative to its 12-month SMA, the inflation adjustment is frozen.

The Result: During a down year, you will continue to receive your standard monthly withdrawal amount, but the 3% raise is skipped to prevent compounding sequence-of-returns risk and preserve core capital.

Fail-Closed Appliance Design: Uses atomic file saving (rpm_state.json), T+1 cash settlement safety checks, hard-coded transaction limits, and a process-wide file lock (fcntl.flock) to prevent the three systemd timers from racing each other on shared state.  Designed to survive power outages and broker disconnects without executing erroneous trades.

Deployment Guide

The RPM is designed to operate as a headless appliance. It does not use a GUI.

1. Prerequisites

Hardware: A dedicated, low-power Linux machine (e.g., Ubuntu 22.04 LTS on an Intel N100 mini-PC).

Broker: An Interactive Brokers account with a Secondary User created specifically for API access (2FA disabled).

Gateway: IBController (IBC) installed to manage the IB Gateway headless UI and handle the mandatory daily 24-hour broker reset.

2. Installation

git clone https://github.com/DAH3313/rpm.git
cd rpm
pip install -r requirements.txt

3. Day 1 Initialization (The Point of No Return)

Before enabling the automation, configure your targets and run the interactive setup.

Edit `config.py` to set your `INITIAL_WITHDRAWAL_RATE` (e.g., `0.085`).

Run the setup script:

```
python3 setup.py
```

The script connects to IBKR, computes funding targets, and walks you through any non-core positions one at a time — you decide per-position whether to liquidate (`L`), keep (`K`), [the default], liquidate-all (`A`), or quit (`Q`).  Then it displays the full plan (kept legacy assets, queued liquidations, target dollar amounts per ticker) and asks for a final `[y/N]`.

On confirmation it executes the selected SELL orders (verifying each one fills before proceeding), executes the foundation BUY orders for SGOV + the four core tickers, and writes `rpm_state.json` with the schema version and is_live_latched flag set.

4. Arming the Appliance (Systemd)

The RPM is governed by three separate systemd timers to prevent logic overlap and wash trades. See the /systemd/ directory for the template files.

_rpm-weekly.timer_ (Runs every Monday at 9:30 AM EST): Evaluates circuit breakers and handles 5/25 drift rebalancing.

_rpm-monthly.timer_ (Runs 4 days prior to your ACH pull): Raises the exact cash required for your monthly withdrawal and handles the November Annual Review.

_rpm-heartbeat.timer_ (Runs once a week): A dead-man's switch that sends an email/SMS confirming the OS, network, and broker connection are alive.  Provides balance and weight of each core asset and buffer status.  See more detail below.

5.  Alert Architecture & Notification Triggers

The RPM is designed to operate as a silent appliance. It will not generate "routine success" emails for everyday rebalancing or monthly withdrawals. It is configured to communicate exactly once a week to confirm hardware viability and software execution.  It will otherwise only alert you if a Circuit Breaker, an extraordinary threshold, or crisis event is triggered.

Routine Communication

Weekly Heartbeat

  Timing: Every Sunday at 12:00 PM (Noon).
  Trigger: Routine systemd timer.
  Description: A comprehensive snapshot confirming the OS, network, and IBKR Gateway are alive. It includes the upcoming days to your scheduled payday, the status and funding percentage of the SGOV buffer, and the current balance/weighting of the core ETF portfolio.

Circuit Breaker & Crisis Alerts

  Crisis Mode: ACTIVATED
  Timing: Evaluated during scheduled runs (Weekly/Monthly).
  Trigger: The synthetic proxy index of the Growth assets drops 7.5% below its 200-day Simple Moving Average (SMA).
  Description: Alerts that the system has halted internal rebalancing and will route all future monthly withdrawals exclusively to the SGOV buffer to protect core assets.

Crisis Mode: RECOVERY

  Timing: Evaluated during scheduled runs (Weekly/Monthly).
  Trigger: The proxy index recovers to 3% above the price at which the crisis mode was originally activated.
  Description: Alerts that normal operations have resumed and the 60-day recovery clock has started before the system will begin automatically siphoning core assets to refill the SGOV buffer.  60 day delay is to allow Growth assets to recover before a heavy draw begins, historically Growth comes back with a roar after a deep dip.

Execution & Threshold Alerts

  Large Core Rebalance Executed
  Timing: Mondays at 9:30 AM EST (if triggered).
  Trigger: The 5/25 drift bands force an internal reallocation with a total trade volume exceeding $10,000.
  Description: Provides an itemized list of the specific BUY/SELL orders executed to re-establish the 50/50 core equilibrium.

Large Buffer Drawdown

  Timing: Monthly Withdrawal Run (if triggered).
  Trigger: The system pulls more than $10,000 from the SGOV buffer in a single transaction.
  Description: A notification of a significant draw on the crisis buffer.

Annual Bull Market Bonus

  Timing: The November Withdrawal Run (Annually).
  Trigger: The Growth bucket achieves a Year-Over-Year return greater than 25%.
  Description: Alerts that the system has automatically extracted 5% of the excess gains as a special cash dividend added to that month's withdrawal.  A pre-Christmas bonus for having a banner market year.

Cascade Failure Alerts (Emergency)

  Cascade Level 1: Buffer at Residual
  Timing: Monthly Withdrawal Run.
  Trigger: The system is in Crisis Mode and the SGOV buffer has hit its $1,500 per-position residual floor; the cash-raising cascade has spilled into the high-yield Fixed Income bucket to meet the monthly withdrawal requirement.
  Description: Critical alert that the buffer is effectively exhausted (drawn down to its protective residual) and FI is now funding monthly draws.

Cascade Level 2: Fixed Income at Residual

  Timing: Monthly Withdrawal Run.
  Trigger: Both SGOV and the Fixed Income positions have hit their $1,500 per-position residual floors; the cascade has spilled into Growth.
  Description: Severe critical alert that the system has been forced to cannibalize highly volatile Growth assets at potentially depressed prices to meet the withdrawal requirement.

  Hardware & Connection Failures

System Failure / Crash

  Timing: Immediate (upon failure).
  Trigger: The Python script crashes, loses connection to the IBKR Gateway and exhausts retries, or encounters an unhandled exception.
  Description: Sends a failure notification containing the full Python traceback log for debugging.

Setup Pending (Pre-Activation)

  Timing: Once a month (if uninitialized).
  Trigger: The machine is powered on and network-connected, but setup.py has not been run.
  Description: Confirms hardware viability but warns that the RPM is unarmed.

6.  Testing in a Paper Trading Account:

If you want to watch the RPM execute trades with fake money before taking it live, make two changes:
In your config.py, change the port: IBKR_PORT = 4002 (IB Gateway uses 4001 for Live, 4002 for Paper).

In your IBC config.ini, set: TradingMode=paper
