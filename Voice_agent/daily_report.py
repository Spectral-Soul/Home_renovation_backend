"""
daily_report.py — sends the daily leads report by email via Resend.
Idempotent: will not send twice for the same calendar date, even if
triggered manually and by the scheduled cron on the same day.
"""

import csv
import io
import base64
import os
from datetime import date, datetime, timedelta, timezone

import resend
from postgrest.exceptions import APIError
from supabase import create_client

resend.api_key = os.environ.get("RESEND_API_KEY", "")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "")
FROM_ADDRESS = os.environ.get("RESEND_FROM_ADDRESS", "Leads Report <reports@agent.tickers.online>")


class DailyReportError(Exception):
    """Raised when the report cannot be sent (config, data, or send failure)."""
    pass


def fetch_todays_leads() -> list[dict]:
    today_start = datetime.combine(date.today(), datetime.min.time()).isoformat()
    tomorrow_start = (datetime.combine(date.today(), datetime.min.time()) + timedelta(days=1)).isoformat()
    result = (
        supabase.table("leads")
        .select("*")
        .gte("created_at", today_start)
        .lt("created_at", tomorrow_start)
        .execute()
    )
    return result.data


def build_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def _already_sent_today() -> bool:
    result = (
        supabase.table("daily_report_log")
        .select("report_date")
        .eq("report_date", date.today().isoformat())
        .execute()
    )
    return len(result.data) > 0


def _mark_sent_today(lead_count: int) -> bool:
    """
    Atomically claims today's send slot. Returns True if this call won the
    claim (i.e. no report was logged for today before this insert).
    Returns False if another process/trigger already claimed it — caller
    should NOT send in that case. Relies on report_date being a PRIMARY KEY.
    """
    try:
        supabase.table("daily_report_log").insert({
            "report_date": date.today().isoformat(),
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "lead_count": lead_count,
        }).execute()
        return True
    except APIError as e:
        # Unique violation on report_date = someone else already sent today's report
        if "duplicate key" in str(e).lower() or "23505" in str(e):
            return False
        raise


def send_daily_report(force: bool = False) -> dict:
    """
    Sends the daily leads report. Idempotent per calendar date unless
    force=True (use force only for deliberate manual re-sends, e.g. testing).
    Returns a status dict instead of just printing, so callers (API routes)
    can report back what actually happened.
    """
    if not OWNER_EMAIL:
        raise DailyReportError("OWNER_EMAIL is not set")
    if not resend.api_key:
        raise DailyReportError("RESEND_API_KEY is not set")

    if not force and _already_sent_today():
        msg = f"[Daily report] already sent for {date.today().isoformat()} — skipping"
        print(msg)
        return {"status": "skipped", "reason": "already_sent_today"}

    rows = fetch_todays_leads()

    # Claim the slot BEFORE sending, so two near-simultaneous triggers
    # (e.g. manual trigger racing the cron) can't both pass the check above
    # and both send. Only the one that wins the insert actually emails.
    if not force:
        won_claim = _mark_sent_today(len(rows))
        if not won_claim:
            msg = f"[Daily report] lost race for {date.today().isoformat()} — another trigger already sent it"
            print(msg)
            return {"status": "skipped", "reason": "race_lost"}

    csv_data = build_csv(rows)
    params = {
        "from": FROM_ADDRESS,
        "to": [OWNER_EMAIL],
        "subject": f"Daily Leads Report — {date.today().isoformat()}",
        "text": f"{len(rows)} leads today." if rows else "No leads today.",
    }
    if rows:
        params["attachments"] = [{
            "filename": f"leads_{date.today().isoformat()}.csv",
            "content": base64.b64encode(csv_data.encode()).decode(),
        }]

    try:
        resend.Emails.send(params)
    except Exception as e:
        # Sending failed after we already claimed the slot — remove the
        # claim so a retry (manual or next cron tick) can actually send.
        if not force:
            supabase.table("daily_report_log").delete().eq(
                "report_date", date.today().isoformat()
            ).execute()
        raise DailyReportError(f"Resend send failed: {e}") from e

    print(f"[Daily report] sent {len(rows)} leads to {OWNER_EMAIL}")
    return {"status": "sent", "lead_count": len(rows)}