# setup.py — Retirement Portfolio Manager (RPM)
# ===============================================
# Interactive CLI tool to initialize the RPM on a new Interactive Brokers account.
#
# Establishes the baseline SGOV and Core buckets.  Per-asset interactive
# liquidation picker (Session 5): operator chooses which non-RPM
# legacy positions get liquidated to fund the RPM, on a per-position
# basis.  Pre-Session-5 this was a "Day-1 bulldozer" that liquidated
# every non-CORE position automatically with a single y/N prompt;
# Session 5 replaces that with an opt-IN-per-position model so the
# operator can keep specific positions (e.g., long-term tax-deferred
# holdings, illiquid lots, or simply assets the operator wants to
# retain alongside the RPM).
#
# Kept legacy assets are SILENTLY IGNORED by the RPM thereafter.  The
# weekly/monthly logic in portfolio.py only looks at TICKERS_GROWTH,
# TICKERS_FI, and TICKER_BUFFER, so a legacy SPY position will sit
# undisturbed alongside the RPM core.  This is intentional and was
# verified during the Session 5 design pass.

import os
import sys
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
C_GREY = '\033[90m'
C_RESET = '\033[0m'

# The picker requires that the post-liquidation Core funding (split
# 50/50 across Growth + FI tickers) be at least this much PER
# TICKER.  Below this floor, the math just doesn't make sense:
# trades become rounding errors and the residual cascade has
# nothing to draw from.  Operator must keep less or rerun with a
# different account.
MIN_PER_TICKER_FUNDING_USD = 5_000.0


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


def gather_legacy_market_values(client, legacy_positions):
    """
    Given [(symbol, qty), ...], queries IBKR for current market prices
    and returns [(symbol, qty, market_value_usd), ...].  Falls back to
    last-close if marketPrice is NaN (typical at off-hours).

    Order is preserved.  If pricing fails entirely for a symbol, that
    entry's market_value will be 0.0 and the prompt will flag it as
    "price unknown".
    """
    if not legacy_positions:
        return []

    # Build contract list in the same order
    contracts = []
    for symbol, _ in legacy_positions:
        c = Stock(symbol, 'SMART', 'USD')
        contracts.append(c)

    # Qualify and fetch
    try:
        client.ib.qualifyContracts(*contracts)
        tickers = client.ib.reqTickers(*contracts)
    except Exception as e:
        logger.warning('Failed to fetch legacy market values: %s', e)
        return [(s, q, 0.0) for s, q in legacy_positions]

    # Map ticker symbol -> price
    import math as _math
    price_map = {}
    for t in tickers:
        try:
            sym = t.contract.symbol
            p = t.marketPrice()
            if p is None or _math.isnan(p):
                p = t.close
            if p is None or _math.isnan(p):
                p = 0.0
            price_map[sym] = float(p)
        except Exception:
            continue

    out = []
    for symbol, qty in legacy_positions:
        price = price_map.get(symbol, 0.0)
        out.append((symbol, qty, qty * price))
    return out


