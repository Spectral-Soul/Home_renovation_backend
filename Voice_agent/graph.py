"""
graph.py — status distinguishes "no slot" from "calendar broken"
"""
import os
from typing import TypedDict, Optional
from datetime import datetime, timedelta, date as _date_cls
from langgraph.graph import StateGraph, END
from langchain_core.tools import tool
from twilio.rest import Client as TwilioClient
from googleapiclient.discovery import build
from models import LeadQualification, route_lead
from db import get_rate_card_price_from_db, insert_lead_row
# pyrefly: ignore [missing-import]
from oauth import get_valid_credentials, CalendarAuthError


class GraphState(TypedDict):
    lead: Optional[LeadQualification]
    owner_phone: str
    estimate: Optional[dict]
    calendar_slot: Optional[dict]
    notify_status: Optional[str]


@tool
def get_rate_card_price(job_type: str) -> dict:
    """Look up the standard price for a given job_type from the rate card."""
    return get_rate_card_price_from_db(job_type)



async def check_calendar_availability(requested_date: str) -> dict:
    try:
        # Reject anything before today — use the business's own timezone,
        # not server/UTC time, so "today" matches what the owner considers today.
        from zoneinfo import ZoneInfo
        today_local = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        try:
            requested = _date_cls.fromisoformat(requested_date)
        except ValueError:
            return {"requested_date": requested_date, "available": False, "confirmed_time": None,
                    "reason": "invalid_date_format"}

        if requested < today_local:
            return {"requested_date": requested_date, "available": False, "confirmed_time": None,
                    "reason": "date_in_past"}

        creds = await get_valid_credentials()
        service = build("calendar", "v3", credentials=creds)

        day_start = f"{requested_date}T00:00:00Z"
        day_end = f"{requested_date}T23:59:59Z"

        freebusy_result = service.freebusy().query(
            body={"timeMin": day_start, "timeMax": day_end, "items": [{"id": "primary"}]}
        ).execute()

        busy_periods = freebusy_result["calendars"]["primary"]["busy"]

        if not busy_periods:
            return {"requested_date": requested_date, "available": True, "confirmed_time": "09:00", "reason": None}
        return {"requested_date": requested_date, "available": False, "confirmed_time": None, "reason": "no_open_slot"}

    except CalendarAuthError as e:
        return {"requested_date": requested_date, "available": False, "confirmed_time": None,
                "reason": "calendar_auth_error", "error": str(e)}
    except Exception as e:
        return {"requested_date": requested_date, "available": False, "confirmed_time": None,
                "reason": "calendar_error", "error": str(e)}

async def book_calendar_event(date: str, time: str, lead_name: str, lead_phone: str) -> dict:
    creds = await get_valid_credentials()
    service = build("calendar", "v3", credentials=creds)

    start_dt = f"{date}T{time}:00"
    end_dt_obj = datetime.fromisoformat(start_dt) + timedelta(hours=2)

    event = {
        "summary": f"Renovation consult — {lead_name}",
        "description": f"Booked via AI receptionist. Contact: {lead_phone}",
        "start": {"dateTime": start_dt, "timeZone": "Asia/Kolkata"},
        "end": {"dateTime": end_dt_obj.isoformat(), "timeZone": "Asia/Kolkata"},
    }
    return service.events().insert(calendarId="primary", body=event).execute()


async def find_next_available_slot(start_date: str, max_days: int = 14) -> dict:
    """
    Searches forward day-by-day from start_date (inclusive) for the first
    open slot, up to max_days ahead. Returns the first available date found,
    or reason='no_slot_in_range' if none exists within the window.
    """
    current = datetime.fromisoformat(start_date)
    for i in range(max_days):
        candidate = (current + timedelta(days=i)).strftime("%Y-%m-%d")
        result = await check_calendar_availability(candidate)
        if result["available"]:
            return result
        if result.get("reason") in ("calendar_auth_error", "calendar_error"):
            return result
    return {"requested_date": start_date, "available": False, "confirmed_time": None, "reason": "no_slot_in_range"}


TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")

twilio_client: Optional[TwilioClient] = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


async def send_sms_to_owner(owner_phone: str, message: str) -> bool:
    if twilio_client is None or not TWILIO_FROM_NUMBER:
        print(f"[Twilio not configured — would SMS {owner_phone}]: {message}")
        return False
    try:
        twilio_client.messages.create(to=owner_phone, from_=TWILIO_FROM_NUMBER, body=message)
        return True
    except Exception as e:
        print(f"[Twilio send failed]: {e}")
        return False


async def generate_estimate_node(state: GraphState) -> GraphState:
    lead = state["lead"]
    result = get_rate_card_price.invoke({"job_type": lead.job_type})
    state["estimate"] = {
        "amount": result["price"],
        "disclaimer": "Standard estimate; final price may vary based on your specific needs.",
    }
    return state


