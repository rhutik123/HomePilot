import re
import json
import logging
from abc import ABC, abstractmethod
from decimal import Decimal
from datetime import datetime, date
from typing import Optional, Dict, Any
from django.conf import settings
from .schemas import ExtractedBillData

logger = logging.getLogger(__name__)


class AIExtractionError(Exception):
    """Raised when an AI extraction provider fails."""
    pass


class AIExtractionConfigurationError(AIExtractionError):
    """Raised when an AI provider is selected but missing required credentials."""
    pass


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
                overall_confidence=0.0,
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
        consumer_account_number = None
        bill_number = None

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

        issue_match = re.search(r"(?:bill date|issue date|date of bill)\s*[:=]?\s*(\d{4}-\d{2}-\d{2}|\d{2}[/-]\d{2}[/-]\d{4})", text, re.IGNORECASE)
        if issue_match:
            raw_issue = issue_match.group(1).replace("/", "-")
            try:
                if len(raw_issue.split("-")[0]) == 4:
                    issue_date = datetime.strptime(raw_issue, "%Y-%m-%d").date()
                else:
                    issue_date = datetime.strptime(raw_issue, "%d-%m-%Y").date()
            except Exception:
                pass

        # 5. Reference and account numbers
        ref_match = re.search(
            r"(?:account|consumer|reference|invoice|bill)(?:\s*(?:no|num|number|#|id))?\s*[:=]\s*([A-Za-z0-9\-]+)",
            text,
            re.IGNORECASE,
        )
        if ref_match:
            reference_number = ref_match.group(1).strip()
            bill_number = reference_number
            consumer_account_number = reference_number

        # 6. Billing period
        period_match = re.search(r"(?:billing period|bill period|cycle):\s*([^\n\r]+)", text, re.IGNORECASE)
        if period_match:
            billing_period = period_match.group(1).strip()

        field_confidences = {}
        if provider_name:
            field_confidences["provider_name"] = 0.95
        if amount:
            field_confidences["amount"] = 0.95
        if due_date:
            field_confidences["due_date"] = 0.95

        result = ExtractedBillData(
            provider_name=provider_name,
            category=category,
            amount=amount,
            currency=currency,
            billing_period=billing_period,
            issue_date=issue_date,
            due_date=due_date,
            reference_number=reference_number,
            consumer_account_number=consumer_account_number,
            bill_number=bill_number,
            field_confidences=field_confidences,
            confidence_notes="Extracted via Mock Extraction Provider.",
            provider_used="mock",
            model_used="rule-based-mock",
        )
        result.compute_missing_fields()
        return result


