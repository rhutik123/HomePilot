import logging
from datetime import timedelta
from decimal import Decimal
from typing import Dict, Any, List
from django.db.models import Sum, Count, Q
from django.utils import timezone

from apps.bills.models import Bill, BillStatus, BillCategory
from apps.payments.models import Payment
from apps.households.models import Household

logger = logging.getLogger(__name__)


class HouseholdAITools:
    """
    Controlled, permission-checked tools available to the AI Assistant.
    RULE: All financial numbers must be calculated by Django ORM / Decimal arithmetic.
    The AI model explains facts and never hallucinates numbers.
    """
    @classmethod
    def get_unpaid_bills(cls, household: Household, user, **kwargs) -> Dict[str, Any]:
        """Returns all currently pending, partially paid, or overdue bills for the household."""
        if not household.is_member(user):
            return {"error": "User does not have permission to view household financial records."}

        unpaid_bills = (
            Bill.objects.filter(
                household=household,
                status__in=[BillStatus.PENDING, BillStatus.PARTIALLY_PAID, BillStatus.OVERDUE],
            )
            .select_related("assigned_to")
            .order_by("due_date")
        )

        total_due = Decimal("0.00")
        bills_data = []
        for b in unpaid_bills:
            bal = b.outstanding_balance
            total_due += bal
            bills_data.append({
                "id": b.id,
                "provider_name": b.provider_name,
                "category": b.category,
                "amount": str(b.amount),
                "total_paid": str(b.total_paid),
                "outstanding_balance": str(bal),
                "currency": b.currency,
                "due_date": b.due_date.isoformat(),
                "status": b.status,
                "assigned_to": getattr(b.assigned_to, "display_name", "Unassigned"),
            })

        return {
            "household": household.name,
            "unpaid_bills_count": len(bills_data),
            "total_outstanding_amount": str(total_due),
            "currency": unpaid_bills.first().currency if unpaid_bills.exists() else "INR",
            "bills": bills_data,
        }

    @classmethod
    def get_upcoming_bills(cls, household: Household, user, days: int = 30, **kwargs) -> Dict[str, Any]:
        """Returns bills due within the next N days that have not yet been fully paid."""
        if not household.is_member(user):
            return {"error": "User does not have permission to view household financial records."}

        today = timezone.localdate()
        cutoff = today + timedelta(days=int(days))

        upcoming = (
            Bill.objects.filter(
                household=household,
                due_date__gte=today,
                due_date__lte=cutoff,
            )
            .exclude(status=BillStatus.PAID)
            .select_related("assigned_to")
            .order_by("due_date")
        )

        total_upcoming = Decimal("0.00")
        bills_list = []
        for b in upcoming:
            total_upcoming += b.outstanding_balance
            bills_list.append({
                "id": b.id,
                "provider_name": b.provider_name,
                "category": b.category,
                "outstanding_balance": str(b.outstanding_balance),
                "currency": b.currency,
                "due_date": b.due_date.isoformat(),
                "status": b.status,
                "assigned_to": getattr(b.assigned_to, "display_name", "Unassigned"),
            })

        return {
            "household": household.name,
            "days_ahead": days,
            "upcoming_count": len(bills_list),
            "total_upcoming_amount": str(total_upcoming),
            "bills": bills_list,
        }

    @classmethod
    def get_monthly_spending(cls, household: Household, user, year: int = None, month: int = None, **kwargs) -> Dict[str, Any]:
        """Calculates exact total spent, paid, and outstanding for a given month."""
        if not household.is_member(user):
            return {"error": "User does not have permission to view household financial records."}

        today = timezone.localdate()
        target_year = int(year) if year else today.year
        target_month = int(month) if month else today.month

        # Bills due in the target month
        month_bills = Bill.objects.filter(
            household=household,
            due_date__year=target_year,
            due_date__month=target_month,
        )

        total_billed = month_bills.aggregate(sum=Sum("amount"))["sum"] or Decimal("0.00")
        total_paid = month_bills.aggregate(sum=Sum("total_paid"))["sum"] or Decimal("0.00")
        total_outstanding = month_bills.aggregate(sum=Sum("outstanding_balance"))["sum"] or Decimal("0.00")

        # Category breakdown
        category_agg = (
            month_bills.values("category")
            .annotate(total=Sum("amount"), count=Count("id"))
            .order_by("-total")
        )

        category_breakdown = [
            {
                "category": row["category"],
                "total": str(row["total"]),
                "count": row["count"],
            }
            for row in category_agg
        ]

        return {
            "household": household.name,
            "year": target_year,
            "month": target_month,
            "total_billed": str(total_billed),
            "total_paid": str(total_paid),
            "total_outstanding": str(total_outstanding),
            "category_breakdown": category_breakdown,
        }

    @classmethod
    def compare_category_spending(cls, household: Household, user, months: int = 3, **kwargs) -> Dict[str, Any]:
        """Aggregates spending across all categories for the past N months."""
        if not household.is_member(user):
            return {"error": "User does not have permission to view household financial records."}

        today = timezone.localdate()
        start_date = today - timedelta(days=int(months) * 30)

        bills = Bill.objects.filter(
            household=household,
            due_date__gte=start_date,
        )

        grand_total = bills.aggregate(sum=Sum("amount"))["sum"] or Decimal("0.00")

        breakdown = (
            bills.values("category")
            .annotate(total=Sum("amount"), count=Count("id"))
            .order_by("-total")
        )

        results = []
        for row in breakdown:
            pct = "0.0%"
            if grand_total > Decimal("0.00"):
                pct = f"{(row['total'] / grand_total * 100):.1f}%"
            results.append({
                "category": row["category"],
                "total": str(row["total"]),
                "count": row["count"],
                "percentage_of_total": pct,
            })

        return {
            "household": household.name,
            "period_months": months,
            "grand_total": str(grand_total),
            "categories": results,
        }

    @classmethod
    def explain_bill(cls, household: Household, user, bill_id: int, **kwargs) -> Dict[str, Any]:
        """Returns deep context on a bill, including payment records, receipts, and attached documents."""
        if not household.is_member(user):
            return {"error": "User does not have permission to view household financial records."}

        try:
            bill = (
                Bill.objects.select_related("household", "assigned_to", "uploaded_by")
                .prefetch_related("payments__receipt", "document_links__document")
                .get(id=bill_id, household=household)
            )
        except Bill.DoesNotExist:
            return {"error": f"Bill #{bill_id} not found in household '{household.name}'."}

        # Payment history
        payments = []
        for p in bill.payments.all():
            has_receipt = False
            try:
                has_receipt = hasattr(p, "receipt") and p.receipt is not None
            except Exception:
                has_receipt = False

            payments.append({
                "payment_id": p.id,
                "amount": str(p.amount),
                "payment_date": p.payment_date.isoformat(),
                "payment_method": p.payment_method,
                "transaction_reference": p.transaction_reference,
                "paid_by": getattr(p.paid_by, "display_name", "Unknown"),
                "has_receipt": has_receipt,
            })

        # Attached documents
        documents = []
        for link in bill.document_links.all():
            doc = link.document
            documents.append({
                "document_id": doc.id,
                "original_filename": doc.original_filename,
                "file_type": doc.file_type,
                "confidence_score": str(doc.confidence_score) if doc.confidence_score else None,
                "is_primary": link.is_primary,
            })

        return {
            "bill_id": bill.id,
            "provider_name": bill.provider_name,
            "category": bill.category,
            "amount": str(bill.amount),
            "total_paid": str(bill.total_paid),
            "outstanding_balance": str(bill.outstanding_balance),
            "currency": bill.currency,
            "status": bill.status,
            "issue_date": bill.issue_date.isoformat() if bill.issue_date else None,
            "due_date": bill.due_date.isoformat() if bill.due_date else None,
            "billing_period": bill.billing_period,
            "reference_number": bill.reference_number,
            "assigned_to": getattr(bill.assigned_to, "display_name", "Unassigned"),
            "uploaded_by": getattr(bill.uploaded_by, "display_name", "Unknown"),
            "notes": bill.notes,
            "payments_count": len(payments),
            "payments": payments,
            "attached_documents_count": len(documents),
            "attached_documents": documents,
        }


TOOL_REGISTRY = {
    "get_unpaid_bills": HouseholdAITools.get_unpaid_bills,
    "get_upcoming_bills": HouseholdAITools.get_upcoming_bills,
    "get_monthly_spending": HouseholdAITools.get_monthly_spending,
    "compare_category_spending": HouseholdAITools.compare_category_spending,
    "explain_bill": HouseholdAITools.explain_bill,
}


def execute_tool_call(tool_name: str, arguments: Dict[str, Any], household: Household, user) -> Dict[str, Any]:
    """Safely executes a registered tool within the household permission scope."""
    handler = TOOL_REGISTRY.get(tool_name)
    if not handler:
        return {"error": f"Tool '{tool_name}' is not recognized."}

    try:
        return handler(household=household, user=user, **arguments)
    except Exception as exc:
        logger.exception(f"Error executing AI tool '{tool_name}': {exc}")
        return {"error": f"Tool execution failed: {str(exc)}"}
