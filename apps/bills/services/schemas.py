from dataclasses import dataclass, field
from decimal import Decimal
from datetime import date
from typing import Optional, List, Dict


@dataclass
class ExtractedBillData:
    """
    Structured data class representing AI-extracted bill information.
    Includes comprehensive fields, confidence scores, and missing-field detection.
    """
    provider_name: Optional[str] = None
    category: str = "OTHER"
    amount: Optional[Decimal] = None  # Total amount due
    currency: str = "INR"
    billing_period: Optional[str] = None
    billing_period_start: Optional[date] = None
    billing_period_end: Optional[date] = None
    issue_date: Optional[date] = None
    due_date: Optional[date] = None
    reference_number: Optional[str] = None  # Account or invoice number
    consumer_account_number: Optional[str] = None
    bill_number: Optional[str] = None
    previous_balance: Optional[Decimal] = None
    current_charges: Optional[Decimal] = None
    taxes_and_fees: Optional[Decimal] = None
    field_confidences: Dict[str, float] = field(default_factory=dict)
    overall_confidence: float = 0.0
    confidence_notes: Optional[str] = None
    missing_fields: List[str] = field(default_factory=list)

    def __post_init__(self):
        # 1. Normalize Category
        valid_categories = {"ELECTRICITY", "WATER", "GAS", "INTERNET", "MOBILE", "OTHER"}
        upper_cat = (self.category or "").upper().strip()
        self.category = upper_cat if upper_cat in valid_categories else "OTHER"

        # 2. Normalize and sanitize Decimal amounts
        for attr in ["amount", "previous_balance", "current_charges", "taxes_and_fees"]:
            val = getattr(self, attr)
            if val is not None:
                if not isinstance(val, Decimal):
                    try:
                        parsed = Decimal(str(val))
                        setattr(self, attr, parsed)
                    except Exception:
                        setattr(self, attr, None)
                cleaned_val = getattr(self, attr)
                if cleaned_val is not None and cleaned_val < Decimal("0.00"):
                    setattr(self, attr, None)

        # 3. Synchronize reference_number and bill_number/consumer_account_number
        if not self.reference_number:
            self.reference_number = self.bill_number or self.consumer_account_number
        elif not self.bill_number:
            self.bill_number = self.reference_number

    def compute_missing_fields(self):
        """Identifies mandatory fields that could not be extracted."""
        required = ["provider_name", "amount", "due_date"]
        missing = []
        for field_name in required:
            val = getattr(self, field_name)
            if val is None or (isinstance(val, str) and not val.strip()):
                missing.append(field_name)
        self.missing_fields = missing

        # Calculate overall confidence if field confidences provided
        if self.field_confidences:
            scores = list(self.field_confidences.values())
            self.overall_confidence = round(sum(scores) / len(scores), 2)
        elif not missing:
            self.overall_confidence = 0.95
        else:
            present_count = len(required) - len(missing)
            self.overall_confidence = round(present_count / len(required), 2)

    def model_dump(self, mode: str = "json") -> dict:
        """Serializes object to a clean JSON-compatible dictionary."""
        return {
            "provider_name": self.provider_name,
            "category": self.category,
            "amount": str(self.amount) if self.amount is not None else None,
            "currency": self.currency,
            "billing_period": self.billing_period,
            "billing_period_start": self.billing_period_start.isoformat() if self.billing_period_start else None,
            "billing_period_end": self.billing_period_end.isoformat() if self.billing_period_end else None,
            "issue_date": self.issue_date.isoformat() if self.issue_date else None,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "reference_number": self.reference_number,
            "consumer_account_number": self.consumer_account_number,
            "bill_number": self.bill_number,
            "previous_balance": str(self.previous_balance) if self.previous_balance is not None else None,
            "current_charges": str(self.current_charges) if self.current_charges is not None else None,
            "taxes_and_fees": str(self.taxes_and_fees) if self.taxes_and_fees is not None else None,
            "field_confidences": self.field_confidences,
            "overall_confidence": self.overall_confidence,
            "confidence_notes": self.confidence_notes,
            "missing_fields": self.missing_fields,
        }
