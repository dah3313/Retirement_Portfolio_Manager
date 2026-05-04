# main.py — Retirement Portfolio Manager (RPM)
# ============================================
# Top-level orchestrator.
#
# Usage:
#   python main.py --weekly         # Evaluates circuit breakers and 5/25 drift
#   python main.py --monthly        # Evaluates circuit breakers and raises cash
#   python main.py --heartbeat      # Sends a heartbeat alert and exits
#   python main.py --dry-run        # Force dry-run mode, executing no trades

import argparse
import datetime
import errno
import fcntl
import json
import logging
import os
import sys
import tempfile

from ib_insync import Stock, MarketOrder

import config
from alert import AlertManager
from ibkr_client import IBKRClient
from portfolio import Portfolio
from strategy import Strategy

# ------------------------------------------------------------------
# Logging setup
# ------------------------------------------------------------------
os.makedirs(config.LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s — %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(config.LOG_DIR, config.LOG_FILE)),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger('rpm.main')


# ------------------------------------------------------------------
# Process-wide lock
# ------------------------------------------------------------------
# RPM is invoked from THREE separate systemd timers (weekly,
# monthly, heartbeat) that all read and write rpm_state.json.
# Although they're scheduled at different times, edge cases CAN
# overlap them (Sunday weekly + Sunday-noon heartbeat falling on a
# 15th that is a Monday before the Friday-monthly window, or any
# manually-triggered run while a scheduled run is mid-execution).
# Two concurrent runs reading the same state file, mutating it in
# different ways, and racing to atomic-replace it is a state
# corruption hazard.
#
# The fix is a simple OS-level advisory file lock (flock) held for
# the entire duration of run_rpm() and the heartbeat path.  The
# lock file is SEPARATE from the state file — acquiring a lock on
# the state file itself would interact awkwardly with the
# tempfile+replace atomic-save pattern (the rename effectively
# breaks the lock anyway, but more importantly: locking the file
# we are about to replace is asking for trouble).
#
# Behavior:
#   - Non-blocking acquire.  If a second process tries to start
#     while one is holding the lock, the second exits cleanly
#     with a log message and exit code 0.  Skipping a run is
#     always safer than corrupting state.
#   - The lock file lives next to the state file; if the state
#     directory doesn't exist yet, we create it (parity with
#     save_state).
#   - flock is Linux/macOS only (RPM's documented deployment
#     target is Linux — the README explicitly calls out 'a
#     dedicated, low-power Linux machine').  On Windows the
#     import-fcntl line would already have failed earlier.
LOCK_FILE = os.path.join(
    os.path.dirname(config.STATE_FILE), 'rpm.lock'
)


class _ProcessLock:
    """Context manager around fcntl.flock.

    Usage:
        try:
            with _ProcessLock():
                ...do work...
        except _LockBusyError:
            sys.exit(0)
    """

    def __enter__(self):
        os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
        # Open with O_CREAT so the lock file is created if absent.
        # Mode 0o644: readable by anyone, writable by owner only —
        # the lock file contains nothing sensitive (the PID of the
        # holder, written below) and being world-readable helps an
        # operator diagnose stuck-lock situations.
        self._fd = os.open(
            LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644
        )
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(self._fd)
            self._fd = None
            if e.errno in (errno.EAGAIN, errno.EACCES):
                raise _LockBusyError() from None
            raise
        # Truncate and write our PID for debugging.  Holder of the
        # lock can be identified with `cat rpm.lock`.
        os.ftruncate(self._fd, 0)
        os.write(
            self._fd,
            f"{os.getpid()}\n".encode('ascii'),
        )
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        # Note: we intentionally do NOT delete the lock file on
        # exit.  Leaving the file present is harmless (flock
        # operates on the inode, not the path) and saves the
        # next run from re-creating it.  Deleting would also race
        # with another process's open()+flock() if they happened
        # to come in right as we exit.
        return False  # don't swallow exceptions


class _LockBusyError(Exception):
    """Raised when another RPM process already holds the lock."""
    pass


# ------------------------------------------------------------------
# Structured audit log (append-only JSONL)
# ------------------------------------------------------------------
def audit_log(event_type, data):
    """Append a structured JSON record to the audit trail."""
    record = {
        'timestamp': datetime.datetime.now().isoformat(),
        'event': event_type,
        **data,
    }
    path = os.path.join(config.LOG_DIR, config.AUDIT_FILE)
    try:
        with open(path, 'a') as f:
            f.write(json.dumps(record) + '\n')
    except Exception as e:
        logger.error('Failed to write audit log: %s', e)


