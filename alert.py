# alert.py — Retirement Portfolio Manager (RPM)
# ============================================
# Sends operational alerts via email (full detail) and SMS (short summary).
# SMS uses Verizon's email-to-text gateway as primary, with Twilio as an
# optional upgrade later.
#
# Credentials are loaded from environment variables so they never appear
# in source code or state files.

import os
import smtplib
import traceback
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger('rpm.alert')

# --- Credential source: environment variables ---
SMTP_SERVER = os.environ.get('RPM_SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT = int(os.environ.get('RPM_SMTP_PORT', '587'))
EMAIL_SENDER = os.environ.get('RPM_EMAIL_SENDER', '')
EMAIL_PASSWORD = os.environ.get('RPM_EMAIL_PASSWORD', '')  # Gmail App Password
EMAIL_RECIPIENT = os.environ.get('RPM_EMAIL_RECIPIENT', '')
SMS_GATEWAY = os.environ.get('RPM_SMS_GATEWAY', '')  # e.g. 5551234567@vtext.com

# Feature flags
USE_EMAIL = bool(EMAIL_SENDER and EMAIL_RECIPIENT)
USE_SMS = bool(SMS_GATEWAY)


class AlertManager:
    """Dispatches operational alerts to email and/or SMS."""

    def send_success(self, message):
        subject = '[RPM] Run Successful'
        self._dispatch(subject, message)

    def send_error(self, error_message, exception=None):
        subject = '[RPM] FAILURE'
        body_full = error_message + '\n'
        if exception:
            body_full += f'\nTraceback:\n{traceback.format_exc()}'
        body_short = f'{error_message} Check email for details.'
        self._dispatch(subject, body_full, body_short)

    def send_heartbeat(self, core_balances, weights, sgov_pct, sgov_status, days_to_payday):
        """
        Format and dispatch the weekly heartbeat message.

        Excludes USD cash balance from the report — it's an
        operational figure that fluctuates between $0 and the
        monthly withdrawal as the ACH window approaches, and
        showing it would create alarming-looking variance from
        week to week.  The figures the operator wants on the
        weekly heartbeat are: am I on track for payday, is the
        buffer healthy, are the core ETFs in balance.

        Two body variants:
          - Long body (email): full detail with each ticker line.
          - Short body (SMS): one-line summary suitable for the
            160-char SMS limit.
        """
        subject = '[RPM] Weekly Heartbeat'

        body = f"Upcoming Payday: {days_to_payday} days\n\n"
        body += f"Buffer Status: {sgov_status.upper()} ({sgov_pct:.1f}% of Target)\n\n"
        body += "Core ETF Balances:\n"
        for ticker, balance in core_balances.items():
            weight = weights.get(ticker, 0)
            body += f"- {ticker}: ${balance:,.2f} ({weight:.1f}%)\n"

        self._dispatch(subject, body, body_short=f"Payday in {days_to_payday}d. Buffer: {sgov_status} ({sgov_pct:.1f}%).")

    def send_buffer_alert(self, stage, details=""):
        """
        Triggers attention-grabbing alerts for buffer transitions.
        Stages: 'ACTIVATED', 'DRAWDOWN', 'EMPTY_MOVING_TO_FI', 'FI_EMPTY_MOVING_TO_GROWTH', 'RECOVERY'
        """
        subject = f"⚠️ [RPM] BUFFER ALERT: {stage}"
        body = f"The RPM Crisis Buffer has reached stage: {stage}\n\n{details}"
        self._dispatch(subject, body)

    def send_custom(self, subject, body):
        """For ad-hoc alerts (e.g., buffer refill progress)."""
        self._dispatch(subject, body)

    # ------------------------------------------------------------------
    # Internal routing
    # ------------------------------------------------------------------
    def _dispatch(self, subject, body_full, body_short=None):
        """
        Route an alert to email and/or SMS depending on which
        gateways are configured (USE_EMAIL / USE_SMS).

        body_short defaults to body_full if not supplied.  Most
        callers don't specify a short variant because their full
        bodies are already short; the heartbeat is the main case
        where a meaningful difference exists between the two.

        SMS uses the cell carrier's email-to-text gateway
        (e.g. number@vtext.com for Verizon) rather than a
        proper SMS API.  This is free and reliable for low
        volume but has two quirks: messages over ~160 chars get
        truncated or split, and the From: address tends to
        show up in the SMS rather than a clean sender name.
        For a future Twilio upgrade, _dispatch and _send_email
        are the only places that change.
        """
        if body_short is None:
            body_short = body_full

        if USE_EMAIL:
            self._send_email(EMAIL_RECIPIENT, subject, body_full)

        if USE_SMS:
            # SMS gateways work best with short, flat text.  We
            # prepend the subject inline since SMS doesn't have a
            # subject field separate from the body.
            sms_text = f'{subject}: {body_short}'
            self._send_email(SMS_GATEWAY, '', sms_text)

    def _send_email(self, target, subject, body):
        """
        Send a single email via SMTP.

        Opens a fresh SMTP connection per call rather than
        reusing one across recipients.  Reasons:
          - The RPM dispatches at most a handful of emails per
            run (heartbeat + maybe one alert).  Connection-pool
            optimization isn't worth the complexity.
          - SMTP auth tokens (Gmail App Passwords) sometimes
            time out on idle connections; a fresh connection
            per dispatch sidesteps the issue entirely.
          - If one send fails (network blip, Gmail rate-limit),
            the next send isn't affected.

        Critical invariant: this method MUST NOT raise.  An
        alert failure should NEVER crash the main RPM run —
        we'd rather lose visibility on one alert than corrupt
        a state file or leave a half-executed trade pattern.
        Hence the broad except: exception is logged but
        swallowed.
        """
        try:
            msg = MIMEMultipart()
            msg['From'] = EMAIL_SENDER
            msg['To'] = target
            if subject:
                msg['Subject'] = subject
            msg.attach(MIMEText(body, 'plain'))

            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
                server.starttls()
                server.login(EMAIL_SENDER, EMAIL_PASSWORD)
                server.send_message(msg)

            logger.info('Alert sent to %s', target)
        except Exception as e:
            # Alert failures must never crash the main program.
            # Log and move on; the operator may notice the
            # missing alert via the absence of the weekly
            # heartbeat, which is itself a signal.
            logger.error('Failed to send alert to %s: %s', target, e)
