"""Read-only published surcharge lookup/calculation, no quote or RFQ side effects."""
from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from services import public_surcharges as service

router = APIRouter(prefix="/api/public/surcharges", tags=["public tariffs"])


class Calculation(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    tariff_id: str = Field(min_length=3, max_length=100)
    provider: Literal["finnlines", "bluedart", "swissport", "maersk", "arkas"]
    service: Literal["RORO", "AIR", "FCL", "LCL", "ROAD_FTL", "ROAD_LTL", "COURIER", "RAIL_FCL"]
    origin: str = Field(pattern=r"^[A-Z]{2}[A-Z0-9]{3}$")
    destination: str = Field(pattern=r"^[A-Z]{2}[A-Z0-9]{3}$")
    on_date: date
    date_basis: Literal["service_date", "carrier_price_calculation_date", "booking_date"] = "service_date"
    equipment: Literal["20GP", "40GP", "40HC", "45HC", "20RF", "40RF"] | None = None
    containers: int | None = Field(default=None, gt=0, le=10000)
    lane_metres: Decimal | None = Field(default=None, gt=0, le=100000)
    chargeable_kg: Decimal | None = Field(default=None, gt=0, le=10000000)
    caf_subject_amount: Decimal | None = Field(default=None, gt=0, le=1000000000)
    base_currency: Literal["EUR", "USD", "INR"] | None = None


@router.get("")
async def tariffs():
    return await service.catalog()


@router.post("/calculate")
async def calculate(body: Calculation):
    try:
        return await service.calculate(body.tariff_id, body.model_dump(mode="json"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