# ------------------------------------------------------------------
# Persistent state & Dynamic Config
# ------------------------------------------------------------------
# Session 4 schema bump: pre-Session-4 the synthetic index was a
# price blend (so transition_price was a dollar share price, e.g.
# ~$45), and post-Session-4 it's a returns-chained level series
# anchored at 100.  An rpm_state.json file written by pre-Session-4
# code with in_buffer_transition=True will have a transition_price
# in the dollar regime; reading it with the new code would compare
# it against a level near 100 and fire "recovery" instantly.  We
# stamp every state file with config.STATE_SCHEMA_VERSION on save
# and refuse to load anything older while in crisis mode — the
# operator must clear state manually if upgrading mid-crisis.

def load_state():
    if os.path.exists(config.STATE_FILE):
        with open(config.STATE_FILE, 'r') as f:
            state = json.load(f)

        # Schema-version check.  See the comment above for why.
        ver = state.get('schema_version', 1)
        if ver < config.STATE_SCHEMA_VERSION:
            if state.get('in_buffer_transition'):
                logger.critical(
                    'STATE SCHEMA MISMATCH (file=%s, schema=%d, expected=%d) '
                    'and crisis mode is active.  The transition_price=%s '
                    'is on the pre-Session-4 share-price scale and is '
                    'incompatible with the post-Session-4 returns-chained '
                    'index level.  Clear state manually (recommended: '
                    'archive the state file, then `rm %s`, then re-run '
                    'setup.py to rebuild).  Aborting.',
                    config.STATE_FILE, ver, config.STATE_SCHEMA_VERSION,
                    state.get('transition_price'), config.STATE_FILE,
                )
                sys.exit(2)
            else:
                # Not in crisis: just upgrade silently.  All other
                # state fields are dollar values, dates, or flags
                # whose semantics didn't change.
                logger.info(
                    'Upgrading state file schema %d -> %d (not in crisis; '
                    'safe in-place migration)',
                    ver, config.STATE_SCHEMA_VERSION,
                )
                state['schema_version'] = config.STATE_SCHEMA_VERSION
        return state

    return {
        'schema_version': config.STATE_SCHEMA_VERSION,
        'current_monthly_withdrawal': 0.0,
        'in_buffer_transition': False,
        'transition_price': None,
        'last_november_growth_value': 0.0,
        'is_live_latched': False,
        'recovery_date': None,
        'sgov_target_dollars': 0.0,
        'last_idle_heartbeat_month': 0
    }

def save_state(state):
    os.makedirs(os.path.dirname(config.STATE_FILE), exist_ok=True)

    # Stamp the schema version on every save so future code can
    # detect older files and migrate or refuse appropriately.
    state['schema_version'] = config.STATE_SCHEMA_VERSION

    fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(config.STATE_FILE))
    with os.fdopen(fd, 'w') as f:
        json.dump(state, f, indent=2)

    os.replace(temp_path, config.STATE_FILE)
    logger.info('State saved atomically to %s', config.STATE_FILE)

def apply_dynamic_config(state):
    """
    Inject dynamically derived targets into the config module so
    downstream modules read the correct runtime values.

    Specifically: portfolio.py and ibkr_client.py both reference
    config.CASH_BUFFER_TARGET (the operational cash floor below
    which we don't deploy cash for rebalancing).  That floor is
    derived from the operator's CURRENT monthly withdrawal, which
    can change year-over-year via the November inflation review.
    Hard-coding it in config.py would mean editing the file
    annually; instead, we mutate the config module at runtime
    here, once per main.py invocation, so all subsequent imports
    in this process see the correct value.

    Why mutate the module rather than passing the value through
    everywhere?  Two reasons:
      - The number is genuinely process-global; every code path
        that does cash math wants the same answer.  Threading
        it through call signatures everywhere would be noisy.
      - config has a sane fallback default (CASH_TRANSACTION_BUFFER)
        for the rare case where state hasn't been populated yet
        (e.g., a heartbeat run before setup completes).  The
        runtime mutation only fires when there's a real number
        to apply.
    """
    withdrawal = state.get('current_monthly_withdrawal', 0.0)
    if withdrawal > 0:
        transaction_buffer = getattr(config, 'CASH_TRANSACTION_BUFFER', 1000.0)
        config.CASH_BUFFER_TARGET = withdrawal + transaction_buffer
        logger.info("Dynamic config applied: CASH_BUFFER_TARGET = $%.2f", config.CASH_BUFFER_TARGET)


