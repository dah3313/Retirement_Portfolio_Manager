# setup.py — Retirement Portfolio Manager (RPM)
# ===============================================
# Interactive CLI tool to initialize the RPM on a new Interactive Brokers account.
# Liquidates non-core assets and establishes the baseline SGOV and Core buckets.

import os
import sys
import time
import json
import logging
from datetime import datetime
from ib_insync import Stock, MarketOrder

import config
from ibkr_client import IBKRClient

# Basic console logging for setup
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger('rpm.setup')

# ANSI colors for the terminal CLI
C_RED = '\033[91m'
C_GREEN = '\033[92m'
C_YELLOW = '\033[93m'
C_CYAN = '\033[96m'
C_RESET = '\033[0m'

def print_header():
    os.system('cls' if os.name == 'nt' else 'clear')
    print(f"{C_CYAN}================================================================={C_RESET}")
    print(f"{C_CYAN}         RETIREMENT PORTFOLIO MANAGER (RPM) - SETUP TOOL         {C_RESET}")
    print(f"{C_CYAN}================================================================={C_RESET}")
    print(f"{C_RED}WARNING: The RPM relies on automated rebalancing and buffer")
    print("siphoning. It is explicitly designed for tax-advantaged accounts")
    print("(Roth IRA / Traditional IRA). Running this in a taxable brokerage")
    print(f"account will generate frequent short-term capital gains.{C_RESET}\n")

def get_tnlv(client):
    """Fetches the Total Net Liquidation Value of the account."""
    for item in client.ib.accountSummary():
        if item.tag == 'NetLiquidation':
            return float(item.value)
    
    # Fallback if NetLiquidation tag is missing
    state_bals = client.get_portfolio_state()
    return sum(state_bals.values())

def save_initial_state(monthly_withdrawal, sgov_target):
    """Creates the initial rpm_state.json file."""
    state = {
        'current_monthly_withdrawal': monthly_withdrawal,
        'in_buffer_transition': False,
        'transition_price': None,
        'last_november_growth_value': 0.0,
        'is_live_latched': True,  # Signals to main.py that setup is complete
        'recovery_date': None,       
        'sgov_target_dollars': sgov_target,
        'last_idle_heartbeat_month': 0  
    }
    
    os.makedirs(os.path.dirname(config.STATE_FILE), exist_ok=True)
    with open(config.STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)
    print(f"{C_GREEN}>> Initial state saved to {config.STATE_FILE}{C_RESET}")

