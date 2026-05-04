# portfolio.py — Retirement Portfolio Manager (RPM)
# ================================================
# Represents the live portfolio state and handles:
#   - Core vs. buffer balance separation
#   - Drift detection against 50/50 target
#   - Cash-raising sell order routing
#   - Rebalance trade generation (SELL and T+1 BUY)
#   - SGOV Buffer Refill mechanics
#
# Pre-RPM-bugfix this header read "Survivor Portfolio Manager (SPM)" —
# the codebase was forked from SPM and the rename was incomplete.  The
# RPM does not derive any behavior from SPM at runtime; only the
# header text and the logger namespace were stale.

import logging
import config

logger = logging.getLogger('rpm.portfolio')


class Portfolio:
    def __init__(self, live_balances: dict):
        """
        Snapshot the live balance dictionary into structured buckets.

        live_balances is {ticker: market_value_usd} for every ticker
        the IBKR client returned a price for, including USD cash.
        Tickers NOT in live_balances default to $0 (not in the
        portfolio).  We never mutate live_balances; this object
        owns its own derived totals so that subsequent rebalance/
        cascade math reads from a consistent snapshot even if
        positions move during the run.

        Three derived totals matter downstream:
          - core_balance = growth + fi (the part the 50/50 target
            applies to; SGOV and USD cash are EXCLUDED from core
            because they're operational, not investment).
          - buffer_balance = SGOV (treated separately because it's
            the crisis blast shield).
          - cash_balance = USD (the operational cash that funds
            the monthly ACH withdrawal).
        """
        self.balances = live_balances

        # Core buckets — sum across the configured Growth and FI
        # tickers, defaulting to 0 for any ticker not present in the
        # live balance dict.
        self.growth_balance = sum(self.balances.get(t, 0.0) for t in config.TICKERS_GROWTH)
        self.fi_balance = sum(self.balances.get(t, 0.0) for t in config.TICKERS_FI)
        self.core_balance = self.growth_balance + self.fi_balance

        # Buffer and Cash — single-ticker buckets, isolated from the
        # 50/50 core math.  Any legacy/non-RPM tickers in
        # live_balances are silently ignored here (this is the
        # "keep legacy positions alongside the RPM" feature added
        # in Session 5's interactive setup picker).
        self.buffer_balance = self.balances.get(config.TICKER_BUFFER, 0.0)
        self.cash_balance = self.balances.get(config.CASH_TICKER, 0.0)

        logger.info(
            'Portfolio loaded — Core: $%.2f (Growth: $%.2f, FI: $%.2f), '
            'Buffer: $%.2f, Cash: $%.2f',
            self.core_balance, self.growth_balance, self.fi_balance,
            self.buffer_balance, self.cash_balance,
        )

    def get_drift(self):
        """
        Evaluate the 5/25 rebalancing bands against the current
        Growth-vs-FI split.

        Returns: (current_growth_pct: float, drifted: bool)
          current_growth_pct — Growth share of core (0.0 to 1.0).
          drifted            — True if EITHER absolute drift > 5%
                               OR relative drift > 25% of target.

        The 5/25 rule is industry-standard: 5% absolute is what
        catches drift in a 50/50 portfolio (the absolute band
        fires first); 25% relative is what catches drift in a
        non-50/50 allocation someone might configure later (e.g.
        a 70/30 where 5% absolute would barely register).  In a
        50/50 the absolute band is always the binding constraint,
        but we evaluate both for forward-compatibility.

        Edge case: core_balance == 0 (a fresh account before
        setup, or a wholly-liquidated portfolio).  Returns
        (0.0, False) so callers can short-circuit cleanly without
        a divide-by-zero.
        """
        if self.core_balance <= 0: return 0.0, False

        current_growth_pct = self.growth_balance / self.core_balance
        target = config.TARGET_ALLOCATION_GROWTH

        abs_drift = abs(current_growth_pct - target)
        rel_drift = abs_drift / target if target > 0 else 0.0

        drifted = (abs_drift > config.REBALANCE_BAND_ABSOLUTE) or (rel_drift > config.REBALANCE_BAND_RELATIVE)
        return current_growth_pct, drifted

    # ------------------------------------------------------------------
    # Rebalance & T+1 Buy Deployment
    # ------------------------------------------------------------------
    def generate_rebalance_trades(self, sgov_target=0.0, refill_active=False):
        """
        Generates buy/sell orders to bring the portfolio back to
        target allocation, deploying any excess settled cash.

        Two phases:
          1. BUY phase — deploys deployable_cash (cash above the
             operational floor):
               (a) FIRST claim: SGOV refill, IF refill_active.
                   This priority is intentional — once we've
                   committed to refilling the buffer (post-crisis
                   recovery + 60-day delay elapsed), refilling it
                   takes priority over core rebalancing.  The
                   buffer is the single most important defensive
                   structure in the system; rebuilding it ASAP
                   matters more than perfect 50/50 balance.
               (b) SECOND claim: core rebalancing.  Deploys
                   remaining cash into the bucket(s) that are
                   underweight relative to a synthetic ideal_core
                   = current_core + remaining_cash.  Both deficits
                   are computed against the SAME synthetic target
                   so they share the same denominator.
          2. SELL phase — trims overweight positions if drifted
             beyond the 5/25 bands.

        Why BUY before SELL?  Two reasons:
          - In peacetime the BUY phase usually closes most of the
             drift gap (cash deployment naturally goes to the
             underweight side), so the SELL phase often has
             nothing to do.  Doing BUY first means SELL has
             post-deployment numbers to work from.
          - A SELL+BUY sequence on the same ticker would be a
             wash trade.  Doing BUY first eliminates the
             possibility because BUY consumes cash and the SELL
             phase only fires for OVERWEIGHT positions, which
             can't be the same as the underweight positions we
             just bought.

        Returns: list of (direction, ticker, dollar_amount) tuples.
        """
        trades = []
        # deployable_cash = cash above the operational floor
        # (CASH_BUFFER_TARGET = monthly_withdrawal + transaction_buffer).
        # Anything below that floor is reserved for the upcoming
        # ACH pull and must NOT be deployed.
        deployable_cash = max(0.0, self.cash_balance - config.CASH_BUFFER_TARGET)

        # 1. BUY SIDE — First Claim: SGOV Buffer
        # Only fires when refill_active is True — i.e. post-crisis
        # recovery + 60-day delay has elapsed.  Buffer refill
        # prioritizes the defensive structure over perfect
        # 50/50 core balance.
        if refill_active and deployable_cash > 50.0:
            sgov_deficit = max(0.0, sgov_target - self.buffer_balance)
            if sgov_deficit > 0:
                amount_for_sgov = min(deployable_cash, sgov_deficit)
                trades.append(('BUY', config.TICKER_BUFFER, round(amount_for_sgov, 2)))
                deployable_cash -= amount_for_sgov
                logger.info('Deployed $%.2f of settled cash directly to SGOV.', amount_for_sgov)

        # 1. BUY SIDE — Second Claim: Core Rebalancing
        # Compute deficits against a synthetic ideal_core that
        # ASSUMES we'll deploy all the remaining cash.  This is
        # what makes the proportional split correct: if cash is
        # $20k and core is $180k, ideal_core = $200k, and we
        # split deficits relative to that $200k denominator.
        # Then we sort and fill the bigger deficit first, which
        # is a tiebreaker for deciding which bucket gets cash
        # when both are underweight (rare, only happens if cash
        # accumulated outside both buckets simultaneously).
        if deployable_cash > 50.0:
            ideal_core = self.core_balance + deployable_cash
            target_growth = ideal_core * config.TARGET_ALLOCATION_GROWTH
            target_fi = ideal_core * config.TARGET_ALLOCATION_FI

            deficit_growth = target_growth - self.growth_balance
            deficit_fi = target_fi - self.fi_balance

            # max(0, deficit) clamps overweight buckets to 0 —
            # we only BUY where we're under target.  Overweight
            # buckets get handled by the SELL phase below.
            deficits = [
                ('GROWTH', max(0, deficit_growth), config.TICKERS_GROWTH),
                ('FI', max(0, deficit_fi), config.TICKERS_FI)
            ]
            # Sort biggest deficit first so the cash flows to the
            # most-underweight bucket before the smaller one.
            deficits.sort(key=lambda x: x[1], reverse=True)

            for bucket_name, deficit, tickers in deficits:
                if deployable_cash > 50.0 and deficit > 0:
                    amount_to_buy = min(deployable_cash, deficit)
                    # Within a bucket, split EVENLY across its
                    # tickers (not by current weight).  The 5/25
                    # math operates at bucket level not
                    # ticker level, so even split is correct: it
                    # keeps the tickers within a bucket roughly
                    # equal-weight over time without churning
                    # them every cycle.
                    split_amount = round(amount_to_buy / len(tickers), 2)
                    for ticker in tickers:
                        trades.append(('BUY', ticker, split_amount))
                    deployable_cash -= amount_to_buy

        # 2. SELL SIDE — Trim overweight positions if drifted
        # Only fires if the 5/25 bands say we've drifted.  In a
        # 50/50 portfolio post-BUY-phase, drift is usually small,
        # so this often does nothing.  When it does fire, it
        # trims proportionally from whichever bucket is
        # overweight.
        current_growth_pct, drifted = self.get_drift()
        if drifted:
            target_growth = self.core_balance * config.TARGET_ALLOCATION_GROWTH
            excess_growth = self.growth_balance - target_growth

            # excess_growth's sign tells us which bucket is
            # overweight.  Note: in a balanced portfolio,
            # (target_growth + target_fi) == core_balance, so
            # excess_growth = -excess_fi.  When excess_growth < 0
            # (Growth underweight), the FI bucket is overweight
            # by exactly abs(excess_growth) — that's the math
            # the else branch leans on.
            if excess_growth > 0:
                # Growth overweight: trim Growth, weighted by
                # each Growth ticker's share of the Growth bucket.
                for ticker in config.TICKERS_GROWTH:
                    weight = self.balances[ticker] / self.growth_balance if self.growth_balance > 0 else 0.0
                    amount = round(excess_growth * weight, 2)
                    if amount > 50.0: trades.append(('SELL', ticker, amount))
            else:
                # FI overweight: trim FI, weighted by each FI
                # ticker's share of the FI bucket.
                excess_fi = abs(excess_growth)
                for ticker in config.TICKERS_FI:
                    weight = self.balances[ticker] / self.fi_balance if self.fi_balance > 0 else 0.0
                    amount = round(excess_fi * weight, 2)
                    if amount > 50.0: trades.append(('SELL', ticker, amount))

        return trades

    # ------------------------------------------------------------------
    # Buffer Refill Math
    # ------------------------------------------------------------------
    def route_buffer_refill_sells(self, sgov_target, monthly_refill_rate):
        """
        Calculate sell orders to raise cash for refilling the SGOV
        buffer post-crisis.

        Two-step source logic:
          Step 1 — Pull from any OVERWEIGHT side of the core first.
                   If Growth is above target, sell Growth.  If FI is
                   above target, sell FI.  This naturally rebalances
                   the core in the same operation.
          Step 2 — If the overweight side wasn't enough, pull the
                   shortfall proportionally from Growth.  Growth is
                   the source-of-last-resort for refill (the
                   philosophy: the buffer was DRAINED to protect
                   Growth during a crisis, so Growth is the
                   long-term beneficiary and rightfully funds the
                   rebuild).

        Refill rate caveat: monthly_refill_rate is the
        config.BUFFER_REFILL_MONTHLY_RATE (default 8.33%, i.e.
        ~12-month full refill).  We cap the refill at the lower of
        (this monthly bite) or (the actual deficit) — so once the
        buffer reaches target, refill orders go to zero naturally.

        Settled-cash optimization: if there's already idle cash
        sitting above the operational floor (e.g. dividends accrued
        between runs), we credit that toward the refill and only
        sell the remainder.  This avoids unnecessary trading.

        Returns: [(ticker, dollar_amount), ...].  Empty list if no
        refill needed (deficit too small or already covered by
        idle cash).  Sub-$0.50 dust orders are filtered out.
        """
        sgov_deficit = max(0.0, sgov_target - self.buffer_balance)
        if sgov_deficit <= 0: return []

        # Cap the bite at the smaller of (this month's allowance)
        # vs (actual deficit).  Once the buffer reaches target,
        # deficit becomes zero and refill orders naturally cease.
        target_refill = min(sgov_target * monthly_refill_rate, sgov_deficit)

        # If we already have settled cash sitting idle (e.g. from dividends),
        # reduce the amount we need to sell this week.  This is the
        # "prefer idle cash over fresh sales" optimization.
        deployable_cash = max(0.0, self.cash_balance - config.CASH_BUFFER_TARGET)
        cash_to_raise = target_refill - deployable_cash

        if cash_to_raise < 50.0:
            # Either deployable cash already covers the refill, or
            # the residual sale would be dust.  Skip.
            return []

        trades = []
        remaining = cash_to_raise

        # Step 1: Pull from the overweight side of the core.
        # Both excess_* values are computed against the CURRENT
        # core_balance (not the post-deployment ideal_core used
        # in generate_rebalance_trades).  Reason: this routine
        # runs DURING the weekly check, so we're working with
        # the live snapshot.
        target_growth = self.core_balance * config.TARGET_ALLOCATION_GROWTH
        target_fi = self.core_balance * config.TARGET_ALLOCATION_FI

        excess_growth = max(0.0, self.growth_balance - target_growth)
        excess_fi = max(0.0, self.fi_balance - target_fi)

        if excess_growth > 0 and remaining > 0:
            amount = min(excess_growth, remaining)
            for ticker in config.TICKERS_GROWTH:
                weight = self.balances.get(ticker, 0) / self.growth_balance
                trades.append((ticker, round(amount * weight, 2)))
            remaining -= amount

        if excess_fi > 0 and remaining > 0:
            amount = min(excess_fi, remaining)
            for ticker in config.TICKERS_FI:
                weight = self.balances.get(ticker, 0) / self.fi_balance
                trades.append((ticker, round(amount * weight, 2)))
            remaining -= amount

        # Step 2: If we STILL need cash, pull proportionally from Growth.
        # Growth is the source-of-last-resort for refill: the buffer
        # was drained DEFENDING Growth during the crisis, so Growth
        # rightfully funds the rebuild.  We don't enforce the
        # per-position residual here because this is a planned
        # multi-month refill, not an emergency cash-raise; the
        # 8.33%/month rate is gentle enough that draining a Growth
        # ticker to residual would take many months and the operator
        # would notice via the heartbeat long before it happened.
        if remaining > 1.0:
            for ticker in config.TICKERS_GROWTH:
                weight = self.balances.get(ticker, 0) / self.growth_balance if self.growth_balance else 0
                trades.append((ticker, round(remaining * weight, 2)))

        logger.info('Buffer Refill SELL targets generated to raise $%.2f: %s', cash_to_raise, trades)
        # Filter sub-dollar dust orders that would just be transaction noise.
        return [(t, a) for t, a in trades if a > 0.50]

    # ------------------------------------------------------------------
    # Cash-raising for monthly withdrawal
    # ------------------------------------------------------------------
    def _pull_from_tier(self, tier_tickers, amount_to_raise):
        """
        Distribute `amount_to_raise` proportionally across
        `tier_tickers`, where each position's contribution is capped
        so its post-sale balance never falls below
        config.PER_POSITION_RESIDUAL_USD.

        Returns: (orders, raised, unfunded)
          orders   - list of (ticker, dollar_amount) sell orders
          raised   - dollars actually raised (= sum of amounts in orders)
          unfunded - dollars that could NOT be raised within this tier
                     (caller cascades to next tier).

        Algorithm:
          1. For each ticker in `tier_tickers`, compute
               available[t] = max(0, balance[t] - residual)
          2. tier_capacity = sum(available)
          3. If amount_to_raise <= tier_capacity:
               Pull proportionally by `available[t]` weights.
               This guarantees each position lands at
               (balance - share_of_pull) >= residual since the
               weight is bounded by available.
             Else:
               Drain each position to its residual: order = available[t].
               Spill the shortfall to the caller as `unfunded`.
          4. Round to cents.  Skip orders < $0.50 (sub-cent dust).

        Edge case: tier_tickers is empty, or every position is
        already at/below residual.  tier_capacity = 0, raised = 0,
        unfunded = amount_to_raise.
        """
        residual = config.PER_POSITION_RESIDUAL_USD

        # Per-position available capacity (post-residual).
        available = {}
        for t in tier_tickers:
            bal = self.balances.get(t, 0.0)
            available[t] = max(0.0, bal - residual)

        tier_capacity = sum(available.values())

        if tier_capacity <= 0:
            return [], 0.0, amount_to_raise

        orders = []

        if amount_to_raise <= tier_capacity:
            # Proportional pull within tier.  Each ticker's share is
            # weighted by its own available capacity, which is
            # bounded by (balance - residual).  Therefore the share
            # is bounded by available[t], which means post-sale the
            # position cannot drop below residual.
            for t in tier_tickers:
                if available[t] <= 0:
                    continue
                weight = available[t] / tier_capacity
                amount = round(amount_to_raise * weight, 2)
                if amount >= 0.50:
                    orders.append((t, amount))
            raised = sum(a for _, a in orders)
            unfunded = max(0.0, amount_to_raise - raised)
            return orders, raised, unfunded

        # Tier insufficient: drain everything to residual, spill the rest.
        for t in tier_tickers:
            if available[t] <= 0:
                continue
            amount = round(available[t], 2)
            if amount >= 0.50:
                orders.append((t, amount))
        raised = sum(a for _, a in orders)
        unfunded = max(0.0, amount_to_raise - raised)
        return orders, raised, unfunded

    def route_cash_raising(self, target_amount, force_buffer=False):
        """
        Determines which assets to sell to meet the monthly withdrawal.

        Hierarchy:
          Crisis mode (force_buffer=True):  SGOV → FI → Growth
          Normal mode (force_buffer=False):       FI → Growth

        Per-position residual (Session 5): NO position is drawn below
        config.PER_POSITION_RESIDUAL_USD.  When a tier's total
        available capacity (sum of (balance - residual) per ticker)
        is exhausted, the unfunded remainder cascades to the next
        tier.  Pre-Session-5 the cascade would drain positions to
        zero, which (in a Roth) wasn't catastrophic but did create
        downstream UX pain in the rebalancing math.

        Returns: [(ticker, dollar_amount), ...]
        """
        sell_orders = []
        remaining = float(target_amount)

        # Path A: Crisis — pull from buffer first.  SGOV is a
        # single-ticker tier; the residual still applies (SGOV is
        # not allowed to be drained to zero either; mostly a UX
        # concern, since SGOV is whole-shares only and a $1,500
        # floor is roughly 15 shares).
        if force_buffer and remaining > 0:
            buffer_orders, raised, remaining = self._pull_from_tier(
                [config.TICKER_BUFFER], remaining,
            )
            sell_orders.extend(buffer_orders)
            if raised > 0:
                logger.info('SGOV cascade: raised $%.2f', raised)
            if remaining > 0:
                logger.warning(
                    'SGOV exhausted at $%.2f residual; spilling '
                    '$%.2f to FI tier', config.PER_POSITION_RESIDUAL_USD,
                    remaining,
                )

        # Path B: Normal / Crisis fallback — pull from FI tier.
        if remaining > 0:
            fi_orders, raised, remaining = self._pull_from_tier(
                config.TICKERS_FI, remaining,
            )
            sell_orders.extend(fi_orders)
            if raised > 0:
                logger.info('FI cascade: raised $%.2f from %s',
                            raised, [t for t, _ in fi_orders])
            if remaining > 0:
                logger.warning(
                    'FI tier exhausted at $%.2f residual per position; '
                    'spilling $%.2f to Growth tier',
                    config.PER_POSITION_RESIDUAL_USD, remaining,
                )

        # Path C: Last resort — pull from Growth tier.
        if remaining > 0:
            growth_orders, raised, remaining = self._pull_from_tier(
                config.TICKERS_GROWTH, remaining,
            )
            sell_orders.extend(growth_orders)
            if raised > 0:
                logger.warning('Growth cascade: raised $%.2f from %s',
                               raised, [t for t, _ in growth_orders])

        if remaining > 0:
            logger.critical(
                'CASCADE EXHAUSTED: SGOV / FI / Growth all at $%.2f '
                'residual.  Could not raise full withdrawal.  '
                'Deficit: $%.2f', config.PER_POSITION_RESIDUAL_USD,
                remaining,
            )

        logger.info('Cash-raising orders: %s', sell_orders)
        return sell_orders