# ------------------------------------------------------------------
# Core execution
# ------------------------------------------------------------------
def run_rpm(is_weekly=False, is_monthly=False, cmd_line_dry_run=False, alerter=None):
    """
    Single execution path for both weekly and monthly runs.

    Phases (each section delimited by a numbered comment below):
      1. Gather live data — connect, snapshot positions, build
         a Portfolio object.
      2. Fetch synthetic-index SMA data — two windows: trend
         (weekly circuit-breaker) and 12-month (annual review).
      3. Evaluate circuit breakers — always runs, regardless of
         is_weekly/is_monthly.  Sets in_buffer_transition,
         transition_price, recovery_date.  Fires crisis alerts.
      4. Weekly routine — drift rebalancing + buffer refill (if
         post-recovery clock has elapsed).
      5. Monthly routine — cash raising for the upcoming ACH
         pull, with November handling for inflation + bonus.
      6. Save state.

    Crisis-related state transitions are handled in phase 3:
      - Entering crisis: clear recovery_date so the refill clock
        won't fire stale.
      - Exiting crisis: set recovery_date to NOW so the refill
        clock starts on the right beat.

    Effective dry_run is the OR of cmd_line_dry_run and
    not-is_latched.  Pre-setup state has is_live_latched=False, so
    even without --dry-run flag, no real trades fire until the
    operator runs setup.py and the latch is set.  Belt-and-
    suspenders against accidental execution.
    """
    state = load_state()
    apply_dynamic_config(state)

    is_latched = state.get('is_live_latched', False)

    if not is_latched and not cmd_line_dry_run:
        # Pre-setup state.  Refuse to run — the operator hasn't
        # gone through setup.py yet, so we don't have a populated
        # state file with funding targets, and any trades we issued
        # would be against an unconfigured portfolio.
        logger.critical("CRITICAL FATAL: RPM has not been initialized.")
        sys.exit(1)

    # Effective dry_run: explicit flag wins; otherwise, anything
    # pre-latch is dry-run by definition (caller forgot --dry-run
    # but we know better).
    effective_dry_run = True if cmd_line_dry_run else not is_latched

    client = IBKRClient()
    now = datetime.datetime.now()
    current_month = now.month

    logger.info('========== RPM RUN START (effective_dry_run=%s) ==========', effective_dry_run)
    audit_log('run_start', {'effective_dry_run': effective_dry_run, 'month': current_month})

    client.connect()

    try:
        # ---- 1. Gather live data ----
        live_balances = client.get_portfolio_state()
        portfolio = Portfolio(live_balances)

        audit_log('portfolio_snapshot', {
            'core_balance': portfolio.core_balance,
            'growth_balance': portfolio.growth_balance,
            'fi_balance': portfolio.fi_balance,
            'buffer_balance': portfolio.buffer_balance,
            'balances': live_balances,
        })

        # ---- 2. Fetch synthetic proxy index SMA data ----
        # The trend-lookback window (10 months as of Session 4) drives
        # the circuit breakers; the 12-month annual-review window
        # drives the inflation freeze and is independent.  Both call
        # the same function but with different period/bar args.
        proxy_level_trend, sma_trend = client.get_synthetic_index_and_sma(
            config.SYNTHETIC_INDEX_TICKERS,
            config.TREND_SMA_PERIOD,
            config.TREND_SMA_BAR,
        )

        proxy_price_12mo, sma_12mo = client.get_synthetic_index_and_sma(
            config.SYNTHETIC_INDEX_TICKERS,
            config.SMA_12MO_PERIOD,
            config.SMA_12MO_BAR,
        )

        # ---- 3. Evaluate circuit breakers (Always Runs) ----
        # was_in_crisis captures the PRE-eval state so we can
        # detect transitions (entering or exiting crisis) for
        # the alert layer below.
        was_in_crisis = state['in_buffer_transition']
        strategy = Strategy(
            in_buffer_transition=was_in_crisis,
            transition_price=state['transition_price'],
        )
        halt_rebalancing, force_buffer = strategy.evaluate_circuit_breakers(
            proxy_level_trend, sma_trend,
        )

        # ALERT: Circuit Breaker Activation / Deactivation
        # Only fires on TRANSITIONS, not steady state.  was_in_crisis
        # is the prior-tick value; force_buffer is the post-eval
        # value.  XOR them for the transition events.
        if not was_in_crisis and force_buffer and alerter:
            alerter.send_buffer_alert('ACTIVATED', f"Proxy index dropped below -7.5% SMA. Crisis mode engaged. Routing all withdrawals to SGOV.")
        elif was_in_crisis and not force_buffer and alerter:
            alerter.send_buffer_alert('RECOVERY', f"Proxy index recovered. Crisis mode deactivated. Initiating normal operations and recovery clock.")

        # Recovery_date bookkeeping.  Set ONLY on the
        # crisis-to-normal transition (not every weekly run
        # while we're already recovered) so the 60-day refill
        # clock starts ticking from the right beat.  Cleared
        # on the normal-to-crisis transition so a stale value
        # from a previous recovery doesn't accidentally
        # short-circuit the new refill clock.
        if was_in_crisis and not force_buffer:
            state['recovery_date'] = now.isoformat()
            logger.info("Crisis mode exited. Recovery clock started at %s", state['recovery_date'])

        if not was_in_crisis and force_buffer:
            state['recovery_date'] = None

        state['in_buffer_transition'] = strategy.in_buffer_transition
        state['transition_price'] = strategy.transition_price

        # ---- 4. WEEKLY ROUTINE: Rebalancing & Refill Logic ----
        if is_weekly:
            logger.info('--- Executing Weekly Drift & Rebalance Check ---')

            # Determine if buffer refill is currently active.
            # Three conditions must all be true:
            #   1. We are NOT currently in crisis mode.
            #   2. We have a recovery_date set (i.e., we exited
            #      crisis at some point).
            #   3. At least BUFFER_REFILL_DELAY_DAYS (default 60)
            #      have passed since recovery.  This delay gives
            #      Growth assets time to recover before we start
            #      siphoning them to refill SGOV.
            refill_active = False
            if not strategy.in_buffer_transition and state.get('recovery_date'):
                recovery_date = datetime.datetime.fromisoformat(state['recovery_date'])
                days_since_recovery = (now - recovery_date).days
                delay_days = getattr(config, 'BUFFER_REFILL_DELAY_DAYS', 60)
                if days_since_recovery >= delay_days:
                    refill_active = True

            if not halt_rebalancing:
                # Generate trades that BOTH deploy idle cash AND
                # trim drift.  Refill prioritization (if active)
                # is handled inside generate_rebalance_trades.
                rebal_trades = portfolio.generate_rebalance_trades(
                    sgov_target=state['sgov_target_dollars'],
                    refill_active=refill_active
                )
                if rebal_trades:
                    # ALERT: Large Rebalance Evaluation (>$10,000 threshold)
                    # Sum the absolute values across all directions
                    # (BUYs + SELLs) so a $5k SELL + $5k BUY pair
                    # counts as $10k of activity, even though it's
                    # net zero cash.
                    total_rebal_volume = sum(abs(amount) for direction, ticker, amount in rebal_trades)
                    if total_rebal_volume > 10000.0 and alerter:
                        trade_details = "\n".join([f"- {direction} {ticker}: ${amount:,.2f}" for direction, ticker, amount in rebal_trades])
                        alerter.send_custom(
                            subject="[RPM] Large Rebalance Executed",
                            body=f"Executed a significant rebalance totaling ${total_rebal_volume:,.2f}.\n\nTrades:\n{trade_details}"
                        )

                    audit_log('rebalance_and_deploy_trades', {'trades': rebal_trades})
                    for direction, ticker, amount in rebal_trades:
                        if direction == 'SELL':
                            client.sell_dollar_amount(ticker, amount, dry_run=effective_dry_run)
                        elif direction == 'BUY':
                            client.buy_dollar_amount(ticker, amount, dry_run=effective_dry_run)

                if refill_active:
                    # SEPARATE refill SELL pass: even if
                    # rebalance generated some refill BUYs from
                    # idle cash, the recurring SELL component
                    # (~8.33%/month) needs to fire too.  These
                    # are the planned monthly bites that
                    # rebuild SGOV over ~12 months.
                    refill_rate = getattr(config, 'BUFFER_REFILL_MONTHLY_RATE', 0.0833)
                    refill_sells = portfolio.route_buffer_refill_sells(
                        sgov_target=state['sgov_target_dollars'],
                        monthly_refill_rate=refill_rate
                    )
                    if refill_sells:
                        audit_log('buffer_refill_sells', {'trades': refill_sells})
                        for ticker, amount in refill_sells:
                            client.sell_dollar_amount(ticker, amount, dry_run=effective_dry_run)
            else:
                logger.info('Rebalancing & Refills HALTED by trend SMA circuit breaker')

        # ---- 5. MONTHLY ROUTINE: Cash Raising & Annual Reviews ----
        if is_monthly:
            logger.info('--- Executing Monthly Cash Raising Routine ---')
            target_withdrawal = state['current_monthly_withdrawal']

            # November annual review.  Two pieces:
            #   - Inflation raise (skip if 12-month SMA is down >=5%)
            #   - Bull-market bonus (extra cash if Growth >+25% YoY)
            # Both update target_withdrawal for THIS month only;
            # state['current_monthly_withdrawal'] persists to the
            # next 11 months at the new (post-raise) level.
            if current_month == getattr(config, 'BONUS_EVAL_MONTH', 11):
                logger.info('--- November Annual Review ---')

                freeze = strategy.evaluate_inflation_freeze(proxy_price_12mo, sma_12mo)
                if not freeze:
                    # Apply 3% inflation raise to the persistent
                    # withdrawal level.  This is the only place
                    # current_monthly_withdrawal increases.
                    state['current_monthly_withdrawal'] *= (1 + getattr(config, 'ANNUAL_INFLATION_RATE', 0.03))
                    target_withdrawal = state['current_monthly_withdrawal']

                # Bonus only applies AFTER the first full year (we
                # need a prior-November snapshot to compute YoY).
                # The bonus is added to THIS month only — it does
                # NOT compound into next year's baseline.
                prev_growth = state.get('last_november_growth_value', 0.0)
                if prev_growth > 0:
                    bonus = strategy.evaluate_november_bonus(portfolio.growth_balance, prev_growth)
                    if bonus > 0:
                        target_withdrawal += bonus
                        if alerter:
                            alerter.send_custom(subject="[RPM] Bull Market Bonus", body=f"Growth bucket exceeded 25% YoY return. Extracted special dividend: ${bonus:,.2f}")

                # Snapshot CURRENT growth for next year's YoY math.
                state['last_november_growth_value'] = portfolio.growth_balance

            # Execute cash raising via the residual-aware cascade.
            # force_buffer comes from the circuit-breaker eval
            # above: True in crisis (SGOV first), False in
            # normal mode (FI first).
            if target_withdrawal > 0:
                sell_orders = portfolio.route_cash_raising(target_withdrawal, force_buffer=force_buffer)

                # ALERTS: Cascade Exhaustion & Large Buffer Drawdowns
                # We diagnose the cascade situation by which
                # tiers contributed to sell_orders.  Multi-tier
                # spans mean a tier hit residual and overflowed
                # to the next.  See the README "Cascade Failure
                # Alerts" section.
                if alerter:
                    sgov_sold = sum(amt for t, amt in sell_orders if t == config.TICKER_BUFFER)
                    fi_sold = sum(amt for t, amt in sell_orders if t in config.TICKERS_FI)
                    growth_sold = sum(amt for t, amt in sell_orders if t in config.TICKERS_GROWTH)

                    if sgov_sold > 10000:
                        alerter.send_buffer_alert('DRAWDOWN', f"Withdrew ${sgov_sold:,.2f} from SGOV buffer.")

                    # Crisis mode + sells from BOTH SGOV and FI
                    # = SGOV hit residual and overflowed.
                    if force_buffer and sgov_sold > 0 and fi_sold > 0:
                        alerter.send_buffer_alert('EMPTY_MOVING_TO_FI', f"CRITICAL: SGOV buffer exhausted. Spillover selling forced into Fixed Income (${fi_sold:,.2f}).")

                    # Sells from BOTH FI and Growth = FI hit
                    # residual and overflowed.  This is the
                    # severest level: cannibalizing volatile
                    # growth assets at depressed prices.
                    if fi_sold > 0 and growth_sold > 0:
                        alerter.send_buffer_alert('FI_EMPTY_MOVING_TO_GROWTH', f"CRITICAL: Fixed Income exhausted. Forced to sell high-volatility Growth assets to meet withdrawal targets (${growth_sold:,.2f}).")

                for ticker, amount in sell_orders:
                    client.sell_dollar_amount(ticker, amount, dry_run=effective_dry_run)

        # ---- 6. Save state ----
        save_state(state)
        logger.info('========== RPM RUN COMPLETE ==========')

        return effective_dry_run

    finally:
        client.disconnect()


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='Retirement Portfolio Manager')
    parser.add_argument('--weekly', action='store_true', help='Execute the weekly drift rebalance check.')
    parser.add_argument('--monthly', action='store_true', help='Execute the monthly cash raising.')
    parser.add_argument('--dry-run', action='store_true', help='Force dry-run regardless of setup state')
    parser.add_argument('--heartbeat', action='store_true', help='Send a detailed heartbeat alert')
    args = parser.parse_args()

    alerter = AlertManager()

    # Acquire the process-wide lock for the rest of main().  See the
    # _ProcessLock docstring above for rationale.  Heartbeat, weekly,
    # and monthly runs are all protected.  Refuse to run — silently,
    # logging only — if another RPM is already underway; a skipped
    # run is always safer than corrupted state.
    try:
        lock = _ProcessLock().__enter__()
    except _LockBusyError:
        logger.warning(
            'Another RPM process is already running (lock held on %s); '
            'skipping this invocation.', LOCK_FILE,
        )
        sys.exit(0)

    try:
        _main_locked(args, alerter)
    finally:
        lock.__exit__(None, None, None)