def main():
    print_header()
    print("Connecting to Interactive Brokers Gateway...")
    
    client = IBKRClient()
    try:
        client.connect()
    except Exception as e:
        print(f"{C_RED}Failed to connect to IBKR. Ensure IBC/Gateway is running on port {config.IBKR_PORT}.{C_RESET}")
        sys.exit(1)

    print(f"{C_GREEN}Connected.{C_RESET} Analyzing account...\n")
    client.ib.sleep(2) # Give IBKR a moment to stream account data

    # 1. Fetch TNLV and calculate targets
    tnlv = get_tnlv(client)
    
    if tnlv < 50_000:
        print(f"{C_RED}Error: Account TNLV is ${tnlv:,.2f}. The RPM requires a higher minimum balance to function properly.{C_RESET}")
        client.disconnect()
        sys.exit(1)

    withdrawal_rate = getattr(config, 'INITIAL_WITHDRAWAL_RATE', 0.085)
    buffer_months = getattr(config, 'INITIAL_BUFFER_MONTHS', 18)
    transaction_buffer = getattr(config, 'CASH_TRANSACTION_BUFFER', 1000.0)

    monthly_withdrawal = (tnlv * withdrawal_rate) / 12.0
    sgov_target = monthly_withdrawal * buffer_months
    cash_target = monthly_withdrawal + transaction_buffer
    core_target = tnlv - sgov_target - cash_target

    growth_target = core_target * config.TARGET_ALLOCATION_GROWTH
    fi_target = core_target * config.TARGET_ALLOCATION_FI

    # 2. Present the mathematical plan to the user
    print(f"{C_YELLOW}--- ACCOUNT SNAPSHOT & TARGETS ---{C_RESET}")
    print(f"Total Net Liquidation Value:  ${tnlv:,.2f}")
    print(f"Configured Withdrawal Rate:   {withdrawal_rate * 100:.1f}%")
    print(f"Target Buffer Size:           {buffer_months} Months")
    print("-" * 34)
    print(f"Monthly Withdrawal:           ${monthly_withdrawal:,.2f}")
    print(f"Base Cash Buffer (USD):       ${cash_target:,.2f}")
    print(f"SGOV Crisis Buffer:           ${sgov_target:,.2f}")
    print(f"Core Portfolio Funding:       ${core_target:,.2f}")
    print(f"  -> Growth Allocation:       ${growth_target:,.2f}")
    print(f"  -> Fixed Income Allocation: ${fi_target:,.2f}\n")

    # 3. Identify assets to liquidate
    positions = client.ib.positions()
    approved_symbols = config.CORE_TICKERS + [config.TICKER_BUFFER, getattr(config, 'CASH_TICKER', 'USD')]
    to_liquidate = []

    for pos in positions:
        if pos.contract.symbol not in approved_symbols and pos.position > 0:
            to_liquidate.append((pos.contract.symbol, pos.position))

    if to_liquidate:
        print(f"{C_YELLOW}--- LIQUIDATION REQUIRED ---{C_RESET}")
        print("The following legacy assets will be sold at market price to fund the RPM:")
        for symbol, qty in to_liquidate:
            print(f"  - {qty} shares of {symbol}")
        print()
    else:
        print(f"{C_GREEN}No legacy assets require liquidation.{C_RESET}\n")

    # 4. The Point of No Return
    print(f"{C_RED}This action will execute live market orders on your Interactive Brokers account.{C_RESET}")
    confirm = input(f"Proceed with initialization? [y/N]: ").strip().lower()

    if confirm != 'y':
        print("\nSetup aborted by user. No trades executed.")
        client.disconnect()
        sys.exit(0)

    print(f"\n{C_CYAN}Executing Initialization...{C_RESET}")

    # 5. Liquidate
    if to_liquidate:
        for symbol, qty in to_liquidate:
            print(f"Selling {qty} shares of {symbol}...")
            contract = Stock(symbol, 'SMART', 'USD')
            client.ib.qualifyContracts(contract)
            trade = client.ib.placeOrder(contract, MarketOrder('SELL', qty))
            
            elapsed = 0
            while not trade.isDone() and elapsed < 60:
                client.ib.waitOnUpdate(timeout=2)
                elapsed += 2
        
        print("Waiting 5 seconds for internal cash settlement reflection...")
        client.ib.sleep(5)

    # 6. Foundation Buys
    print(f"{C_CYAN}Establishing RPM Foundation...{C_RESET}")
    
    # SGOV Buy
    print(f"Buying ${sgov_target:,.2f} of {config.TICKER_BUFFER} (Buffer)...")
    client.buy_dollar_amount(config.TICKER_BUFFER, sgov_target)

    # Core Buys
    growth_per_ticker = growth_target / len(config.TICKERS_GROWTH)
    for ticker in config.TICKERS_GROWTH:
        print(f"Buying ${growth_per_ticker:,.2f} of {ticker} (Growth)...")
        client.buy_dollar_amount(ticker, growth_per_ticker)

    fi_per_ticker = fi_target / len(config.TICKERS_FI)
    for ticker in config.TICKERS_FI:
        print(f"Buying ${fi_per_ticker:,.2f} of {ticker} (Fixed Income)...")
        client.buy_dollar_amount(ticker, fi_per_ticker)

    # 7. Finalize
    save_initial_state(monthly_withdrawal, sgov_target)
    
    print(f"\n{C_GREEN}================================================================={C_RESET}")
    print(f"{C_GREEN}SETUP COMPLETE.{C_RESET}")
    print("The RPM is now fully funded and armed. You may now enable the")
    print("systemd timers to begin automated lifecycle management.")
    print(f"{C_GREEN}================================================================={C_RESET}")

    client.disconnect()

if __name__ == '__main__':
    main()