def prompt_legacy_decisions(legacy_with_values):
    """
    Per-position prompt.  Defaults to KEEP if the operator presses
    Enter.  Returns (to_liquidate, to_keep) where each is a list of
    (symbol, qty, market_value_usd) tuples in the same order they
    were prompted.

    Operator can also enter:
      'a'  liquidate ALL remaining (the original bulldozer mode,
           explicitly opt-in)
      'q'  abort the entire setup (no trades; no state file written)

    Pre-Session-5 the prompt was a single y/N that liquidated
    everything without distinction.  The new design is a deliberate
    speed bump: the operator must consciously approve each
    liquidation (or use 'a' as an explicit batch override).
    """
    print(f"{C_YELLOW}--- LEGACY POSITION REVIEW ---{C_RESET}")
    print(f"Found {len(legacy_with_values)} legacy "
          f"position{'s' if len(legacy_with_values) != 1 else ''}:\n")

    for i, (sym, qty, mv) in enumerate(legacy_with_values, 1):
        if mv > 0:
            print(f"  [{i}] {sym:<6}  {qty:>10.4f} shares  "
                  f"(~${mv:>12,.2f})")
        else:
            print(f"  [{i}] {sym:<6}  {qty:>10.4f} shares  "
                  f"({C_GREY}price unknown{C_RESET})")

    total_mv = sum(mv for _, _, mv in legacy_with_values)
    print(f"\n  {C_GREY}Total estimated value: ${total_mv:,.2f}{C_RESET}\n")

    print(f"For each position, choose: {C_GREEN}[L]iquidate{C_RESET} | "
          f"{C_GREEN}[K]eep (default){C_RESET} | "
          f"{C_RED}[A]ll-liquidate{C_RESET} | {C_RED}[Q]uit{C_RESET}")
    print(f"{C_GREY}(K and Enter both keep; A liquidates this AND all "
          f"remaining){C_RESET}\n")

    to_liquidate = []
    to_keep = []
    auto_liquidate_rest = False

    for sym, qty, mv in legacy_with_values:
        if auto_liquidate_rest:
            to_liquidate.append((sym, qty, mv))
            print(f"  {sym:<6} -> {C_RED}LIQUIDATE{C_RESET} (auto)")
            continue

        # Loop until valid input
        while True:
            mv_str = f"~${mv:,.2f}" if mv > 0 else "price unknown"
            prompt = (
                f"  {sym:<6} ({mv_str:<20}) "
                f"[L/K/A/Q] (default K): "
            )
            ans = input(prompt).strip().lower()
            if ans in ('', 'k', 'keep'):
                to_keep.append((sym, qty, mv))
                print(f"    {C_GREEN}-> KEEP{C_RESET}")
                break
            elif ans in ('l', 'liquidate'):
                to_liquidate.append((sym, qty, mv))
                print(f"    {C_RED}-> LIQUIDATE{C_RESET}")
                break
            elif ans in ('a', 'all'):
                # Confirm the bulldozer before flipping the flag
                confirm = input(
                    f"    {C_RED}Liquidate {sym} AND all remaining "
                    f"legacy positions? [y/N]: {C_RESET}"
                ).strip().lower()
                if confirm == 'y':
                    to_liquidate.append((sym, qty, mv))
                    print(f"    {C_RED}-> LIQUIDATE (auto-rest enabled)"
                          f"{C_RESET}")
                    auto_liquidate_rest = True
                    break
                else:
                    print(f"    {C_GREY}(cancelled, choose again){C_RESET}")
                    continue
            elif ans in ('q', 'quit'):
                print(f"\n{C_RED}Setup aborted by user during legacy "
                      f"review.{C_RESET}")
                sys.exit(0)
            else:
                print(f"    {C_GREY}(unrecognized; please type L, K, A, "
                      f"or Q){C_RESET}")

    print()
    return to_liquidate, to_keep


