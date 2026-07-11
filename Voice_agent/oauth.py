"""
oauth.py — Google OAuth2 flow for owner calendar access. Single-owner system.
Async throughout — Google token refresh and Supabase calls are offloaded
to threads, since the underlying supabase-py client is synchronous.
"""

import os
import asyncio
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, JSONResponse
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from google.auth.exceptions import RefreshError
from db import supabase

app = FastAPI()

CLIENT_SECRETS_FILE = "client_secret.json"
SCOPES = ["https://www.googleapis.com/auth/calendar"]
REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:8000/oauth/callback")

STATE_TTL_SECONDS = 600

# single declaration now — each state maps to a dict, not a bare float
_pending_states: dict[str, dict] = {}


class CalendarAuthError(Exception):
    """Raised when calendar credentials are missing or invalid. Not FastAPI-specific."""
    pass


def _cleanup_expired_states():
    now = datetime.now(timezone.utc).timestamp()
    expired = [
        s for s, entry in _pending_states.items()
        if now - entry["created_at"] > STATE_TTL_SECONDS
    ]
    for s in expired:
        _pending_states.pop(s, None)


@app.get("/oauth/start")
def start_oauth():
    _cleanup_expired_states()
    flow = Flow.from_client_secrets_file(CLIENT_SECRETS_FILE, scopes=SCOPES, redirect_uri=REDIRECT_URI)
    auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")

    _pending_states[state] = {
        "created_at": datetime.now(timezone.utc).timestamp(),
        "code_verifier": flow.code_verifier,
    }
    return RedirectResponse(auth_url)


@app.get("/oauth/callback")
async def oauth_callback(code: str = None, state: str = None, error: str = None):
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth denied or failed: {error}")

    if not state or state not in _pending_states:
        raise HTTPException(status_code=400, detail="Invalid or missing state — possible CSRF attempt")

    stored = _pending_states.pop(state)
    code_verifier = stored["code_verifier"]

    flow = Flow.from_client_secrets_file(CLIENT_SECRETS_FILE, scopes=SCOPES, redirect_uri=REDIRECT_URI)
    flow.code_verifier = code_verifier

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    try:
        await asyncio.to_thread(flow.fetch_token, code=code)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to exchange code for token: {e}")

    creds = flow.credentials

    if not creds.refresh_token:
        raise HTTPException(
            status_code=400,
            detail="No refresh_token returned — owner may need to revoke prior access and reconnect",
        )

    await asyncio.to_thread(
        lambda: supabase.table("owner_calendar_credentials").upsert({
            "id": 1,
            "refresh_token": creds.refresh_token,
            "connected_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    )

    return {"status": "connected", "message": "Calendar access granted"}


async def get_valid_credentials() -> Credentials:
    result = await asyncio.to_thread(
        lambda: supabase.table("owner_calendar_credentials").select("*").eq("id", 1).execute()
    )
    if not result.data:
        raise CalendarAuthError("No calendar connected yet — owner must complete /oauth/start")

    stored_refresh_token = result.data[0]["refresh_token"]

    creds = Credentials(
        token=None,
        refresh_token=stored_refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        scopes=SCOPES,
    )

    last_error = None
    for attempt in range(2):
        try:
            await asyncio.to_thread(creds.refresh, GoogleRequest())
            if creds.refresh_token and creds.refresh_token != stored_refresh_token:
                await asyncio.to_thread(
                    lambda: supabase.table("owner_calendar_credentials").upsert({
                        "id": 1,
                        "refresh_token": creds.refresh_token,
                        "connected_at": datetime.now(timezone.utc).isoformat(),
                    }).execute()
                )
            return creds
        except RefreshError as e:
            last_error = e
            if attempt == 0:
                await asyncio.sleep(1)

    raise CalendarAuthError(f"Calendar token expired or revoked after retry: {last_error}")


@app.exception_handler(CalendarAuthError)
async def calendar_auth_error_handler(request: Request, exc: CalendarAuthError):
    return JSONResponse(status_code=401, content={"detail": str(exc)})