async def check_calendar_node(state: GraphState) -> GraphState:
    lead = state["lead"]
    if not lead.preferred_date:
        state["calendar_slot"] = {
            "requested_date": None,
            "available": False,
            "reason": "no_preferred_date",
        }
        return state
    requested = lead.preferred_date.isoformat()
    result = await check_calendar_availability(requested)
    state["calendar_slot"] = result
    return state


async def dispatch_node(state: GraphState) -> GraphState:
    slot = state.get("calendar_slot", {})

    if slot.get("available"):
        try:
            await book_calendar_event(
                slot["requested_date"],
                slot["confirmed_time"],
                state["lead"].contact.name,
                state["lead"].contact.phone,
            )
            insert_lead_row(state["lead"], state.get("estimate"), state.get("calendar_slot"), status="booked")
        except CalendarAuthError as e:
            state["notify_status"] = f"calendar_booking_failed: {e}"
            insert_lead_row(state["lead"], state.get("estimate"), state.get("calendar_slot"), status="flagged_for_review")
        return state

    reason = slot.get("reason")
    if reason in ("calendar_auth_error", "calendar_error"):
        state["notify_status"] = f"calendar_check_failed: {slot.get('error')}"
        insert_lead_row(state["lead"], state.get("estimate"), state.get("calendar_slot"), status="flagged_for_review")
    else:
        insert_lead_row(state["lead"], state.get("estimate"), state.get("calendar_slot"), status="no_slot_available")
        await send_sms_to_owner(
            state["owner_phone"],
            f"New lead: {state['lead'].contact.name} ({state['lead'].contact.phone})\n"
            f"Job: {state['lead'].job_type.replace('_', ' ').title()}\n"
            f"Location: {state['lead'].location}\n"
            f"Requested: {slot.get('requested_date', 'no date given')}\n"
            f"Status: Interested but no slot open on that date. Call to reschedule."
        )

    return state


async def generate_estimate_only_node(state: GraphState) -> GraphState:
    lead = state["lead"]
    result = get_rate_card_price.invoke({"job_type": lead.job_type})
    state["estimate"] = {
        "amount": result["price"],
        "disclaimer": "Standard estimate; final price may vary based on your specific needs.",
    }
    insert_lead_row(lead, state["estimate"], None, status="asked_not_booked")
    return state


def build_lead_summary(lead: LeadQualification) -> str:
    return (
        f"Lead needs review:\n"
        f"Name: {lead.contact.name}\nPhone: {lead.contact.phone}\n"
        f"Job: {lead.job_type_raw or lead.job_type}\nLocation: {lead.location}\n"
        f"Timeline: {lead.timeline_raw or lead.timeline}\nBudget: {lead.budget_raw or lead.budget}\n"
        f"Notes: {lead.notes or 'none'}"
    )


async def flag_for_review_node(state: GraphState) -> GraphState:
    lead = state["lead"]
    summary = build_lead_summary(lead)
    sent = await send_sms_to_owner(state["owner_phone"], summary)
    if not sent:
        retry_sent = await send_sms_to_owner(state["owner_phone"], summary)
        state["notify_status"] = "sent_on_retry" if retry_sent else "failed"
    else:
        state["notify_status"] = "sent"
    insert_lead_row(lead, None, None, status="flagged_for_review")
    return state


async def discard_node(state: GraphState) -> GraphState:
    insert_lead_row(state["lead"], None, None, status="discarded")
    return state


def route_lead_wrapper(state: GraphState) -> str:
    return route_lead(state["lead"])


graph = StateGraph(GraphState)
graph.add_node("generate_estimate", generate_estimate_node)
graph.add_node("check_calendar", check_calendar_node)
graph.add_node("dispatch", dispatch_node)
graph.add_node("generate_estimate_only", generate_estimate_only_node)
graph.add_node("flag_for_review", flag_for_review_node)
graph.add_node("discard", discard_node)

graph.set_conditional_entry_point(
    route_lead_wrapper,
    {
        "generate_estimate": "generate_estimate",
        "generate_estimate_only": "generate_estimate_only",
        "flag_for_review": "flag_for_review",
        "discard": "discard",
    },
)

graph.add_edge("generate_estimate", "check_calendar")
graph.add_edge("check_calendar", "dispatch")
graph.add_edge("dispatch", END)
graph.add_edge("generate_estimate_only", END)
graph.add_edge("flag_for_review", END)
graph.add_edge("discard", END)

compiled_graph = graph.compile()


async def run_graph(initial_state: GraphState):
    return await compiled_graph.ainvoke(initial_state)