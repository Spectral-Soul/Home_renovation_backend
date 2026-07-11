"""
extraction.py — LangChain + OpenAI, structured output enforced by Pydantic schema directly.
"""
from datetime import date
from langchain.chat_models import init_chat_model
from models import LeadQualification
import os
from dotenv import load_dotenv

load_dotenv()
llm = init_chat_model(model="gpt-4o-mini")
structured_llm = llm.with_structured_output(LeadQualification)

EXTRACTION_PROMPT = """Extract structured lead data from this home renovation company call transcript.

Today's date is {today}. When the caller gives a relative or partial date 
(e.g. "13th June", "next month", "next Tuesday"), resolve it to the nearest 
future occurrence of that date relative to today's date, in YYYY-MM-DD format. 
If you cannot confidently resolve a date, leave preferred_date as null rather 
than guessing or producing a malformed date.

CRITICAL: When information is not mentioned or not available, use JSON null — 
never the string "unknown", "n/a", "not provided", or similar placeholder text. 
Only the enum fields (job_type, timeline, budget, disposition) should ever 
contain the literal word "unknown" as a value, since it's a defined valid 
option for those specific fields. All other fields (location, phone, notes, 
_raw fields) must be null, not the string "unknown", when the caller didn't 
provide that information.

Rules:
- If the caller never gave their name or phone number clearly, leave contact.phone as null.
- If the caller explicitly said they don't want to proceed, disposition is explicitly_declined.
- If the caller only asked about pricing without expressing intent to book, disposition is price_check_only.
- An initial question about pricing does NOT make disposition price_check_only 
  if the caller later expresses intent to book, requests a specific date, or 
  provides contact info for follow-up. Treat the early price question as small 
  talk in that case — disposition is genuine_interest.
- disposition is spam_or_noise if ANY of the following apply: the transcript is 
  incoherent or nonsensical (random characters, gibberish name like "saop" or 
  "asdf", a phone number given with no other context); the caller never 
  articulates an actual renovation need despite being asked directly; the 
  conversation appears to be a test call, misdial, or accidental connection; 
  or the transcript is too short/garbled to represent a real inquiry. When in 
  doubt between spam_or_noise and genuine_interest for a low-quality transcript, 
  prefer spam_or_noise — a missed real lead costs less than spamming the owner 
  with noise on every broken call.
- is_standard_job is true only if the job clearly matches a normal renovation category; false if unusually custom.
- confidence reflects how complete and clear the extraction is, not how good the lead is.

Transcript:
{transcript}"""

async def run_extraction(transcript: str) -> LeadQualification:
    result = await structured_llm.ainvoke(
        EXTRACTION_PROMPT.format(transcript=transcript, today=date.today().isoformat())
    )
    return result