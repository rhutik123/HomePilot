import csv
from datetime import date
from decimal import Decimal
from typing import Optional
from django.http import HttpResponse
from django.utils import timezone

from apps.bills.models import Bill, BillCategory
from apps.households.models import Household


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
