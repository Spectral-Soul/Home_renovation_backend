"""
webhook.py — receives ElevenLabs post_call_transcription webhook, runs extraction, triggers graph.
"""

import hmac
import hashlib
import time
import os
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from extraction import run_extraction
from graph import run_graph, check_calendar_availability
from fastapi import UploadFile, File
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from daily_report import send_daily_report
from datetime import datetime, timedelta


app = FastAPI()

WEBHOOK_SECRET = os.environ.get("ELEVENLABS_WEBHOOK_SECRET", "")
OWNER_PHONE = os.environ.get("OWNER_PHONE", "")
print(f"[DEBUG] OWNER_PHONE loaded as: '{OWNER_PHONE}'")


scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
scheduler.add_job(send_daily_report, "cron", hour=20, minute=0)

@app.on_event("startup")
async def start_scheduler():
    scheduler.start()

def verify_signature(raw_body: bytes, signature_header: str) -> bool:
    if not signature_header or not WEBHOOK_SECRET:
        return False
    try:
        parts = dict(p.split("=", 1) for p in signature_header.split(","))
        timestamp = parts.get("t", "")
        received_sig = parts.get("v1", "") or parts.get("v0", "")

        if abs(time.time() - int(timestamp)) > 300:
            return False

        signed_payload = f"{timestamp}.{raw_body.decode()}"
        expected_sig = hmac.new(WEBHOOK_SECRET.encode(), signed_payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected_sig, received_sig)
    except Exception:
        return False

class AvailabilityRequest(BaseModel):
    requested_date: str  # expects YYYY-MM-DD

@app.post("/tools/check-availability")
async def check_availability_tool(req: AvailabilityRequest):
    result = await check_calendar_availability(req.requested_date)
    if result["available"]:
        return {"available": True, "message": f"{req.requested_date} is open at {result['confirmed_time']}."}
    return {"available": False, "message": f"{req.requested_date} is not available."}

@app.post("/trigger-daily-report")
async def trigger_daily_report():
    result = send_daily_report()
    return result

@app.post("/webhooks/elevenlabs/post-call")
async def handle_post_call(request: Request):
    raw_body = await request.body()
    signature = request.headers.get("ElevenLabs-Signature", "")

    if not verify_signature(raw_body, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()
    call_data = payload.get("data", {})

    # <<< UNVERIFIED against a real payload — field names are best-guess from
    # ElevenLabs' documented structure, not confirmed against actual data yet.

    transcript_turns = call_data.get("transcript", [])
    transcript_text = "\n".join(
        f"{turn.get('role', 'unknown')}: {turn.get('message', '')}"
        for turn in transcript_turns
    )

    try:
        lead = await run_extraction(transcript_text)
    except Exception as e:
        # extraction failed entirely — log raw transcript, don't lose the call
        print(f"[extraction failed]: {e}")
        return {"status": "extraction_failed", "error": str(e)}

    result = await run_graph({
        "lead": lead,
        "owner_phone": OWNER_PHONE,
        "estimate": None,
        "calendar_slot": None,
        "notify_status": None,
    })

    return {"status": "processed", "result": str(result)}