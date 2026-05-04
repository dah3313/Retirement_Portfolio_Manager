# strategy.py — Retirement Portfolio Manager (RPM)
# ===============================================
# Stateless evaluation of market conditions.  State (transition flags) is
# passed in from the caller and returned as updated values — this module
# never reads or writes files.
#
# Pre-RPM-bugfix this header read "Survivor Portfolio Manager (SPM)" —
# the codebase was forked from SPM and the rename was incomplete.  The
# RPM does not derive any behavior from SPM at runtime; only the
# header text and the logger namespace were stale.
#
# Session 4 (synthetic index math + 10-month lookback):
#   - evaluate_circuit_breakers() now takes (current_level, sma) where
#     both are dimensionless index levels (the synthetic index is
#     built by chaining returns; see ibkr_client).  Behavior is
#     unchanged because the percent-drawdown math is the same shape.
#   - The trend-lookback window changed from 200 daily bars to
#     10 monthly bars; that's a config.py change, no code change
#     here.
#
# KEY FIX vs. original Gemini code:
#   The circuit breakers compare the proxy index's CURRENT LEVEL
#   against its own SMA — both in the same dimensionless unit.
#   The original code compared portfolio dollar value against SPY's SMA,
#   which is an apples-to-oranges comparison that would always trigger.

import logging
import config

logger = logging.getLogger('rpm.strategy')


