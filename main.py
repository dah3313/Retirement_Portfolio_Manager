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
def load_state():
    if os.path.exists(config.STATE_FILE):
        with open(config.STATE_FILE, 'r') as f:
            return json.load(f)
    return {
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
    
    fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(config.STATE_FILE))
    with os.fdopen(fd, 'w') as f:
        json.dump(state, f, indent=2)
    
    os.replace(temp_path, config.STATE_FILE)
    logger.info('State saved atomically to %s', config.STATE_FILE)

def apply_dynamic_config(state):
    """Injects dynamically derived targets into config so downstream modules use them."""
    withdrawal = state.get('current_monthly_withdrawal', 0.0)
    if withdrawal > 0:
        transaction_buffer = getattr(config, 'CASH_TRANSACTION_BUFFER', 1000.0)
        config.CASH_BUFFER_TARGET = withdrawal + transaction_buffer
        logger.info("Dynamic config applied: CASH_BUFFER_TARGET = $%.2f", config.CASH_BUFFER_TARGET)


# ------------------------------------------------------------------
# Core execution
# ------------------------------------------------------------------
def run_rpm(is_weekly=False, is_monthly=False, cmd_line_dry_run=False):
    state = load_state()
    apply_dynamic_config(state)
    
    # Check if the system has been initialized via setup.py
    is_latched = state.get('is_live_latched', False)
    
    if not is_latched and not cmd_line_dry_run:
        logger.critical("CRITICAL FATAL: RPM has not been initialized.")
        logger.critical("You must run 'python setup.py' to establish the foundation before enabling systemd timers.")
        sys.exit(1)

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
        proxy_price_200, sma_200 = client.get_synthetic_price_and_sma(
            config.SYNTHETIC_INDEX_TICKERS,
            config.SMA_200_PERIOD,
            config.SMA_200_BAR,
        )

        proxy_price_12mo, sma_12mo = client.get_synthetic_price_and_sma(
            config.SYNTHETIC_INDEX_TICKERS,
            config.SMA_12MO_PERIOD,
            config.SMA_12MO_BAR,
        )

        audit_log('sma_data', {
            'proxy': 'SYNTHETIC_GROWTH',
            'proxy_tickers': config.SYNTHETIC_INDEX_TICKERS,
            'price_200': proxy_price_200,
            'sma_200': sma_200,
            'price_12mo': proxy_price_12mo,
            'sma_12mo': sma_12mo,
        })

        # ---- 3. Evaluate circuit breakers (Always Runs) ----
        strategy = Strategy(
            in_buffer_transition=state['in_buffer_transition'],
            transition_price=state['transition_price'],
        )
        halt_rebalancing, force_buffer = strategy.evaluate_circuit_breakers(
            proxy_price_200, sma_200,
        )

        # Detect transition changes to set/clear the Recovery Clock
        if state['in_buffer_transition'] and not force_buffer:
            state['recovery_date'] = now.isoformat()
            logger.info("Crisis mode exited. Recovery clock started at %s", state['recovery_date'])
            audit_log('recovery_started', {'date': state['recovery_date']})
        
        if not state['in_buffer_transition'] and force_buffer:
            state['recovery_date'] = None

        state['in_buffer_transition'] = strategy.in_buffer_transition
        state['transition_price'] = strategy.transition_price

        audit_log('circuit_breakers', {
            'halt_rebalancing': halt_rebalancing,
            'force_buffer': force_buffer,
            'in_buffer_transition': strategy.in_buffer_transition,
            'transition_price': strategy.transition_price,
        })

        # ---- 4. WEEKLY ROUTINE: Rebalancing & Refill Logic ----
        if is_weekly:
            logger.info('--- Executing Weekly Drift & Rebalance Check ---')
            refill_active = False
            if not strategy.in_buffer_transition and state.get('recovery_date'):
                recovery_date = datetime.datetime.fromisoformat(state['recovery_date'])
                days_since_recovery = (now - recovery_date).days
                delay_days = getattr(config, 'BUFFER_REFILL_DELAY_DAYS', 60)
                if days_since_recovery >= delay_days:
                    refill_active = True

            if not halt_rebalancing:
                # Execute Drift Sells & Cash Deployment Buys
                rebal_trades = portfolio.generate_rebalance_trades(
                    sgov_target=state['sgov_target_dollars'], 
                    refill_active=refill_active
                )
                if rebal_trades:
                    audit_log('rebalance_and_deploy_trades', {'trades': rebal_trades})
                    for direction, ticker, amount in rebal_trades:
                        if direction == 'SELL':
                            logger.info('Rebalance SELL: %s $%.2f', ticker, amount)
                            client.sell_dollar_amount(ticker, amount, dry_run=effective_dry_run)
                        elif direction == 'BUY':
                            logger.info('Rebalance/Refill BUY: %s $%.2f', ticker, amount)
                            client.buy_dollar_amount(ticker, amount, dry_run=effective_dry_run)

                # Execute Buffer Refill Sells
                if refill_active:
                    refill_rate = getattr(config, 'BUFFER_REFILL_MONTHLY_RATE', 0.0833)
                    refill_sells = portfolio.route_buffer_refill_sells(
                        sgov_target=state['sgov_target_dollars'],
                        monthly_refill_rate=refill_rate
                    )
                    if refill_sells:
                        audit_log('buffer_refill_sells', {'trades': refill_sells})
                        for ticker, amount in refill_sells:
                            logger.info('Buffer Refill SELL: %s $%.2f', ticker, amount)
                            client.sell_dollar_amount(ticker, amount, dry_run=effective_dry_run)
            else:
                logger.info('Rebalancing & Refills HALTED by 200-day SMA circuit breaker')
                audit_log('rebalance_halted', {})

        # ---- 5. MONTHLY ROUTINE: Cash Raising & Annual Reviews ----
        if is_monthly:
            logger.info('--- Executing Monthly Cash Raising Routine ---')
            target_withdrawal = state['current_monthly_withdrawal']

            # November annual review
            if current_month == getattr(config, 'BONUS_EVAL_MONTH', 11):
                logger.info('--- November Annual Review ---')

                freeze = strategy.evaluate_inflation_freeze(proxy_price_12mo, sma_12mo)
                if freeze:
                    logger.info('Inflation adjustment FROZEN (market down vs 12mo SMA)')
                    audit_log('inflation_frozen', {})
                else:
                    old_withdrawal = state['current_monthly_withdrawal']
                    state['current_monthly_withdrawal'] *= (1 + getattr(config, 'ANNUAL_INFLATION_RATE', 0.03))
                    target_withdrawal = state['current_monthly_withdrawal']
                    logger.info('Inflation adjusted: $%.2f → $%.2f', old_withdrawal, target_withdrawal)
                    audit_log('inflation_adjusted', {'old': old_withdrawal, 'new': target_withdrawal})

                prev_growth = state.get('last_november_growth_value', 0.0)
                if prev_growth > 0:
                    bonus = strategy.evaluate_november_bonus(portfolio.growth_balance, prev_growth)
                    if bonus > 0:
                        target_withdrawal += bonus
                        logger.info('November bonus: +$%.2f', bonus)
                        audit_log('november_bonus', {'bonus': bonus})

                state['last_november_growth_value'] = portfolio.growth_balance

            # Execute cash raising
            if target_withdrawal > 0:
                logger.info('Raising $%.2f for withdrawal', target_withdrawal)
                sell_orders = portfolio.route_cash_raising(
                    target_withdrawal, force_buffer=force_buffer,
                )

                audit_log('cash_raising', {
                    'target': target_withdrawal,
                    'force_buffer': force_buffer,
                    'orders': sell_orders,
                })

                for ticker, amount in sell_orders:
                    logger.info('SELL %s for $%.2f', ticker, amount)
                    success = client.sell_dollar_amount(ticker, amount, dry_run=effective_dry_run)
                    if not success:
                        logger.error('Order may not have filled: %s $%.2f', ticker, amount)

        # ---- 6. Save state ----
        save_state(state)
        audit_log('run_complete', {'state': state})
        logger.info('========== RPM RUN COMPLETE ==========')

        return effective_dry_run

    finally:
        client.disconnect()


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='Retirement Portfolio Manager')
    parser.add_argument(
        '--weekly', action='store_true',
        help='Execute the weekly 5/25 drift rebalance check.',
    )
    parser.add_argument(
        '--monthly', action='store_true',
        help='Execute the monthly cash raising and November annual review.',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Force dry-run regardless of setup state',
    )
    parser.add_argument(
        '--heartbeat', action='store_true',
        help='Send a heartbeat alert and exit',
    )
    args = parser.parse_args()

    alerter = AlertManager()

    if args.heartbeat:
        state = load_state()
        is_latched = state.get('is_live_latched', False)
        current_month = datetime.datetime.now().month

        if is_latched:
            alerter.send_custom(
                subject="[RPM] Heartbeat — SYSTEM ARMED & LIVE",
                body="The RPM is actively managing the portfolio."
            )
        else:
            last_month = state.get('last_idle_heartbeat_month', 0)
            if current_month != last_month:
                alerter.send_custom(
                    subject="[RPM] Monthly Sentinel Check — SETUP PENDING",
                    body="Hardware and network are functional. RPM setup has not been completed."
                )
                state['last_idle_heartbeat_month'] = current_month
                save_state(state)
        return

    # Fail fast if the timer doesn't specify an action route
    if not (args.weekly or args.monthly):
        logger.error("Execution aborted: You must specify --weekly or --monthly.")
        sys.exit(1)

    try:
        effective_dry_run = run_rpm(
            is_weekly=args.weekly, 
            is_monthly=args.monthly, 
            cmd_line_dry_run=args.dry_run
        )
        mode_str = ' [DRY RUN]' if effective_dry_run else ' [LIVE EXECUTION]'
        action_str = 'WEEKLY Rebalance' if args.weekly else 'MONTHLY Withdrawal'
        alerter.send_success(f'RPM {action_str} executed successfully.{mode_str}')
        
    except Exception as e:
        logger.exception('RPM crashed')
        alerter.send_error('RPM terminated unexpectedly.', exception=e)
        sys.exit(1)


if __name__ == '__main__':
    main()