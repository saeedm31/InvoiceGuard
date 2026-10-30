"""Typed shapes shared by the agent, the checks and the app."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Decision = Literal["pay", "approve", "hold", "reject"]


class InvoiceLine(BaseModel):
    sku: str | None = Field(None, description="Item code if printed, else null")
    description: str
    qty: float = Field(gt=0)
    unit_price: float = Field(ge=0, description="Price per unit in EUR, without VAT")


class ExtractedInvoice(BaseModel):
    """What the model reads off the PDF. Only facts printed on the invoice; null when missing."""

    vendor_name: str
    vendor_vat_id: str | None = None
    invoice_number: str
    invoice_date: str = Field(description="ISO date YYYY-MM-DD")
    po_number: str | None = Field(None, description="Purchase order reference, null if not printed")
    lines: list[InvoiceLine] = Field(min_length=1)
    net: float
    vat_rate: float = Field(ge=0, le=0.3, description="0.23 for 23%; 0 for reverse charge")
    vat: float
    gross: float
    iban: str | None = None
    currency: str = "EUR"

    @field_validator("vendor_vat_id", "iban", "po_number")
    @classmethod
    def _blank_to_none(cls, v):
        v = (v or "").strip()
        return None if v in ("", "-", "n/a", "N/A") else v


class Check(BaseModel):
    code: str  # e.g. PRICE_MISMATCH; "OK_*" codes are passes
    ok: bool
    detail: str
    hard: bool = True  # hard exceptions block payment; soft ones come from scanners and the model


class Findings(BaseModel):
    """What the investigating model reports back. It can only add concerns, never approve."""

    vendor_id: str | None = None
    proposed_decision: Decision
    reasons: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
