import csv
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional
from django.http import HttpResponse
from django.utils import timezone

from apps.bills.models import Bill, BillCategory, BillStatus
from apps.payments.models import Payment
from apps.households.models import Household
from apps.recurring.models import RecurringSchedule, RecurrenceFrequency


class CSVReportExporter:
    """
    Generates structured, downloadable CSV reports of household expenses.
    """
    @classmethod
    def export_bills_csv(
        cls,
        household: Household,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        category: Optional[str] = None,
        status: Optional[str] = None,
    ) -> HttpResponse:
        queryset = (
            Bill.objects.filter(household=household)
            .select_related("assigned_to", "uploaded_by")
            .order_by("-due_date")
        )

        if date_from:
            queryset = queryset.filter(due_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(due_date__lte=date_to)
        if category:
            queryset = queryset.filter(category=category)
        if status:
            queryset = queryset.filter(status=status)

        timestamp = timezone.now().strftime("%Y%m%d_%H%M%S")
        safe_hh_name = "".join(c for c in household.name if c.isalnum() or c in ("-", "_")).rstrip()
        filename = f"homepilot_bills_{safe_hh_name}_{timestamp}.csv"

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)

        # Write CSV headers
        writer.writerow([
            "Bill ID",
            "Provider Name",
            "Category",
            "Amount",
            "Currency",
            "Total Paid",
            "Outstanding Balance",
            "Status",
            "Due Date",
            "Issue Date",
            "Billing Period",
            "Assigned To",
            "Uploaded By",
            "Reference Number",
            "Notes",
        ])

        total_amount = Decimal("0.00")
        total_paid = Decimal("0.00")
        total_outstanding = Decimal("0.00")

        cat_map = dict(BillCategory.choices)

        for bill in queryset:
            total_amount += bill.amount
            total_paid += bill.total_paid
            total_outstanding += bill.outstanding_balance

            writer.writerow([
                bill.id,
                bill.provider_name,
                cat_map.get(bill.category, bill.category),
                f"{bill.amount:.2f}",
                bill.currency,
                f"{bill.total_paid:.2f}",
                f"{bill.outstanding_balance:.2f}",
                bill.get_status_display(),
                bill.due_date.isoformat() if bill.due_date else "",
                bill.issue_date.isoformat() if bill.issue_date else "",
                bill.billing_period or "",
                getattr(bill.assigned_to, "display_name", "") or "",
                getattr(bill.uploaded_by, "display_name", "") or "",
                bill.reference_number or "",
                bill.notes or "",
            ])

        # Write summary row
        writer.writerow([])
        writer.writerow([
            "TOTALS",
            f"{queryset.count()} Bills",
            "",
            f"{total_amount:.2f}",
            "",
            f"{total_paid:.2f}",
            f"{total_outstanding:.2f}",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        ])

        return response

    @classmethod
    def export_payments_csv(
        cls,
        household: Household,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
    ) -> HttpResponse:
        """
        Exports payment history and transaction receipts in CSV format.
        """
        queryset = (
            Payment.objects.filter(household=household)
            .select_related("bill", "paid_by")
            .order_by("-payment_date", "-id")
        )

        if date_from:
            queryset = queryset.filter(payment_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(payment_date__lte=date_to)

        timestamp = timezone.now().strftime("%Y%m%d_%H%M%S")
        safe_hh_name = "".join(c for c in household.name if c.isalnum() or c in ("-", "_")).rstrip()
        filename = f"homepilot_payments_{safe_hh_name}_{timestamp}.csv"

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        writer.writerow([
            "Payment ID",
            "Bill ID",
            "Provider Name",
            "Bill Category",
            "Amount",
            "Currency",
            "Payment Date",
            "Payment Method",
            "Transaction Reference",
            "Paid By",
            "Receipt Attached",
            "Notes",
        ])

        total_amount = Decimal("0.00")
        for p in queryset:
            total_amount += p.amount
            has_receipt = hasattr(p, "receipt") and p.receipt is not None

            writer.writerow([
                p.id,
                p.bill.id if p.bill else "",
                p.bill.provider_name if p.bill else "Direct",
                p.bill.get_category_display() if p.bill else "",
                f"{p.amount:.2f}",
                p.currency,
                p.payment_date.isoformat(),
                p.get_payment_method_display(),
                p.transaction_reference or "",
                getattr(p.paid_by, "display_name", "") or "",
                "YES" if has_receipt else "NO",
                p.notes or "",
            ])

        writer.writerow([])
        writer.writerow([
            "TOTALS",
            f"{queryset.count()} Payments",
            "",
            "",
            f"{total_amount:.2f}",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        ])
        return response

    @classmethod
    def export_recurring_csv(cls, household: Household) -> HttpResponse:
        """
        Exports active recurring schedules and subscription commitments.
        """
        queryset = (
            RecurringSchedule.objects.filter(household=household)
            .select_related("assigned_to", "created_by")
            .order_by("next_due_date")
        )

        timestamp = timezone.now().strftime("%Y%m%d_%H%M%S")
        safe_hh_name = "".join(c for c in household.name if c.isalnum() or c in ("-", "_")).rstrip()
        filename = f"homepilot_recurring_{safe_hh_name}_{timestamp}.csv"

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        writer.writerow([
            "Schedule ID",
            "Provider Name",
            "Category",
            "Frequency",
            "Expected Amount",
            "Currency",
            "Next Due Date",
            "Active",
            "Assigned To",
            "Notes",
        ])

        for s in queryset:
            writer.writerow([
                s.id,
                s.provider_name,
                s.get_category_display(),
                s.get_frequency_display(),
                f"{s.expected_amount:.2f}",
                s.currency,
                s.next_due_date.isoformat() if s.next_due_date else "",
                "YES" if s.is_active else "NO",
                getattr(s.assigned_to, "display_name", "") or "",
                s.notes or "",
            ])

        return response

    @classmethod
    def export_overdue_bills_csv(cls, household: Household) -> HttpResponse:
        """
        Exports overdue bills report.
        """
        today = timezone.localdate()
        queryset = (
            Bill.objects.filter(
                household=household,
                due_date__lt=today,
                status__in=[BillStatus.PENDING, BillStatus.PARTIALLY_PAID, BillStatus.OVERDUE],
            )
            .select_related("assigned_to")
            .order_by("due_date")
        )

        timestamp = timezone.now().strftime("%Y%m%d_%H%M%S")
        safe_hh_name = "".join(c for c in household.name if c.isalnum() or c in ("-", "_")).rstrip()
        filename = f"homepilot_overdue_{safe_hh_name}_{timestamp}.csv"

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        writer.writerow([
            "Bill ID",
            "Provider Name",
            "Category",
            "Total Amount",
            "Outstanding Balance",
            "Currency",
            "Due Date",
            "Days Overdue",
            "Status",
            "Assigned To",
        ])

        total_due = Decimal("0.00")
        for b in queryset:
            total_due += b.outstanding_balance
            writer.writerow([
                b.id,
                b.provider_name,
                b.get_category_display(),
                f"{b.amount:.2f}",
                f"{b.outstanding_balance:.2f}",
                b.currency,
                b.due_date.isoformat(),
                (today - b.due_date).days,
                b.get_status_display(),
                getattr(b.assigned_to, "display_name", "") or "",
            ])

        writer.writerow([])
        writer.writerow([
            "TOTALS",
            f"{queryset.count()} Overdue Bills",
            "",
            "",
            f"{total_due:.2f}",
            "",
            "",
            "",
            "",
            "",
        ])
        return response

    @classmethod
    def export_upcoming_obligations_csv(cls, household: Household, days: int = 30) -> HttpResponse:
        """
        Exports upcoming bills due in next N days.
        """
        today = timezone.localdate()
        cutoff = today + timedelta(days=int(days))
        queryset = (
            Bill.objects.filter(
                household=household,
                due_date__gte=today,
                due_date__lte=cutoff,
            )
            .exclude(status=BillStatus.PAID)
            .select_related("assigned_to")
            .order_by("due_date")
        )

        timestamp = timezone.now().strftime("%Y%m%d_%H%M%S")
        safe_hh_name = "".join(c for c in household.name if c.isalnum() or c in ("-", "_")).rstrip()
        filename = f"homepilot_upcoming_{safe_hh_name}_{timestamp}.csv"

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        writer.writerow([
            "Bill ID",
            "Provider Name",
            "Category",
            "Total Amount",
            "Outstanding Balance",
            "Currency",
            "Due Date",
            "Days Until Due",
            "Status",
            "Assigned To",
        ])

        total_due = Decimal("0.00")
        for b in queryset:
            total_due += b.outstanding_balance
            writer.writerow([
                b.id,
                b.provider_name,
                b.get_category_display(),
                f"{b.amount:.2f}",
                f"{b.outstanding_balance:.2f}",
                b.currency,
                b.due_date.isoformat(),
                (b.due_date - today).days,
                b.get_status_display(),
                getattr(b.assigned_to, "display_name", "") or "",
            ])

        writer.writerow([])
        writer.writerow([
            "TOTALS",
            f"{queryset.count()} Upcoming Bills",
            "",
            "",
            f"{total_due:.2f}",
            "",
            "",
            "",
            "",
            "",
        ])
        return response
