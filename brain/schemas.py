"""FROZEN strict output schemas for each 0G checkpoint (SPEC §10.1).

Model output is parsed as JSON and validated here. Invalid -> retry once -> SAFE_STOP.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore")


class HazardEncounterOut(_Strict):          # CP-H1
    is_hazard: bool
    type: Literal["accident", "road_works", "waterlogging", "none"]
    severity: float = Field(ge=0, le=1)
    passable_for: list[Literal["rover", "drone"]]
    confidence: float = Field(ge=0, le=1)
    description: str = ""


class RouteAheadOut(_Strict):               # CP-H2
    action: Literal["proceed", "reroute", "wait", "handoff_to_drone"]
    reason: str
    trust_report: bool


class VerificationOut(_Strict):             # CP-V
    verdict: Literal["confirm", "dispute"]
    observed_type: Literal["accident", "road_works", "waterlogging", "none"]
    confidence: float = Field(ge=0, le=1)
    reason: str = ""


class DeliveryArrivalOut(_Strict):          # CP-D
    at_correct_location: bool
    confidence: float = Field(ge=0, le=1)


class InquiryOut(_Strict):                  # CP-Q (bystander voice inquiry)
    what_happened: str
    cause: str = ""
    traffic_blocked: bool
    eta_minutes: Optional[float] = Field(default=None, ge=0)
    confidence: float = Field(ge=0, le=1)
    agrees_with_photo: bool
    summary_hi: str
    summary_en: str


OUTPUT_SCHEMAS: dict[str, type[_Strict]] = {
    "CP-H1": HazardEncounterOut,
    "CP-H2": RouteAheadOut,
    "CP-V": VerificationOut,
    "CP-D": DeliveryArrivalOut,
    "CP-Q": InquiryOut,
}