class GeminiExtractionProvider(BaseExtractionProvider):
    """
    Genuine Hosted AI Provider using Google Gemini (via google-genai SDK).
    Enforces structured JSON extraction, field-level confidence, and missing field detection.
    """
    MODEL_NAME = "gemini-2.5-flash"
    provider_name = "gemini"

    def __init__(self, api_key: Optional[str] = None, allow_fallback: bool = False):
        self.api_key = api_key or getattr(settings, "GEMINI_API_KEY", "")
        self.allow_fallback = allow_fallback or getattr(settings, "BILL_EXTRACTION_FALLBACK_TO_MOCK", False)

    def extract(self, text: str, mime_type: str = "") -> ExtractedBillData:
        if not self.api_key:
            raise AIExtractionConfigurationError(
                "GEMINI_API_KEY is not configured in the environment. Cannot perform AI extraction."
            )

        if not text or len(text.strip()) < 10:
            logger.info("Document contains insufficient text. Returning empty extraction.")
            res = ExtractedBillData(
                confidence_notes="Document text is empty or too short for AI extraction.",
                provider_used="gemini",
                model_used=self.MODEL_NAME,
            )
            res.compute_missing_fields()
            return res

        prompt = (
            "You are an expert utility bill document parsing system for HomePilot. "
            "Analyze the following extracted bill document text and extract structured fields in JSON format.\n\n"
            "Strict Extraction Rules:\n"
            "1. Output ONLY valid JSON matching the schema below. No markdown backticks, no prose.\n"
            "2. Never hallucinate or invent dates, amounts, or reference numbers. "
            "If a field cannot be definitively found, set its value to null.\n"
            "3. category MUST be one of: 'ELECTRICITY', 'WATER', 'GAS', 'INTERNET', 'MOBILE', 'OTHER'.\n"
            "4. Dates MUST use ISO 8601 format: 'YYYY-MM-DD'. If day/month cannot be determined, return null.\n"
            "5. All monetary amounts must be numerical strings (e.g. '2450.00').\n"
            "6. Provide a field_confidences object with a confidence score from 0.0 to 1.0 for each detected field.\n\n"
            "JSON Schema:\n"
            "{\n"
            '  "provider_name": string or null,\n'
            '  "category": string,\n'
            '  "total_amount": string or null,\n'
            '  "currency": string,\n'
            '  "consumer_account_number": string or null,\n'
            '  "bill_number": string or null,\n'
            '  "billing_period": string or null,\n'
            '  "billing_period_start": string or null,\n'
            '  "billing_period_end": string or null,\n'
            '  "issue_date": string or null,\n'
            '  "due_date": string or null,\n'
            '  "previous_balance": string or null,\n'
            '  "current_charges": string or null,\n'
            '  "taxes_and_fees": string or null,\n'
            '  "field_confidences": {"provider_name": float, "total_amount": float, "due_date": float}\n'
            "}\n\n"
            f"--- BILL DOCUMENT TEXT ---\n{text}\n--- END TEXT ---"
        )

        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=self.api_key)
            response = client.models.generate_content(
                model=self.MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.0,
                ),
            )

            raw_json = response.text.strip()
            # Strip markdown codeblocks if model inadvertently added them
            if raw_json.startswith("```"):
                raw_json = re.sub(r"^```(?:json)?\s*", "", raw_json)
                raw_json = re.sub(r"\s*```$", "", raw_json)

            data = json.loads(raw_json)
            result = self._parse_json_response(data)
            result.provider_used = "gemini"
            result.model_used = self.MODEL_NAME
            return result

        except json.JSONDecodeError as json_err:
            logger.error("Gemini AI returned non-JSON output: %s", json_err)
            raise AIExtractionError(f"AI returned invalid JSON: {json_err}")
        except Exception as exc:
            error_type = exc.__class__.__name__
            logger.error("Gemini AI extraction failed (%s): %s", error_type, str(exc))
            if self.allow_fallback:
                logger.warning("Falling back to rule-based mock extractor because allow_fallback is enabled.")
                fallback = MockExtractionProvider().extract(text, mime_type)
                fallback.provider_used = "mock"
                fallback.model_used = "rule-based-mock-fallback"
                fallback.confidence_notes = f"Fallback extractor used due to AI provider failure: {str(exc)}"
                return fallback
            raise AIExtractionError(f"Gemini AI extraction failed ({error_type}): {str(exc)}")

    def _parse_json_response(self, data: Dict[str, Any]) -> ExtractedBillData:
        """Parses and validates Gemini JSON output into typed ExtractedBillData."""
        def parse_date(val: Optional[str]) -> Optional[date]:
            if not val:
                return None
            try:
                return datetime.strptime(val.strip(), "%Y-%m-%d").date()
            except Exception:
                return None

        def parse_decimal(val: Optional[Any]) -> Optional[Decimal]:
            if val is None:
                return None
            try:
                clean = str(val).replace(",", "").replace("$", "").replace("₹", "").strip()
                parsed = Decimal(clean)
                return parsed if parsed >= Decimal("0.00") else None
            except Exception:
                return None

        issue_date = parse_date(data.get("issue_date"))
        due_date = parse_date(data.get("due_date"))
        period_start = parse_date(data.get("billing_period_start"))
        period_end = parse_date(data.get("billing_period_end"))

        # Logical sanity check: issue_date cannot be after due_date
        if issue_date and due_date and issue_date > due_date:
            logger.warning("Extracted issue_date is after due_date; nullifying issue_date.")
            issue_date = None

        field_confidences = data.get("field_confidences", {})
        if not isinstance(field_confidences, dict):
            field_confidences = {}

        result = ExtractedBillData(
            provider_name=data.get("provider_name"),
            category=data.get("category", "OTHER"),
            amount=parse_decimal(data.get("total_amount")),
            currency=data.get("currency") or "INR",
            billing_period=data.get("billing_period"),
            billing_period_start=period_start,
            billing_period_end=period_end,
            issue_date=issue_date,
            due_date=due_date,
            reference_number=data.get("bill_number") or data.get("consumer_account_number"),
            consumer_account_number=data.get("consumer_account_number"),
            bill_number=data.get("bill_number"),
            previous_balance=parse_decimal(data.get("previous_balance")),
            current_charges=parse_decimal(data.get("current_charges")),
            taxes_and_fees=parse_decimal(data.get("taxes_and_fees")),
            field_confidences=field_confidences,
            confidence_notes=f"Extracted via Gemini AI ({self.MODEL_NAME}).",
            provider_used="gemini",
            model_used=self.MODEL_NAME,
        )
        result.compute_missing_fields()
        return result


class LLMExtractionProvider(GeminiExtractionProvider):
    """Alias for GeminiExtractionProvider to maintain backwards compatibility."""
    pass


def get_extraction_provider() -> BaseExtractionProvider:
    """Factory function returning the configured extraction provider."""
    backend = getattr(settings, "BILL_EXTRACTION_BACKEND", "mock").lower()
    if backend in ["gemini", "google", "llm"]:
        api_key = getattr(settings, "GEMINI_API_KEY", "")
        return GeminiExtractionProvider(api_key=api_key)
    return MockExtractionProvider()
