"""
db.py — single Supabase client + all direct database operations.
"""
import os
from typing import Optional
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("------Supabase Connection Success-----")
else:
    print("[db.py] WARNING: SUPABASE_URL / SUPABASE_SERVICE_KEY not set — running in stub mode")


def get_rate_card_price_from_db(job_type: str) -> dict:
    """Look up standard price for a job_type from the rate_card table."""
    if supabase is None:
        return {"job_type": job_type, "price": 0.0}

    result = supabase.table("rate_card").select("*").eq("job_type", job_type).execute()
    if result.data:
        return {"job_type": job_type, "price": result.data[0]["price"]}
    return {"job_type": job_type, "price": None}


def insert_lead_row(lead, estimate: Optional[dict], calendar_slot: Optional[dict], status: str) -> None:
    """Insert one qualified lead/call into the leads table."""
    if lead is None:
        row = {"status": status, "notes": "extraction not implemented — raw log only"}
    else:
        row = {
            "name": lead.contact.name,
            "phone": lead.contact.phone,
            "job_type": lead.job_type,
            "location": lead.location,
            "timeline": lead.timeline,
            "preferred_date": lead.preferred_date.isoformat() if lead.preferred_date else None,
            "budget": lead.budget,
            "disposition": lead.disposition,
            "confidence": lead.confidence,
            "is_standard_job": lead.is_standard_job,
            "custom_price": lead.custom_price,
            "estimate": estimate,
            "calendar_slot": calendar_slot,
            "status": status,
        }

    if supabase is None:
        print(f"[db.py] STUB MODE — would insert: {row}")
        return

    supabase.table("leads").insert(row).execute()



