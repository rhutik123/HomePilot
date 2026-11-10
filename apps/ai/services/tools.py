import logging
from datetime import timedelta
from decimal import Decimal
from typing import Dict, Any, List
from django.db.models import Sum, Count, Q, Avg
from django.utils import timezone

from apps.bills.models import Bill, BillStatus, BillCategory
from apps.payments.models import Payment
from apps.households.models import Household
from apps.recurring.models import RecurringSchedule, RecurrenceFrequency
from apps.audit.models import AuditEvent
from apps.audit.services import AuditService

logger = logging.getLogger(__name__)


def _format_currency(val) -> str:
    if val is None:
        return "0.00"
    return f"{Decimal(str(val)):.2f}"


class HouseholdAITools:
    """
    Controlled, permission-checked tools available to the AI Assistant.
    CRITICAL SECURITY MANDATES:
    1. Household tenant boundary is strictly checked for every operation.
    2. The AI model is NEVER given raw SQL or database execution access.
    3. All figures, sums, and balances are calculated deterministically by Django ORM / Decimal arithmetic.
    4. The AI only explains the calculated results and never invents numbers.
    """

    @classmethod
    def get_upcoming_bills(
        cls,
        household: Household,
        user,
        days: int = 30,
        only_assigned_to_me: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Returns bills due within the next N days (default 30) that have outstanding balances.
        Optionally filters only bills assigned to the calling user.
        """
        if not household.is_member(user):
            return {"error": "User does not have permission to view household financial records."}

        today = timezone.localdate()
        cutoff = today + timedelta(days=int(days))

        qs = (
            Bill.objects.filter(
                household=household,
                due_date__gte=today,
                due_date__lte=cutoff,
            )
            .exclude(status=BillStatus.PAID)
            .select_related("assigned_to")
            .order_by("due_date")
        )

        if only_assigned_to_me:
            qs = qs.filter(assigned_to=user)

        total_upcoming = qs.aggregate(sum=Sum("outstanding_balance"))["sum"] or Decimal("0.00")
        bills_list = []
        for b in qs:
            bills_list.append({
                "id": b.id,
                "provider_name": b.provider_name,
                "category": b.category,
                "category_display": b.get_category_display(),
                "amount": _format_currency(b.amount),
                "outstanding_balance": _format_currency(b.outstanding_balance),
                "currency": b.currency,
                "due_date": b.due_date.isoformat(),
                "days_until_due": (b.due_date - today).days,
                "status": b.status,
                "assigned_to": getattr(b.assigned_to, "display_name", "Unassigned"),
            })

        return {
            "household": household.name,
            "days_ahead": int(days),
            "only_assigned_to_me": bool(only_assigned_to_me),
            "upcoming_count": len(bills_list),
            "total_upcoming_amount": _format_currency(total_upcoming),
            "bills": bills_list,
        }

    @classmethod
    def get_overdue_bills(
        cls,
        household: Household,
        user,
        only_assigned_to_me: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Returns all bills with past due dates that remain unpaid or partially paid.
        """
        if not household.is_member(user):
            return {"error": "User does not have permission to view household financial records."}

        today = timezone.localdate()
        qs = (
            Bill.objects.filter(
                household=household,
                due_date__lt=today,
                status__in=[BillStatus.PENDING, BillStatus.PARTIALLY_PAID, BillStatus.OVERDUE],
            )
            .select_related("assigned_to")
            .order_by("due_date")
        )

        if only_assigned_to_me:
            qs = qs.filter(assigned_to=user)

        total_overdue = qs.aggregate(sum=Sum("outstanding_balance"))["sum"] or Decimal("0.00")
        overdue_list = []
        for b in qs:
            overdue_list.append({
                "id": b.id,
                "provider_name": b.provider_name,
                "category": b.category,
                "category_display": b.get_category_display(),
                "amount": _format_currency(b.amount),
                "outstanding_balance": _format_currency(b.outstanding_balance),
                "due_date": b.due_date.isoformat(),
                "days_overdue": (today - b.due_date).days,
                "status": b.status,
                "assigned_to": getattr(b.assigned_to, "display_name", "Unassigned"),
            })

        return {
            "household": household.name,
            "overdue_count": len(overdue_list),
            "total_overdue_amount": _format_currency(total_overdue),
            "only_assigned_to_me": bool(only_assigned_to_me),
            "bills": overdue_list,
        }

    @classmethod
    def get_household_spending(
        cls,
        household: Household,
        user,
        year: int = None,
        month: int = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Calculates exact spending figures for a given month or the current calendar month,
        including month-over-month comparisons and category aggregates.
        """
        if not household.is_member(user):
            return {"error": "User does not have permission to view household financial records."}

        today = timezone.localdate()
        target_year = int(year) if year else today.year
        target_month = int(month) if month else today.month

        # Month bills
        month_bills = Bill.objects.filter(
            household=household,
            due_date__year=target_year,
            due_date__month=target_month,
        )

        total_billed = month_bills.aggregate(sum=Sum("amount"))["sum"] or Decimal("0.00")
        total_paid = month_bills.aggregate(sum=Sum("total_paid"))["sum"] or Decimal("0.00")
        total_outstanding = month_bills.aggregate(sum=Sum("outstanding_balance"))["sum"] or Decimal("0.00")

        # Category breakdown for the month
        cat_agg = (
            month_bills.values("category")
            .annotate(total=Sum("amount"), count=Count("id"))
            .order_by("-total")
        )
        cat_breakdown = [
            {
                "category": r["category"],
                "category_display": dict(BillCategory.choices).get(r["category"], r["category"]),
                "total": _format_currency(r["total"]),
                "count": r["count"],
            }
            for r in cat_agg
        ]

        # Previous month comparison
        prev_month = 12 if target_month == 1 else target_month - 1
        prev_year = target_year - 1 if target_month == 1 else target_year
        prev_bills = Bill.objects.filter(
            household=household,
            due_date__year=prev_year,
            due_date__month=prev_month,
        )
        prev_total = prev_bills.aggregate(sum=Sum("amount"))["sum"] or Decimal("0.00")
        diff = total_billed - prev_total
        pct_change = "0.0%"
        if prev_total > Decimal("0.00"):
            pct_change = f"{((diff / prev_total) * Decimal('100.0')):.1f}%"

        return {
            "household": household.name,
            "period": f"{target_year}-{target_month:02d}",
            "total_billed": _format_currency(total_billed),
            "total_paid": _format_currency(total_paid),
            "total_outstanding": _format_currency(total_outstanding),
            "bills_count": month_bills.count(),
            "previous_period": f"{prev_year}-{prev_month:02d}",
            "previous_total": _format_currency(prev_total),
            "difference": _format_currency(diff),
            "percentage_change": pct_change,
            "category_breakdown": cat_breakdown,
        }

    @classmethod
    def get_monthly_spending(
        cls,
        household: Household,
        user,
        year: int = None,
        month: int = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Backward-compatible alias for get_household_spending."""
        return cls.get_household_spending(household, user, year=year, month=month, **kwargs)

    @classmethod
    def compare_category_spending(
        cls,
        household: Household,
        user,
        months: int = 3,
        **kwargs,
    ) -> Dict[str, Any]:
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
                "total": _format_currency(row["total"]),
                "count": row["count"],
                "percentage_of_total": pct,
            })

        return {
            "household": household.name,
            "period_months": int(months),
            "grand_total": _format_currency(grand_total),
            "categories": results,
        }

    @classmethod
    def get_category_spending(
        cls,
        household: Household,
        user,
        category: str,
        months: int = 6,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Calculates spending history for a specific category (e.g. ELECTRICITY, WATER, INTERNET)
        over the past N months.
        """
        if not household.is_member(user):
            return {"error": "User does not have permission to view household financial records."}

        # Resolve category
        category_clean = str(category).strip().upper()
        valid_cats = {c.value: c.label for c in BillCategory}
        matched_cat = None
        for val, label in valid_cats.items():
            if category_clean == val or category_clean == label.upper() or category_clean in label.upper():
                matched_cat = val
                break

        if not matched_cat:
            matched_cat = BillCategory.OTHER

        today = timezone.localdate()
        start_date = today - timedelta(days=int(months) * 30)

        bills = Bill.objects.filter(
            household=household,
            category=matched_cat,
            due_date__gte=start_date,
        ).order_by("-due_date")

        total_spent = bills.aggregate(sum=Sum("amount"))["sum"] or Decimal("0.00")
        avg_spent = bills.aggregate(avg=Avg("amount"))["avg"] or Decimal("0.00")

        bills_records = [
            {
                "id": b.id,
                "provider_name": b.provider_name,
                "amount": _format_currency(b.amount),
                "due_date": b.due_date.isoformat(),
                "status": b.status,
            }
            for b in bills
        ]

        return {
            "household": household.name,
            "category": matched_cat,
            "category_display": dict(BillCategory.choices).get(matched_cat, matched_cat),
            "period_months": int(months),
            "total_spent": _format_currency(total_spent),
            "average_spent": _format_currency(avg_spent),
            "bills_count": len(bills_records),
            "bills": bills_records,
        }

    @classmethod
    def get_provider_spending(
        cls,
        household: Household,
        user,
        provider_name: str,
        months: int = 6,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Analyzes spending and rate changes for a specific provider (e.g. Tata Power, Airtel Broadband)
        over the past N months.
        """
        if not household.is_member(user):
            return {"error": "User does not have permission to view household financial records."}

        today = timezone.localdate()
        start_date = today - timedelta(days=int(months) * 30)

        bills = Bill.objects.filter(
            household=household,
            provider_name__icontains=str(provider_name).strip(),
            due_date__gte=start_date,
        ).order_by("-due_date")

        total_spent = bills.aggregate(sum=Sum("amount"))["sum"] or Decimal("0.00")
        avg_spent = bills.aggregate(avg=Avg("amount"))["avg"] or Decimal("0.00")

        # Rate change analysis between last 2 bills
        rate_change_text = "Not enough bill history for trend analysis."
        percentage_change = "0.0%"
        recent_two = list(bills[:2])
        if len(recent_two) >= 2:
            latest = recent_two[0]
            prev = recent_two[1]
            diff = latest.amount - prev.amount
            if prev.amount > Decimal("0.00"):
                pct = ((diff / prev.amount) * Decimal("100.0")).quantize(Decimal("0.1"))
                percentage_change = f"{pct:+.1f}%"
                direction = "increased" if diff > 0 else ("decreased" if diff < 0 else "unchanged")
                rate_change_text = (
                    f"Charges {direction} by {_format_currency(abs(diff))} INR ({percentage_change}) "
                    f"from {_format_currency(prev.amount)} (due {prev.due_date}) to {_format_currency(latest.amount)} (due {latest.due_date})."
                )

        return {
            "household": household.name,
            "provider_query": provider_name,
            "period_months": int(months),
            "total_spent": _format_currency(total_spent),
            "average_spent": _format_currency(avg_spent),
            "bills_count": bills.count(),
            "rate_change_summary": rate_change_text,
            "percentage_change": percentage_change,
            "bills": [
                {
                    "id": b.id,
                    "amount": _format_currency(b.amount),
                    "due_date": b.due_date.isoformat(),
                    "status": b.status,
                }
                for b in bills
            ],
        }

    @classmethod
    def get_payment_summary(
        cls,
        household: Household,
        user,
        limit: int = 10,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Returns recent payment transactions recorded in the household,
        including payment methods, receipts, and amounts settled.
        """
        if not household.is_member(user):
            return {"error": "User does not have permission to view household financial records."}

        payments_qs = (
            Payment.objects.filter(bill__household=household)
            .select_related("bill", "paid_by")
            .order_by("-payment_date", "-id")
        )

        total_settled = payments_qs.aggregate(sum=Sum("amount"))["sum"] or Decimal("0.00")
        recent = payments_qs[:int(limit)]

        payments_list = []
        for p in recent:
            has_receipt = False
            try:
                has_receipt = hasattr(p, "receipt") and p.receipt is not None
            except Exception:
                has_receipt = False

            payments_list.append({
                "id": p.id,
                "bill_id": p.bill.id,
                "provider_name": p.bill.provider_name,
                "amount": _format_currency(p.amount),
                "payment_date": p.payment_date.isoformat(),
                "payment_method": p.payment_method,
                "transaction_reference": p.transaction_reference or "N/A",
                "paid_by": getattr(p.paid_by, "display_name", "Unknown"),
                "has_receipt": has_receipt,
            })

        return {
            "household": household.name,
            "total_payments_count": payments_qs.count(),
            "total_amount_settled": _format_currency(total_settled),
            "recent_payments": payments_list,
        }

    @classmethod
    def get_recurring_bills(
        cls,
        household: Household,
        user,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Analyzes active recurring bill schedules configured for the household
        and calculates their total monthly recurring commitment.
        """
        if not household.is_member(user):
            return {"error": "User does not have permission to view household financial records."}

        active_schedules = (
            RecurringSchedule.objects.filter(household=household, is_active=True)
            .select_related("assigned_to")
            .order_by("next_due_date")
        )

        monthly_commitment = Decimal("0.00")
        schedules_list = []

        for sched in active_schedules:
            amt = sched.expected_amount or Decimal("0.00")
            monthly_equiv = amt

            if sched.frequency == RecurrenceFrequency.MONTHLY:
                monthly_equiv = amt
            elif sched.frequency == RecurrenceFrequency.QUARTERLY:
                monthly_equiv = (amt / Decimal("3")).quantize(Decimal("0.01"))
            elif sched.frequency == RecurrenceFrequency.YEARLY:
                monthly_equiv = (amt / Decimal("12")).quantize(Decimal("0.01"))

            monthly_commitment += monthly_equiv
            schedules_list.append({
                "id": sched.id,
                "provider_name": sched.provider_name,
                "category": sched.category,
                "category_display": sched.get_category_display(),
                "frequency": sched.frequency,
                "frequency_display": sched.get_frequency_display(),
                "expected_amount": _format_currency(amt),
                "monthly_equivalent": _format_currency(monthly_equiv),
                "next_due_date": sched.next_due_date.isoformat() if sched.next_due_date else None,
                "assigned_to": getattr(sched.assigned_to, "display_name", "Unassigned"),
            })

        return {
            "household": household.name,
            "active_schedules_count": active_schedules.count(),
            "estimated_monthly_recurring_total": _format_currency(monthly_commitment),
            "schedules": schedules_list,
        }

    @classmethod
    def get_unpaid_bills(
        cls,
        household: Household,
        user,
        only_assigned_to_me: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Returns all unpaid or partially paid bills for the household,
        with optional filtering for bills assigned to the calling user.
        """
        if not household.is_member(user):
            return {"error": "User does not have permission to view household financial records."}

        qs = (
            Bill.objects.filter(
                household=household,
                status__in=[BillStatus.PENDING, BillStatus.PARTIALLY_PAID, BillStatus.OVERDUE],
            )
            .select_related("assigned_to")
            .order_by("due_date")
        )

        if only_assigned_to_me:
            qs = qs.filter(assigned_to=user)

        total_due = qs.aggregate(sum=Sum("outstanding_balance"))["sum"] or Decimal("0.00")
        bills_data = []
        for b in qs:
            bills_data.append({
                "id": b.id,
                "provider_name": b.provider_name,
                "category": b.category,
                "category_display": b.get_category_display(),
                "amount": _format_currency(b.amount),
                "total_paid": _format_currency(b.total_paid),
                "outstanding_balance": _format_currency(b.outstanding_balance),
                "currency": b.currency,
                "due_date": b.due_date.isoformat(),
                "status": b.status,
                "assigned_to": getattr(b.assigned_to, "display_name", "Unassigned"),
            })

        return {
            "household": household.name,
            "unpaid_bills_count": len(bills_data),
            "total_outstanding_amount": _format_currency(total_due),
            "only_assigned_to_me": bool(only_assigned_to_me),
            "bills": bills_data,
        }

    @classmethod
    def explain_bill(cls, household: Household, user, bill_id: int, **kwargs) -> Dict[str, Any]:
        """Returns detailed context on a specific bill, including payment records and attached receipts."""
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

        payments = []
        for p in bill.payments.all():
            has_receipt = False
            try:
                has_receipt = hasattr(p, "receipt") and p.receipt is not None
            except Exception:
                has_receipt = False

            payments.append({
                "payment_id": p.id,
                "amount": _format_currency(p.amount),
                "payment_date": p.payment_date.isoformat(),
                "payment_method": p.payment_method,
                "transaction_reference": p.transaction_reference or "N/A",
                "paid_by": getattr(p.paid_by, "display_name", "Unknown"),
                "has_receipt": has_receipt,
            })

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
            "category_display": bill.get_category_display(),
            "amount": _format_currency(bill.amount),
            "total_paid": _format_currency(bill.total_paid),
            "outstanding_balance": _format_currency(bill.outstanding_balance),
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
    "get_upcoming_bills": HouseholdAITools.get_upcoming_bills,
    "get_overdue_bills": HouseholdAITools.get_overdue_bills,
    "get_household_spending": HouseholdAITools.get_household_spending,
    "get_monthly_spending": HouseholdAITools.get_monthly_spending,
    "get_category_spending": HouseholdAITools.get_category_spending,
    "compare_category_spending": HouseholdAITools.compare_category_spending,
    "get_provider_spending": HouseholdAITools.get_provider_spending,
    "get_payment_summary": HouseholdAITools.get_payment_summary,
    "get_recurring_bills": HouseholdAITools.get_recurring_bills,
    "get_unpaid_bills": HouseholdAITools.get_unpaid_bills,
    "explain_bill": HouseholdAITools.explain_bill,
}


def execute_tool_call(tool_name: str, arguments: Dict[str, Any], household: Household, user) -> Dict[str, Any]:
    """Safely executes a registered tool within the household permission scope."""
    handler = TOOL_REGISTRY.get(tool_name)
    if not handler:
        AuditService.log(
            action=AuditEvent.Action.AI_TOOL_EXECUTED,
            actor=user,
            household=household,
            description=f"AI tool '{tool_name}' execution failed: tool not recognized.",
            metadata={"tool_name": tool_name, "arguments": arguments, "success": False},
        )
        return {"error": f"Tool '{tool_name}' is not recognized."}

    try:
        result = handler(household=household, user=user, **arguments)
        is_success = "error" not in result
        AuditService.log(
            action=AuditEvent.Action.AI_TOOL_EXECUTED,
            actor=user,
            household=household,
            description=f"AI tool '{tool_name}' executed {'successfully' if is_success else 'with error'}.",
            metadata={"tool_name": tool_name, "arguments": arguments, "success": is_success},
        )
        return result
    except Exception as exc:
        logger.exception(f"Error executing AI tool '{tool_name}': {exc}")
        AuditService.log(
            action=AuditEvent.Action.AI_TOOL_EXECUTED,
            actor=user,
            household=household,
            description=f"AI tool '{tool_name}' failed with exception: {exc}",
            metadata={"tool_name": tool_name, "arguments": arguments, "error": str(exc), "success": False},
        )
        return {"error": f"Tool execution failed: {str(exc)}"}