def _main_locked(args, alerter):
    """Body of main() that runs while holding the process lock.
    Factored out so the lock acquire/release is in one place."""

    if args.heartbeat:
        state = load_state()
        is_latched = state.get('is_live_latched', False)

        if is_latched:
            # Connect and pull live state for the detailed heartbeat
            client = IBKRClient()
            try:
                client.connect()
                balances = client.get_portfolio_state()
                client.disconnect()

                portfolio = Portfolio(balances)
                
                # Math for Alert formatting
                sgov_target = state.get('sgov_target_dollars', 1.0)
                sgov_pct = (portfolio.buffer_balance / sgov_target) * 100 if sgov_target else 0.0
                
                sgov_status = "ARMED"
                if state.get('in_buffer_transition'):
                    sgov_status = "CRISIS MODE ACTIVE"
                elif sgov_pct < 95.0:
                    sgov_status = "REFILLING"

                core_balances = {t: balances.get(t, 0) for t in config.CORE_TICKERS}
                core_total = portfolio.core_balance if portfolio.core_balance > 0 else 1.0
                weights = {t: (v / core_total * 100) for t, v in core_balances.items()}

                # Calculate days to the 15th (assumed standard payday).
                # The IBKR auto-ACH typically pulls on the 15th; if
                # today is past the 15th, roll forward to NEXT month's
                # 15th (handling the December-to-January year boundary).
                # The .replace(day=15) trick works for any source month
                # because all months have a 15th.
                now = datetime.datetime.now()
                payday = now.replace(day=15)
                if now > payday:
                    month = now.month + 1
                    year = now.year
                    if month > 12:
                        month = 1
                        year += 1
                    payday = datetime.datetime(year, month, 15)
                days_to_payday = (payday - now).days

                alerter.send_heartbeat(core_balances, weights, sgov_pct, sgov_status, days_to_payday)
                
            except Exception as e:
                logger.error(f"Heartbeat failed to pull portfolio data: {e}")
                alerter.send_error("Heartbeat routine failed to connect to IBKR.", exception=e)
        else:
            current_month = datetime.datetime.now().month
            last_month = state.get('last_idle_heartbeat_month', 0)
            if current_month != last_month:
                alerter.send_custom(
                    subject="[RPM] Monthly Sentinel Check — SETUP PENDING",
                    body="Hardware and network are functional. RPM setup has not been completed."
                )
                state['last_idle_heartbeat_month'] = current_month
                save_state(state)
        return

    if not (args.weekly or args.monthly):
        logger.error("Execution aborted: You must specify --weekly or --monthly.")
        sys.exit(1)

    try:
        # Pass the alerter down into the execution loop to handle the specific triggers
        run_rpm(
            is_weekly=args.weekly, 
            is_monthly=args.monthly, 
            cmd_line_dry_run=args.dry_run,
            alerter=alerter
        )
        
        # NOTE: Routine send_success() emails have been removed here to maintain appliance silence.
        
    except Exception as e:
        logger.exception('RPM crashed')
        alerter.send_error('RPM terminated unexpectedly.', exception=e)
        sys.exit(1)


if __name__ == '__main__':
    main()