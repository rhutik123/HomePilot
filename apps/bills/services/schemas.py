from dataclasses import dataclass, field
from decimal import Decimal
from datetime import date
from typing import Optional, List


@dataclass
class ExtractedBillData:
    """
    Pure-Python structured data class representing extracted bill information.
    Performs field sanitization, type normalization, and missing-field detection
    without external binary dependencies.
    """
    provider_name: Optional[str] = None
    category: str = "OTHER"
    amount: Optional[Decimal] = None
    currency: str = "INR"
    billing_period: Optional[str] = None
    issue_date: Optional[date] = None
    due_date: Optional[date] = None
    reference_number: Optional[str] = None
    confidence_notes: Optional[str] = None
    missing_fields: List[str] = field(default_factory=list)

    def __post_init__(self):
        # 1. Normalize Category
        valid_categories = {"ELECTRICITY", "WATER", "GAS", "INTERNET", "MOBILE", "OTHER"}
        upper_cat = (self.category or "").upper().strip()
        self.category = upper_cat if upper_cat in valid_categories else "OTHER"

        # 2. Validate & normalize positive Decimal amount
        if self.amount is not None:
            if not isinstance(self.amount, Decimal):
                try:
                    self.amount = Decimal(str(self.amount))
                except Exception:
                    self.amount = None
            if self.amount is not None and self.amount <= Decimal("0.00"):
                self.amount = None

    def compute_missing_fields(self):
        """Identifies mandatory fields that could not be extracted."""
        required = ["provider_name", "amount", "due_date"]
        missing = []
        for field_name in required:
            val = getattr(self, field_name)
            if val is None or (isinstance(val, str) and not val.strip()):
                missing.append(field_name)
        self.missing_fields = missing

    def model_dump(self, mode: str = "json") -> dict:
        """Serializes object to a clean JSON-compatible dictionary."""
        return {
            "provider_name": self.provider_name,
            "category": self.category,
            "amount": str(self.amount) if self.amount is not None else None,
            "currency": self.currency,
            "billing_period": self.billing_period,
            "issue_date": self.issue_date.isoformat() if self.issue_date else None,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "reference_number": self.reference_number,
            "confidence_notes": self.confidence_notes,
            "missing_fields": self.missing_fields,
        }
