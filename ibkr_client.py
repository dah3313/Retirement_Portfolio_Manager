# ibkr_client.py — Retirement Portfolio Manager (RPM)
# ==================================================
# Handles all communication with Interactive Brokers via ib_insync.
# Every method that touches the network includes timeout and error handling.
#
# Pre-RPM-bugfix this header read "Survivor Portfolio Manager (SPM)" —
# the codebase was forked from SPM and the rename was incomplete.  The
# RPM does not derive any behavior from SPM at runtime; only the
# header text and the logger namespace were stale.

from ib_insync import IB, Stock, MarketOrder
import math
import logging
import time
import config

logger = logging.getLogger('rpm.ibkr')


class IBKRClient:
    def __init__(self):
        self.ib = IB()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------
    def connect(self, retries=3, delay=30):
        """
        Connect to TWS / IB Gateway with retry logic.

        IBKR Gateway has a mandatory daily 24-hour reset window
        that IBC handles automatically.  If our weekly/monthly
        timer fires during that reset window, the connection will
        fail.  Three retries spaced 30 seconds apart covers the
        typical reset duration; if all three fail, we bubble up
        the exception so the AlertManager fires a connection-
        failure email and the operator knows something is
        actually wrong (vs a transient blip).

        Why three retries specifically?  IBC's reset is bounded
        at ~60 seconds; 3 * 30s = 90 seconds total wait covers
        the longest observed reset.  More retries would mask
        genuine outages; fewer would false-alarm on every reset.

        Idempotent: if already connected, returns immediately
        (the `if not self.ib.isConnected()` guard).
        """
        for attempt in range(1, retries + 1):
            if not self.ib.isConnected():
                try:
                    self.ib.connect(
                        config.IBKR_HOST,
                        config.IBKR_PORT,
                        clientId=config.IBKR_CLIENT_ID,
                        timeout=20,
                    )
                    logger.info('Connected to IBKR at %s:%s', config.IBKR_HOST, config.IBKR_PORT)
                    return # Success
                except Exception as e:
                    logger.warning('Connection attempt %d failed: %s', attempt, e)
                    if attempt < retries:
                        logger.info('Sleeping for %d seconds before retrying...', delay)
                        time.sleep(delay)
                    else:
                        logger.error('Exhausted all connection retries.')
                        raise # Bubble up to trigger the AlertManager email

    def disconnect(self):
        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info('Disconnected from IBKR')

    # ------------------------------------------------------------------
    # Portfolio state
    # ------------------------------------------------------------------
    def get_portfolio_state(self):
        """
        Return a dict of {ticker: market_value_usd} for every tracked
        ticker.  Tickers NOT in the account get $0 entries (so
        downstream code can read them without KeyError).  USD cash is
        included via the CASH_TICKER key.

        IMPORTANT: only the configured RPM tickers (CORE_TICKERS +
        TICKER_BUFFER + CASH_TICKER) appear in the result.  Any legacy
        positions kept by the operator during setup.py's interactive
        picker are silently filtered out here — the rest of the RPM
        never sees them.  This is the mechanism that makes "keep my
        SPY position alongside the RPM" work.

        Two-pass approach:
          1. positions() to get share counts (cheap, always available)
          2. reqTickers() in a single batch for the price data
             (expensive, requires market-data subscription)

        Doing the price fetch in a single batch (not one ticker at a
        time) is materially faster: a 5-symbol batch is ~one second
        round-trip vs. ~5 seconds for sequential calls.  IBKR
        rate-limits per-call but not per-batch in the same way.

        Price fallback chain: marketPrice() -> close -> 0.  marketPrice
        is real-time during market hours but returns NaN outside RTH;
        close is the prior-day's settle and is always populated.  Both
        being NaN means the contract has no recent print at all
        (delisted or extreme illiquidity), in which case we log a
        warning and use 0 — the position effectively becomes invisible
        to the RPM until prices return.
        """
        all_tickers = config.CORE_TICKERS + [config.TICKER_BUFFER]
        state = {t: 0.0 for t in all_tickers}
        state[config.CASH_TICKER] = 0.0

        positions = self.ib.positions()

        # 1. Map positions — build {ticker: share_count} for the
        # tickers we care about.  Legacy/non-RPM positions in the
        # account are skipped here because they're not in `state`'s
        # initialized keys.
        pos_map = {pos.contract.symbol: pos.position for pos in positions}
        if 'USD' in pos_map: # Handle base currency
            state[config.CASH_TICKER] = pos_map['USD']

        # 2. Qualify contracts and request bulk tickers.  Only ask
        # for prices on tickers we actually hold — querying SGOV when
        # we have no SGOV position is wasted bandwidth.
        contracts = [Stock(symbol, 'SMART', 'USD') for symbol in all_tickers if symbol in pos_map]
        if contracts:
            self.ib.qualifyContracts(*contracts)
            tickers = self.ib.reqTickers(*contracts) # Batch request, no sleep needed

            for ticker in tickers:
                symbol = ticker.contract.symbol
                # Price fallback chain: live price -> last close -> 0.
                # See docstring for rationale.
                price = ticker.marketPrice()
                if math.isnan(price):
                    price = ticker.close
                if math.isnan(price):
                    logger.warning('No price available for %s — using 0', symbol)
                    price = 0.0

                state[symbol] = pos_map[symbol] * price

        logger.info('Portfolio state: %s', state)
        return state

    # ------------------------------------------------------------------
    # Synthetic Index — returns (current_level, sma_value)
    # ------------------------------------------------------------------
    def get_synthetic_index_and_sma(self, symbols: list, duration_str, bar_size):
        """
        Build an equal-weighted synthetic index of `symbols` by chaining
        per-bar returns, and return a tuple:
            (synthetic_current_level, synthetic_sma_level)

        Both values are dimensionless levels anchored at 100 at the
        start of the fetch window, so the caller can safely compute
        a percentage drawdown via (current - sma) / sma.  Returns
        (None, None) if data is unavailable or dates do not align.

        ----------------------------------------------------------------
        Math (post-Session-4):
        ----------------------------------------------------------------
        For each symbol s and each aligned bar t > 0:
            r_{s,t} = P_{s,t} / P_{s,t-1} - 1
        Equal-weight across symbols:
            r_{idx,t} = mean(r_{s,t} for s in symbols)
        Compound into a level series anchored at L_0 = 100:
            L_t = L_{t-1} * (1 + r_{idx,t})
        Return:
            current = L_T   (last bar)
            sma     = mean(L_t for t in [0, T])

        Pre-Session-4 this function averaged SHARE PRICES across
        symbols (`synthetic_close = sum(prices[sym] * weight)`),
        which is silently price-weighted: a $500 fund moving 1%
        contributed $5 to the synthetic, while a $50 fund moving
        1% contributed $0.50 — even though the spec calls for an
        equal-weight blend.  Chaining returns is the standard fix
        and matches how index providers actually build composites.

        Why anchor at 100?  Arbitrary choice.  The caller computes
        a ratio so the anchor cancels out.  100 just makes log
        readability cleaner than e.g. anchoring at 1.0.

        ----------------------------------------------------------------
        Edge cases handled:
        ----------------------------------------------------------------
          - Empty symbols list           → (None, None)
          - No bars returned for a       → (None, None)
            symbol
          - No overlapping dates after   → (None, None)
            alignment
          - <2 aligned dates (need at    → (None, None) (no return
            least one prior to compute     can be computed from a
            a return)                      single bar)
          - Zero or negative prior price → skip that bar, log a
                                            warning, continue with
                                            the rest.  Funds don't
                                            generally trade through
                                            zero, but a stale or
                                            corporate-action-mangled
                                            bar shouldn't crash the
                                            whole circuit breaker.
        """
        if not symbols:
            logger.error('No symbols provided for synthetic index')
            return None, None

        # ---- 1. Fetch raw bars per symbol ----
        all_bars = {}
        for symbol in symbols:
            contract = Stock(symbol, 'SMART', 'USD')
            self.ib.qualifyContracts(contract)

            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime='',
                durationStr=duration_str,
                barSizeSetting=bar_size,
                whatToShow='TRADES',
                useRTH=True,
                formatDate=1,
            )

            if not bars:
                logger.error('No historical bars returned for %s', symbol)
                return None, None
            all_bars[symbol] = bars

        # ---- 2. Align by date ----
        # Build {date: {symbol: close}} so we can intersect on dates
        # where every symbol has a print.  A holiday or trading halt
        # affecting only one fund shouldn't poison the index.
        date_map = {}
        for symbol, bars in all_bars.items():
            for bar in bars:
                d = bar.date
                if d not in date_map:
                    date_map[d] = {}
                date_map[d][symbol] = bar.close

        valid_dates = [
            d for d, prices in date_map.items()
            if len(prices) == len(symbols)
        ]
        valid_dates.sort()

        if not valid_dates:
            logger.error('No overlapping trading dates found for %s', symbols)
            return None, None

        if len(valid_dates) < 2:
            # Need at least one prior bar to compute a return.  This
            # should never happen for a 10-month monthly fetch, but
            # belt-and-suspenders.
            logger.error(
                'Only %d aligned bar(s) for %s; need >= 2 to chain '
                'returns', len(valid_dates), symbols,
            )
            return None, None

        # ---- 3. Build per-bar equal-weighted return series ----
        n_symbols = len(symbols)
        synthetic_returns = []     # one entry per t in [1, T]
        for i in range(1, len(valid_dates)):
            d_prev = valid_dates[i - 1]
            d_curr = valid_dates[i]

            per_symbol_returns = []
            for sym in symbols:
                p_prev = date_map[d_prev][sym]
                p_curr = date_map[d_curr][sym]
                if p_prev <= 0:
                    logger.warning(
                        'Non-positive prior close for %s at %s '
                        '(p_prev=%.4f); skipping this bar',
                        sym, d_prev, p_prev,
                    )
                    # Skip the entire equal-weight bar rather than
                    # contaminating the average with a partial set
                    # of symbols.  Setting per_symbol_returns to []
                    # and breaking accomplishes that.
                    per_symbol_returns = []
                    break
                per_symbol_returns.append((p_curr / p_prev) - 1.0)

            if len(per_symbol_returns) == n_symbols:
                synthetic_returns.append(
                    sum(per_symbol_returns) / n_symbols
                )

        if not synthetic_returns:
            logger.error(
                'No usable synthetic returns built for %s after '
                'filtering bad bars', symbols,
            )
            return None, None

        # ---- 4. Compound into a level series anchored at 100 ----
        # The level series has length len(synthetic_returns) + 1
        # because we include the L_0 = 100 anchor.
        levels = [100.0]
        for r in synthetic_returns:
            levels.append(levels[-1] * (1.0 + r))

        sma_value = sum(levels) / len(levels)
        current_level = levels[-1]

        logger.info(
            'Synthetic Index %s [%s / %s] — current: %.4f, '
            'SMA(%d bars): %.4f, drawdown: %.2f%%',
            symbols, duration_str, bar_size,
            current_level, len(levels), sma_value,
            ((current_level - sma_value) / sma_value) * 100
            if sma_value > 0 else 0.0,
        )
        return current_level, sma_value

    # Backwards-compatible alias.  Pre-Session-4 callers used
    # `get_synthetic_price_and_sma` and the function body averaged
    # share prices.  The semantic shift (returns-chained, not
    # price-blended) is documented above; the rename is just to
    # stop the name from lying.  Old name retained as a thin
    # delegate so any operator notebooks still work.
    def get_synthetic_price_and_sma(self, symbols: list, duration_str, bar_size):
        """DEPRECATED: use get_synthetic_index_and_sma."""
        logger.warning(
            'get_synthetic_price_and_sma() is deprecated; calling '
            'get_synthetic_index_and_sma() instead'
        )
        return self.get_synthetic_index_and_sma(
            symbols, duration_str, bar_size,
        )

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------
    def sell_dollar_amount(self, symbol, dollar_amount, dry_run=False):
        """
        Submit a market sell order for a specific dollar amount
        using IBKR's cashQty (fractional-share) order type.

        cashQty is the modern IBKR way to specify orders by
        notional value rather than share count — no need to
        compute (dollar_amount / current_price) ourselves and
        worry about fractional rounding.  totalQuantity=0 is the
        sentinel for cashQty mode; both must be specified.

        Safety: MAX_SINGLE_TRADE_DOLLARS is enforced as a CAP
        (not a reject).  Caller asks for $50k, system caps to
        $15k, logs an error so the operator notices.  Reasoning:
        a runaway calculation that requests an oversized trade
        is more likely to be a bug than a legitimate need; capping
        keeps the bug from doing real damage while still making
        forward progress on whatever the caller wanted.  An
        operator who genuinely wants a >$15k trade should adjust
        MAX_SINGLE_TRADE_DOLLARS, not work around it.

        Wait pattern: 60-second timeout in 5-second polling
        increments.  Most market orders fill in <2 seconds, so
        60 is generous; but stale-quote situations can take
        longer.  If we hit timeout, we explicitly cancelOrder
        and return False so the caller can decide what to do
        with the dangling intent.  Never leave an order
        floating in IBKR after this method returns.

        Returns True only on confirmed Filled.  False on any
        error, timeout, partial fill, or non-Filled terminal
        status.  dry_run mode logs and returns True without
        touching IBKR.
        """
        if dollar_amount <= 0:
            logger.warning('Skipping sell of %s — amount is $%.2f', symbol, dollar_amount)
            return False

        if dollar_amount > config.MAX_SINGLE_TRADE_DOLLARS:
            logger.error(
                'SAFETY CAP: Attempted sell of %s for $%.2f exceeds max $%.2f. '
                'Capping to max.',
                symbol, dollar_amount, config.MAX_SINGLE_TRADE_DOLLARS,
            )
            dollar_amount = config.MAX_SINGLE_TRADE_DOLLARS

        if dry_run:
            logger.info('[DRY RUN] Would SELL %s for $%.2f', symbol, dollar_amount)
            return True

        contract = Stock(symbol, 'SMART', 'USD')
        self.ib.qualifyContracts(contract)

        # cashQty + totalQuantity=0 is IBKR's notional-order mode
        # for fractional shares.  Both fields are required.
        order = MarketOrder('SELL', totalQuantity=0, cashQty=dollar_amount)
        trade = self.ib.placeOrder(contract, order)

        # Wait up to 60 seconds for fill.  5-second polling means
        # at most a 5-second delay after the actual fill before we
        # see the status update.  Acceptable for a once-monthly
        # sequence of orders.
        timeout = 60
        elapsed = 0
        while not trade.isDone() and elapsed < timeout:
            self.ib.waitOnUpdate(timeout=5)
            elapsed += 5

        if trade.orderStatus.status == 'Filled':
            logger.info('FILLED: Sold $%.2f of %s', dollar_amount, symbol)
            return True
        else:
            # Anything other than Filled within the timeout window:
            # cancel the order so it can't fire later when we're
            # not watching, log the situation, and let the caller
            # decide.  Never let a dangling order sit in IBKR.
            logger.error('ORDER INCOMPLETE: %s status=%s after %ds. Canceling order.',
                         symbol, trade.orderStatus.status, elapsed)
            self.ib.cancelOrder(order) # Explicitly cancel the dangling order
            self.ib.sleep(2) # Give IBKR a moment to process the cancellation
            return False

    def buy_dollar_amount(self, symbol, dollar_amount, dry_run=False):
        """
        Submit a market buy order using IBKR's cashQty (fractional
        shares).  Mirror of sell_dollar_amount; see that method's
        docstring for the cashQty pattern, the safety cap rationale,
        the wait-and-cancel logic, and the dry_run semantics.
        """
        if dollar_amount <= 0:
            return False

        if dollar_amount > config.MAX_SINGLE_TRADE_DOLLARS:
            logger.error('SAFETY CAP: Buy order %s capped at max.', symbol)
            dollar_amount = config.MAX_SINGLE_TRADE_DOLLARS

        if dry_run:
            logger.info('[DRY RUN] Would BUY %s for $%.2f', symbol, dollar_amount)
            return True

        contract = Stock(symbol, 'SMART', 'USD')
        self.ib.qualifyContracts(contract)

        order = MarketOrder('BUY', totalQuantity=0, cashQty=dollar_amount)
        trade = self.ib.placeOrder(contract, order)

        timeout = 60
        elapsed = 0
        while not trade.isDone() and elapsed < timeout:
            self.ib.waitOnUpdate(timeout=5)
            elapsed += 5

        if trade.orderStatus.status == 'Filled':
            logger.info('FILLED: Bought $%.2f of %s', dollar_amount, symbol)
            return True
        else:
            logger.error('ORDER INCOMPLETE: %s status=%s after %ds. Canceling order.',
                         symbol, trade.orderStatus.status, elapsed)
            self.ib.cancelOrder(order) # Explicitly cancel the dangling order
            self.ib.sleep(2) # Give IBKR a moment to process the cancellation
            return False