def save_initial_state(monthly_withdrawal, sgov_target):
    """Creates the initial rpm_state.json file."""
    # schema_version stamped at write time so main.load_state()
    # accepts the file without triggering the Session-4 migration
    # check.  Setup is by definition a fresh start, so we always
    # write the current version.
    state = {
        'schema_version': config.STATE_SCHEMA_VERSION,
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
        print(f"{C_RED}Failed to connect to IBKR. Ensure IBC/Gateway is "
              f"running on port {config.IBKR_PORT}.{C_RESET}")
        sys.exit(1)

    print(f"{C_GREEN}Connected.{C_RESET} Analyzing account...\n")
    client.ib.sleep(2)  # Give IBKR a moment to stream account data

    # 1. Fetch TNLV (the funding ceiling)
    tnlv = get_tnlv(client)

    if tnlv < 50_000:
        print(f"{C_RED}Error: Account TNLV is ${tnlv:,.2f}. The RPM "
              f"requires a higher minimum balance to function "
              f"properly.{C_RESET}")
        client.disconnect()
        sys.exit(1)

    withdrawal_rate = getattr(config, 'INITIAL_WITHDRAWAL_RATE', 0.085)
    buffer_months = getattr(config, 'INITIAL_BUFFER_MONTHS', 18)
    transaction_buffer = getattr(config, 'CASH_TRANSACTION_BUFFER', 1000.0)

    # 2. Identify legacy assets (anything not core/buffer/cash)
    positions = client.ib.positions()
    approved_symbols = (
        config.CORE_TICKERS
        + [config.TICKER_BUFFER, getattr(config, 'CASH_TICKER', 'USD')]
    )
    legacy_positions = []
    for pos in positions:
        sym = pos.contract.symbol
        if sym not in approved_symbols and pos.position > 0:
            legacy_positions.append((sym, pos.position))

    # 3. Fetch market values + run the picker
    if legacy_positions:
        legacy_with_values = gather_legacy_market_values(
            client, legacy_positions,
        )
        to_liquidate, to_keep = prompt_legacy_decisions(legacy_with_values)
    else:
        print(f"{C_GREEN}No legacy assets present.{C_RESET}\n")
        to_liquidate = []
        to_keep = []

    # 4. Recompute the funding plan with the picker's decisions in mind.
    # available_for_rpm = TNLV minus the value of assets we're keeping.
    # The kept assets stay in the account but are invisible to the
    # RPM logic thereafter (portfolio.py only sums approved tickers).
    kept_value = sum(mv for _, _, mv in to_keep)
    available_for_rpm = tnlv - kept_value

    monthly_withdrawal = (available_for_rpm * withdrawal_rate) / 12.0
    sgov_target = monthly_withdrawal * buffer_months
    cash_target = monthly_withdrawal + transaction_buffer
    core_target = available_for_rpm - sgov_target - cash_target

    growth_target = core_target * config.TARGET_ALLOCATION_GROWTH
    fi_target = core_target * config.TARGET_ALLOCATION_FI

    # 5. Funding sanity check.  If the operator kept so much that the
    # core can't be split into meaningful per-ticker allocations, abort
    # and explain why.  $5k/ticker is the floor; below that, the
    # residual cascade ($1,500 per position) leaves only $3,500
    # operating range per ticker, which is too tight.
    n_growth_tickers = len(config.TICKERS_GROWTH)
    n_fi_tickers = len(config.TICKERS_FI)
    growth_per_ticker = (
        growth_target / n_growth_tickers if n_growth_tickers else 0
    )
    fi_per_ticker = (
        fi_target / n_fi_tickers if n_fi_tickers else 0
    )

    if (core_target <= 0
            or growth_per_ticker < MIN_PER_TICKER_FUNDING_USD
            or fi_per_ticker < MIN_PER_TICKER_FUNDING_USD):
        print(f"\n{C_RED}=================================================================")
        print("FUNDING SHORTFALL — CANNOT INITIALIZE RPM")
        print("=================================================================")
        print(f"After keeping ${kept_value:,.2f} of legacy positions, the")
        print(f"remaining ${available_for_rpm:,.2f} is not enough to fund the")
        print(f"RPM's three-tier architecture at the target withdrawal rate")
        print(f"({withdrawal_rate * 100:.1f}%) and buffer size ({buffer_months} months):")
        print()
        print(f"  Monthly withdrawal:        ${monthly_withdrawal:,.2f}")
        print(f"  SGOV buffer target:        ${sgov_target:,.2f}")
        print(f"  Cash transaction target:   ${cash_target:,.2f}")
        print(f"  Core funding remaining:    ${core_target:,.2f}")
        print(f"    Growth per ticker:       ${growth_per_ticker:,.2f}")
        print(f"    FI per ticker:           ${fi_per_ticker:,.2f}")
        print(f"  Required floor per ticker: ${MIN_PER_TICKER_FUNDING_USD:,.2f}")
        print()
        print("Options:")
        print("  - Re-run setup and liquidate more of your legacy positions, OR")
        print("  - Lower INITIAL_WITHDRAWAL_RATE or INITIAL_BUFFER_MONTHS")
        print("    in config.py, OR")
        print("  - Add more cash to the account before re-running setup.")
        print(f"================================================================={C_RESET}")
        client.disconnect()
        sys.exit(2)

    # 6. Present the funding plan
    print(f"{C_YELLOW}--- ACCOUNT SNAPSHOT & RPM TARGETS ---{C_RESET}")
    print(f"Total Net Liquidation Value:    ${tnlv:,.2f}")
    if to_keep:
        print(f"  Legacy positions kept:        ${kept_value:,.2f}")
        for sym, qty, mv in to_keep:
            print(f"    {sym:<6} {qty:>10.4f} sh  ~${mv:>12,.2f}")
        print(f"  Available for RPM funding:    ${available_for_rpm:,.2f}")
    print(f"Configured Withdrawal Rate:     {withdrawal_rate * 100:.1f}%")
    print(f"Target Buffer Size:             {buffer_months} months")
    print("-" * 36)
    print(f"Monthly Withdrawal:             ${monthly_withdrawal:,.2f}")
    print(f"Base Cash Buffer (USD):         ${cash_target:,.2f}")
    print(f"SGOV Crisis Buffer:             ${sgov_target:,.2f}")
    print(f"Core Portfolio Funding:         ${core_target:,.2f}")
    print(f"  -> Growth Allocation:         ${growth_target:,.2f}")
    for t in config.TICKERS_GROWTH:
        print(f"       {t:<6}: ${growth_per_ticker:,.2f}")
    print(f"  -> Fixed Income Allocation:   ${fi_target:,.2f}")
    for t in config.TICKERS_FI:
        print(f"       {t:<6}: ${fi_per_ticker:,.2f}\n")

    if to_liquidate:
        print(f"{C_YELLOW}--- LIQUIDATIONS QUEUED ---{C_RESET}")
        liquidation_total = sum(mv for _, _, mv in to_liquidate)
        for sym, qty, mv in to_liquidate:
            print(f"  - {qty:>10.4f} shares of {sym} (~${mv:,.2f})")
        print(f"  {C_GREY}Total: ${liquidation_total:,.2f}{C_RESET}\n")

    # 7. The Point of No Return
    print(f"{C_RED}This action will execute live market orders on your "
          f"Interactive Brokers account.{C_RESET}")
    confirm = input(f"Proceed with initialization? [y/N]: ").strip().lower()

    if confirm != 'y':
        print("\nSetup aborted by user. No trades executed.")
        client.disconnect()
        sys.exit(0)

    print(f"\n{C_CYAN}Executing Initialization...{C_RESET}")

    # 8. Liquidate (only the assets the operator selected)
    # Track any sale that did NOT confirm Filled within the timeout.
    # Pre-Session-3 setup.py merely waited 60s and then proceeded
    # into the foundation buys regardless of whether the SELL
    # actually filled.  A rejected or stuck SELL therefore caused
    # the script to issue BUYs against cash that wasn't actually
    # settled — IBKR margin would cover it transiently and then the
    # position would unwind awkwardly.  Now we explicitly check
    # trade.orderStatus.status == 'Filled' and abort the foundation
    # buys if any sale didn't fill, leaving the operator to
    # investigate manually.
    failed_sales = []
    if to_liquidate:
        for sym, qty, _mv in to_liquidate:
            print(f"Selling {qty} shares of {sym}...")
            contract = Stock(sym, 'SMART', 'USD')
            client.ib.qualifyContracts(contract)
            trade = client.ib.placeOrder(contract, MarketOrder('SELL', qty))

            elapsed = 0
            while not trade.isDone() and elapsed < 60:
                client.ib.waitOnUpdate(timeout=2)
                elapsed += 2

            status = trade.orderStatus.status
            if status != 'Filled':
                # Cancel the dangling order so it can't fire later
                # while we're constructing the foundation buys.
                print(
                    f"{C_RED}WARNING: SELL {qty} {sym} did not fill "
                    f"within 60s (status={status}). Canceling order."
                    f"{C_RESET}"
                )
                try:
                    client.ib.cancelOrder(trade.order)
                    client.ib.sleep(2)
                except Exception as e:
                    print(
                        f"{C_RED}  ...also failed to cancel order: "
                        f"{e}{C_RESET}"
                    )
                failed_sales.append((sym, qty, status))
            else:
                print(f"{C_GREEN}  ...Filled.{C_RESET}")

        print("Waiting 5 seconds for internal cash settlement reflection...")
        client.ib.sleep(5)

    if failed_sales:
        print(f"\n{C_RED}=================================================================")
        print("LIQUIDATION INCOMPLETE — ABORTING FOUNDATION BUYS")
        print("=================================================================")
        print("The following SELL orders did not confirm as Filled:")
        for sym, qty, status in failed_sales:
            print(f"  - {qty} shares of {sym} (final status: {status})")
        print(
            "\nNo SGOV/Core buys have been issued and rpm_state.json\n"
            "has NOT been written.  Investigate the failed sales in\n"
            "TWS/IB Gateway, resolve the positions manually, and\n"
            "re-run setup.py from a clean account state."
        )
        print(f"================================================================={C_RESET}")
        client.disconnect()
        sys.exit(2)

    # 9. Foundation Buys
    print(f"{C_CYAN}Establishing RPM Foundation...{C_RESET}")

    # SGOV Buy
    print(f"Buying ${sgov_target:,.2f} of {config.TICKER_BUFFER} (Buffer)...")
    client.buy_dollar_amount(config.TICKER_BUFFER, sgov_target)

    # Core Buys
    for ticker in config.TICKERS_GROWTH:
        print(f"Buying ${growth_per_ticker:,.2f} of {ticker} (Growth)...")
        client.buy_dollar_amount(ticker, growth_per_ticker)

    for ticker in config.TICKERS_FI:
        print(f"Buying ${fi_per_ticker:,.2f} of {ticker} (Fixed Income)...")
        client.buy_dollar_amount(ticker, fi_per_ticker)

    # 10. Finalize
    save_initial_state(monthly_withdrawal, sgov_target)

    print(f"\n{C_GREEN}================================================================={C_RESET}")
    print(f"{C_GREEN}SETUP COMPLETE.{C_RESET}")
    if to_keep:
        print(f"Note: {len(to_keep)} legacy position"
              f"{'s' if len(to_keep) != 1 else ''} "
              f"(${kept_value:,.2f}) will remain in the account "
              f"alongside the RPM.")
        print("The RPM will silently ignore them in all subsequent")
        print("rebalancing, withdrawal, and circuit-breaker logic.")
    print("The RPM is now fully funded and armed. You may now enable the")
    print("systemd timers to begin automated lifecycle management.")
    print(f"{C_GREEN}================================================================={C_RESET}")

    client.disconnect()


if __name__ == '__main__':
    main()
