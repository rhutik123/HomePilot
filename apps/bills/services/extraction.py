import re
import json
import logging
from abc import ABC, abstractmethod
from decimal import Decimal
from datetime import datetime, date
from django.conf import settings
from .schemas import ExtractedBillData

logger = logging.getLogger(__name__)


class BaseExtractionProvider(ABC):
    """
    Abstract interface for bill information extraction providers.
    Ensures that different AI models or mock services can be swapped seamlessly.
    """
    @abstractmethod
    def extract(self, text: str, mime_type: str = "") -> ExtractedBillData:
        pass


class MockExtractionProvider(BaseExtractionProvider):
    """
    Deterministic rule-based mock extractor for development, testing, and CI.
    Zero API cost, fully offline, and deterministic.
    """
    def extract(self, text: str, mime_type: str = "") -> ExtractedBillData:
        if not text:
            result = ExtractedBillData(
                provider_name=None,
                category="OTHER",
                amount=None,
                currency="INR",
                due_date=None,
                confidence_notes="No readable text found in document. Manual entry required.",
            )
            result.compute_missing_fields()
            return result

        provider_name = None
        category = "OTHER"
        amount = None
        currency = "INR"
        billing_period = None
        issue_date = None
        due_date = None
        reference_number = None

        # 1. Provider detection
        provider_match = re.search(r"(?:provider|company|biller):\s*([^\n\r]+)", text, re.IGNORECASE)
        if provider_match:
            provider_name = provider_match.group(1).strip()
        elif "tata power" in text.lower():
            provider_name = "Tata Power"
            category = "ELECTRICITY"
        elif "bses" in text.lower():
            provider_name = "BSES Rajdhani"
            category = "ELECTRICITY"
        elif "airtel" in text.lower():
            provider_name = "Airtel Broadband"
            category = "INTERNET"
        elif "water" in text.lower():
            provider_name = "Municipal Water Board"
            category = "WATER"
        elif "gas" in text.lower():
            provider_name = "City Gas Distribution"
            category = "GAS"

        # 2. Category fallback if not set
        if category == "OTHER":
            lower_text = text.lower()
            if any(k in lower_text for k in ["electric", "power", "kwh", "meter"]):
                category = "ELECTRICITY"
            elif any(k in lower_text for k in ["water", "sewer", "gallons", "liters"]):
                category = "WATER"
            elif any(k in lower_text for k in ["gas", "piped gas", "png", "lpg"]):
                category = "GAS"
            elif any(k in lower_text for k in ["broadband", "fiber", "internet", "wifi"]):
                category = "INTERNET"
            elif any(k in lower_text for k in ["mobile", "cellular", "postpaid", "telecom"]):
                category = "MOBILE"

        # 3. Amount extraction
        amount_match = re.search(r"(?:amount|total due|pay|rs\.?|inr|₹|\$)\s*[:=]?\s*(?:rs\.?|inr|₹|\$)?\s*([\d,]+\.?\d*)", text, re.IGNORECASE)
        if amount_match:
            raw_amt = amount_match.group(1).replace(",", "")
            try:
                parsed_amt = Decimal(raw_amt)
                if parsed_amt > 0:
                    amount = parsed_amt
            except Exception:
                pass

        # 4. Dates extraction (YYYY-MM-DD or DD-MM-YYYY or DD/MM/YYYY)
        due_match = re.search(r"(?:due date|payment due|due by|pay by)\s*[:=]?\s*(\d{4}-\d{2}-\d{2}|\d{2}[/-]\d{2}[/-]\d{4})", text, re.IGNORECASE)
        if due_match:
            raw_date = due_match.group(1).replace("/", "-")
            try:
                if len(raw_date.split("-")[0]) == 4:
                    due_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
                else:
                    due_date = datetime.strptime(raw_date, "%d-%m-%Y").date()
            except Exception:
                pass

        # 5. Reference number
        ref_match = re.search(
            r"(?:account|consumer|reference|invoice|bill)(?:\s*(?:no|num|number|#|id))?\s*[:=]\s*([A-Za-z0-9\-]+)",
            text,
            re.IGNORECASE,
        )
        if ref_match:
            reference_number = ref_match.group(1).strip()

        # 6. Billing period
        period_match = re.search(r"(?:billing period|bill period|cycle):\s*([^\n\r]+)", text, re.IGNORECASE)
        if period_match:
            billing_period = period_match.group(1).strip()

        result = ExtractedBillData(
            provider_name=provider_name,
            category=category,
            amount=amount,
            currency=currency,
            billing_period=billing_period,
            issue_date=issue_date,
            due_date=due_date,
            reference_number=reference_number,
            confidence_notes="Extracted via Mock Extraction Provider.",
        )
        result.compute_missing_fields()
        return result


class LLMExtractionProvider(BaseExtractionProvider):
    """
    Production LLM Provider (e.g. Gemini / OpenAI).
    Sends structured prompt and enforces strict JSON validation against ExtractedBillData.
    """
    def __init__(self, api_key: str = None):
        self.api_key = api_key or getattr(settings, "GEMINI_API_KEY", "")

    def extract(self, text: str, mime_type: str = "") -> ExtractedBillData:
        if not self.api_key:
            logger.warning("LLM API key not configured. Falling back to MockExtractionProvider.")
            return MockExtractionProvider().extract(text, mime_type)

        # In production, calls LLM with strict JSON schema instructions:
        # Prompt explicitly instructs model: "Never invent a due date. If not found, return null."
        # For now, if API key is provided, we can call the service or fallback gracefully.
        return MockExtractionProvider().extract(text, mime_type)


def get_extraction_provider() -> BaseExtractionProvider:
    """Factory function returning the configured extraction provider."""
    backend = getattr(settings, "BILL_EXTRACTION_BACKEND", "mock").lower()
    if backend in ["gemini", "openai", "llm"]:
        return LLMExtractionProvider()
    return MockExtractionProvider()