class Strategy:
    def __init__(self, in_buffer_transition=False, transition_price=None):
        """
        in_buffer_transition: Are we currently in crisis mode (withdrawing
                              from SGOV instead of FI)?
        transition_price:     The proxy index LEVEL at which we entered
                              crisis mode (post-Session-4 this is a
                              dimensionless index level anchored at
                              100, not a share price).  Used to
                              calculate the recovery target.  Name kept
                              as `transition_price` for state-file
                              backwards compatibility — existing
                              rpm_state.json files have this key and
                              renaming would force a state migration.
        """
        self.in_buffer_transition = in_buffer_transition
        self.transition_price = transition_price

    # ------------------------------------------------------------------
    # Circuit breakers — evaluated weekly
    # ------------------------------------------------------------------
    def evaluate_circuit_breakers(self, proxy_current_level, proxy_sma):
        """
        Compares the synthetic Growth-asset index level against its
        own trend SMA (10-month monthly bars as of Session 4; was
        200 daily bars pre-Session-4).  Both inputs are dimensionless
        index levels anchored at the same base, so the percent
        drawdown is just (current - sma) / sma.

        Returns: (halt_rebalancing: bool, force_buffer: bool)

        Side effects: updates self.in_buffer_transition and
                      self.transition_price.

        Parameter names changed in Session 4 from
        (proxy_current_price, proxy_sma_200) to
        (proxy_current_level, proxy_sma).  The values are now index
        levels, not share prices, and the SMA window is no longer
        nailed at 200 days.  Behavior is identical — percentage
        drawdown math is unchanged.
        """
        if proxy_sma is None or proxy_sma <= 0:
            logger.warning('Invalid trend SMA value; skipping circuit breaker eval')
            return False, self.in_buffer_transition

        if proxy_current_level is None or proxy_current_level <= 0:
            logger.warning('Invalid current index level; skipping circuit breaker eval')
            return False, self.in_buffer_transition

        drawdown = (proxy_current_level - proxy_sma) / proxy_sma

        logger.info(
            'Circuit breaker — index level: %.4f, trend SMA: %.4f, drawdown: %.2f%%',
            proxy_current_level, proxy_sma, drawdown * 100,
        )

        # Tier 1: Halt rebalancing at -5%
        halt_rebalancing = drawdown <= config.HALT_REBALANCE_THRESHOLD

        # Tier 2: Enter crisis mode at -7.5%
        if not self.in_buffer_transition and drawdown <= config.SHY_TRANSITION_THRESHOLD:
            self.in_buffer_transition = True
            self.transition_price = proxy_current_level
            logger.warning(
                'ENTERING CRISIS MODE — index level %.4f is %.1f%% below trend SMA',
                proxy_current_level, drawdown * 100,
            )

        # Recovery: exit crisis when index recovers 3% above transition level
        if self.in_buffer_transition and self.transition_price is not None:
            recovery_target = self.transition_price * (1 + config.RECOVERY_ABOVE_TRANSITION)
            if proxy_current_level >= recovery_target:
                logger.info(
                    'EXITING CRISIS MODE — index level %.4f >= recovery target %.4f',
                    proxy_current_level, recovery_target,
                )
                self.in_buffer_transition = False
                self.transition_price = None

        return halt_rebalancing, self.in_buffer_transition

    # ------------------------------------------------------------------
    # Annual evaluations — run once in November
    # ------------------------------------------------------------------
    def evaluate_inflation_freeze(self, proxy_current_price, proxy_sma_12mo):
        """
        Decide whether the annual 3% inflation raise should be
        skipped this year.

        Compares the synthetic Growth-asset index against its
        12-month SMA (built from monthly bars).  If the index is
        down 5% or more relative to its 12-month SMA, the freeze
        is applied: the operator's monthly withdrawal stays at
        last year's level rather than getting the standard 3%
        bump.

        Why the 12-month window (vs the 10-month trend window
        used by the weekly circuit breaker)?  Different time
        horizons for different decisions.  The weekly circuit
        breaker is a "are we IN a downturn right now" check; the
        12-month is a "did the WHOLE YEAR justify a raise"
        check.  A short sharp dip and recovery within 12 months
        shouldn't freeze the inflation raise; only a sustained
        annual underperformance should.

        Why freeze vs. just skipping?  Sequence-of-returns risk.
        Compounding a raise on top of a down year accelerates
        portfolio depletion in the worst-case scenario.  Skipping
        the raise during downturns preserves capital for when
        markets recover.

        Returns True if the freeze should be applied.  Returns
        False on invalid inputs (caller treats this as "raise
        proceeds normally") — this is the safer default because
        a missed freeze (operator gets the 3% raise during a
        questionable year) is recoverable next year, but an
        accidental freeze (operator's income doesn't track
        inflation when it should) creates real lifestyle pain.
        """
        if proxy_sma_12mo is None or proxy_sma_12mo <= 0:
            return False
        if proxy_current_price is None or proxy_current_price <= 0:
            return False

        drawdown = (proxy_current_price - proxy_sma_12mo) / proxy_sma_12mo
        freeze = drawdown <= config.INFLATION_FREEZE_THRESHOLD

        logger.info(
            'Inflation freeze eval — proxy: %.2f, SMA-12mo: %.2f, '
            'drawdown: %.2f%%, freeze: %s',
            proxy_current_price, proxy_sma_12mo, drawdown * 100, freeze,
        )
        return freeze

    def evaluate_november_bonus(self, current_growth_value, prev_year_growth_value):
        """
        Check whether the growth bucket earned a special dividend.

        Trigger: YoY return on the growth bucket > 25 percent
                 (config.BONUS_GROWTH_YOY_THRESHOLD)
        Action:  Take config.BONUS_EXCESS_TAKE_RATE of the excess
                 above the trigger threshold.  Currently 5 percent
                 of the excess (i.e., a $100k growth bucket up 30%
                 YoY — 5% over the 25% trigger — yields a $250
                 bonus: $100k * 0.05 excess * 0.05 take rate).
                 Pre-fix the docstring claimed "20 percent of the
                 excess" which was a holdover from an earlier
                 design iteration; the actual config value (and
                 the implementation below) is 5 percent.

        The 5%-of-5%-excess math is intentionally conservative:
        a banner year (Growth up 30% YoY) yields a small bonus
        (a few hundred dollars on a $100k bucket) rather than
        a large one.  The philosophy is that the BUFFER is the
        primary defense and ad-hoc cash extractions risk
        depleting the structure that makes high withdrawal
        rates sustainable.  A small annual bonus is enough to
        reward a banner year without compromising the
        decumulation model.

        Edge case: prev_year_growth_value <= 0.  This happens
        on the FIRST November after setup (we don't have a
        prior-November snapshot to compare against).  Return 0
        and let the caller skip the bonus this year; from the
        SECOND November onward there will be a valid prior
        snapshot.

        Returns the bonus dollar amount (0.0 if not triggered).
        """
        if prev_year_growth_value <= 0:
            return 0.0

        yoy_return = (
            (current_growth_value - prev_year_growth_value)
            / prev_year_growth_value
        )

        if yoy_return > config.BONUS_GROWTH_YOY_THRESHOLD:
            # bonus = (prev_growth) * (yoy_return - threshold) * take_rate
            # The base of the percentage is the PRIOR-year value,
            # not the current value, because we want the bonus
            # tied to the run-up that's already happened (not
            # extrapolating from the new high-water mark).
            excess_pct = yoy_return - config.BONUS_GROWTH_YOY_THRESHOLD
            bonus = (prev_year_growth_value * excess_pct) * config.BONUS_EXCESS_TAKE_RATE
            logger.info(
                'November bonus triggered — YoY: %.1f%%, excess: %.1f%%, '
                'bonus: $%.2f',
                yoy_return * 100, excess_pct * 100, bonus,
            )
            return bonus

        logger.info(
            'November bonus NOT triggered — YoY: %.1f%% (threshold: %.1f%%)',
            yoy_return * 100, config.BONUS_GROWTH_YOY_THRESHOLD * 100,
        )
        return 0.0
