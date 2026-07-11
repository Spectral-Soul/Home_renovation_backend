"""
Lead qualification schema for the home-renovation voice agent.
"""
from typing import Optional
from enum import Enum
import phonenumbers
from pydantic import BaseModel, ConfigDict, Field, field_validator
from datetime import date


class Disposition(str, Enum):
    genuine_interest = "genuine_interest"
    price_check_only = "price_check_only"
    explicitly_declined = "explicitly_declined"
    spam_or_noise = "spam_or_noise"


class JobType(str, Enum):
    kitchen_remodel = "kitchen_remodel"
    bathroom_remodel = "bathroom_remodel"
    basement_remodel = "basement_remodel"
    whole_home_remodel = "whole_home_remodel"
    flooring = "flooring"
    painting = "painting"
    general_renovation = "general_renovation"
    other = "other"


class TimelineUrgency(str, Enum):
    immediate = "immediate"
    within_month = "within_month"
    within_quarter = "within_quarter"
    planning_ahead = "planning_ahead"
    unknown = "unknown"


class BudgetRange(str, Enum):
    under_5k = "under_5k"
    range_5k_15k = "5k_15k"
    range_15k_30k = "15k_30k"
    range_30k_plus = "30k_plus"
    not_disclosed = "not_disclosed"
    unknown = "unknown"

class ContactInfo(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v.strip().lower() in ("unknown", "n/a", "null", ""):
            return None
        try:
            parsed = phonenumbers.parse(v, "IN")
        except phonenumbers.NumberParseException as e:
            raise ValueError(f"Could not parse phone number: {e}")
        if not phonenumbers.is_valid_number(parsed):
            raise ValueError(f"Invalid phone number: {v}")
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)

class LeadQualification(BaseModel):
    model_config = ConfigDict(use_enum_values=True, extra="forbid")

    contact: ContactInfo
    job_type: JobType
    job_type_raw: Optional[str] = None
    location: str
    timeline: TimelineUrgency
    timeline_raw: Optional[str] = None
    preferred_date: Optional[date] = None
    budget: BudgetRange
    budget_raw: Optional[str] = None
    notes: Optional[str] = None
    disposition: Disposition
    confidence: float = Field(..., ge=0.0, le=1.0)

    is_standard_job: bool = Field(
        ..., description="True if job_type maps cleanly to the fixed rate card"
    )
    custom_price: Optional[float] = Field(
        default=None, description="Owner-set price for non-standard jobs, filled in after manual review"
    )


CONFIDENCE_THRESHOLD = 0.7


def route_lead(lead: LeadQualification) -> str:
    if lead.disposition in (Disposition.spam_or_noise.value, Disposition.explicitly_declined.value):
        return "discard"
    if lead.disposition == Disposition.price_check_only.value:
        return "generate_estimate_only"

    if not lead.is_standard_job:
        return "flag_for_review"   # non-standard work always needs owner pricing

    missing_critical = (
        lead.budget == BudgetRange.unknown.value
        or lead.timeline == TimelineUrgency.unknown.value
    )
    if lead.confidence >= CONFIDENCE_THRESHOLD and not missing_critical:
        return "generate_estimate"
    return "flag_for_review"