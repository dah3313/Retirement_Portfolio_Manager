##Retirement Portfolio Manager (RPM)

#This whole project from conception on was predicated on a high-yield multi sector bonds backend bolted on to a high volatility high yield front end.  The logic structure is designed to provide reliable income at via a high withdrawal rate 8.5% relative to the starting portfolio total balance while preventing damage to Growth assets in a market downturn and to harvest the high volatility growth assets while providing for a thick buffer (SGOV+FI) thus de-risking the withdrawal schema to ride out extended market rough patches.  I can't claim any credit for the code itself, Gemini and Claude did all the coding.  The ideas are my own.

The RPM is a stateless, rules-based, high-velocity decumulation appliance designed to run autonomously on a headless Linux server. It connects directly to an Interactive Brokers (IBKR) account to manage a specialized retirement portfolio.
⚠️ CRITICAL WARNINGS: READ BEFORE CLONING ⚠️

#1. THE DAY-1 BULLDOZER (MASS LIQUIDATION)

To function mathematically, the RPM requires absolute control over the portfolio's asset allocation. When you run setup.py to initialize the system, it will automatically issue market SELL orders for every single asset in your account that is not explicitly defined in the config.py core holdings. It does not care about your cost basis. It will liquidate your legacy stocks, mutual funds, and old ETFs to cash, and use that cash to build its required foundation.

#2. STRICTLY FOR TAX-ADVANTAGED ACCOUNTS

Because of the Day 1 mass liquidation, the 5/25 weekly rebalancing bands, and the automated buffer siphoning logic, this system generates a high volume of internal trades. You must only run this inside a tax-advantaged account (e.g., Roth IRA or Traditional IRA). Running this in a standard taxable brokerage account will generate an immediate and continuous nightmare of short-term capital gains and complex tax liabilities.
The Philosophy: High-Velocity Decumulation

The RPM is not designed to manage a standard, low-yield 60/40 Boglehead portfolio. If you want to withdraw a safe 4% a year, buy VTI and BND and manage it yourself.

The RPM is a specialized engine designed to mathematically sustain exceptionally high withdrawal rates (e.g., 8.0% to 8.5%). To achieve this, it relies on a specific structural architecture that pairs highly volatile growth assets with high-yield active bonds, protected by a massive, isolated cash-equivalent blast shield.
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

200-Day SMA Circuit Breakers: The RPM monitors a synthetic proxy index of your Growth assets. If the proxy falls 5% below its 200-day Simple Moving Average (SMA), all rebalancing is halted to prevent selling low. If it falls 7.5% below, Crisis Mode is triggered, and withdrawals are routed exclusively to the SGOV buffer.

5/25 Drift Rebalancing: During peacetime, if any core asset drifts 5% absolute or 25% relative to its target allocation, the system sweeps the excess profits into the underweight assets to maintain the 50/50 equilibrium (T+1 Settlement).

Buffer Refill Routing: Once the market recovers from a crisis, the system initiates a 12-month automated siphoning sequence, bleeding off core assets to rebuild the 18-month SGOV blast shield.

November Annual Review: Every November, the system evaluates the portfolio against a 12-month SMA. If the market is healthy, it applies a 3% inflation raise to your monthly withdrawal. It also calculates a "Bull Market Bonus"—if the Growth bucket exceeds a 25% YoY return, it extracts 5% of the excess gains as a special cash dividend.

Fail-Closed Appliance Design: Uses atomic file saving (rpm_state.json), T+1 cash settlement safety checks, and hard-coded transaction limits to ensure it can survive power outages and broker disconnects without executing erroneous trades.

Deployment Guide

The RPM is designed to operate as a headless appliance. It does not use a GUI.
1. Prerequisites

Hardware: A dedicated, low-power Linux machine (e.g., Ubuntu 22.04 LTS on an Intel N100 mini-PC).

Broker: An Interactive Brokers account with a Secondary User created specifically for API access (2FA disabled).

Gateway: IBController (IBC) installed to manage the IB Gateway headless UI and handle the mandatory daily 24-hour broker reset.

2. Installation

git clone https://github.com/yourusername/rpm.git
cd rpm
pip install -r requirements.txt

3. Day 1 Initialization (The Point of No Return)

Before enabling the automation, you must configure your targets and execute the mass liquidation.

Edit config.py to set your INITIAL_WITHDRAWAL_RATE (e.g., 0.085).

Run the setup script:

python3 setup.py

The script will calculate your targets, explicitly list the legacy assets it is about to liquidate, and ask for a [y/N] confirmation.

Upon confirmation, it executes the trades, builds the foundational buckets, and locks the rpm_state.json file.

4. Arming the Appliance (Systemd)

The RPM is governed by three separate systemd timers to prevent logic overlap and wash trades. See the /systemd/ directory for the template files.

rpm-weekly.timer (Runs every Monday at 9:30 AM EST): Evaluates circuit breakers and handles 5/25 drift rebalancing.

rpm-monthly.timer (Runs 4 days prior to your ACH pull): Raises the exact cash required for your monthly withdrawal and handles the November Annual Review.

rpm-heartbeat.timer (Runs every 6 hours): A dead-man's switch that sends an email/SMS confirming the OS, network, and broker connection are alive.

5.  Testing in a Paper Trading Account:

If you want to watch the RPM execute trades with fake money before taking it live, make two changes:
In your config.py, change the port: IBKR_PORT = 4002 (IB Gateway uses 4001 for Live, 4002 for Paper).

In your IBC config.ini, set: TradingMode=paper