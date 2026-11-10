import io
from datetime import date
from decimal import Decimal
from typing import Optional
from xml.sax.saxutils import escape as xml_escape
from django.http import HttpResponse
from django.utils import timezone

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    HRFlowable,
)

from apps.bills.models import Bill, BillCategory
from apps.payments.models import Payment
from apps.households.models import Household
from apps.analytics.services.analytics_service import AnalyticsService


class PDFReportExporter:
    """
    Generates professional, publication-quality PDF financial statements
    for households using ReportLab.
    """
    @classmethod
    def export_financial_statement_pdf(
        cls,
        household: Household,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
    ) -> HttpResponse:
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            rightMargin=36,
            leftMargin=36,
            topMargin=36,
            bottomMargin=36,
        )

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "DocTitle",
            parent=styles["Heading1"],
            fontSize=22,
            leading=26,
            textColor=colors.HexColor("#0F172A"),
            spaceAfter=4,
        )
        subtitle_style = ParagraphStyle(
            "DocSubtitle",
            parent=styles["Normal"],
            fontSize=10,
            leading=14,
            textColor=colors.HexColor("#475569"),
            spaceAfter=15,
        )
        section_heading = ParagraphStyle(
            "SectionHeading",
            parent=styles["Heading2"],
            fontSize=13,
            leading=17,
            textColor=colors.HexColor("#1E293B"),
            spaceBefore=14,
            spaceAfter=8,
        )
        cell_bold = ParagraphStyle(
            "CellBold",
            parent=styles["Normal"],
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#0F172A"),
            fontName="Helvetica-Bold",
        )
        cell_text = ParagraphStyle(
            "CellText",
            parent=styles["Normal"],
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#1E293B"),
        )
        kpi_title = ParagraphStyle(
            "KPITitle",
            parent=styles["Normal"],
            fontSize=9,
            leading=11,
            textColor=colors.HexColor("#64748B"),
        )
        kpi_val = ParagraphStyle(
            "KPIVal",
            parent=styles["Heading2"],
            fontSize=15,
            leading=18,
            textColor=colors.HexColor("#0F172A"),
        )

        elements = []

        # 1. Header Banner
        elements.append(Paragraph("HomePilot — Household Financial Statement", title_style))
        date_range_str = "All Time"
        if date_from and date_to:
            date_range_str = f"{date_from.strftime('%b %d, %Y')} – {date_to.strftime('%b %d, %Y')}"
        elif date_from:
            date_range_str = f"From {date_from.strftime('%b %d, %Y')}"
        elif date_to:
            date_range_str = f"Until {date_to.strftime('%b %d, %Y')}"

        elements.append(
            Paragraph(
                f"Household: <b>{xml_escape(household.name)}</b> &nbsp;|&nbsp; Reporting Period: <b>{xml_escape(date_range_str)}</b> "
                f"&nbsp;|&nbsp; Generated: {timezone.now().strftime('%B %d, %Y %H:%M')}",
                subtitle_style,
            )
        )
        elements.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#E2E8F0"), spaceAfter=14))

        # 2. Executive KPI Cards
        overview = AnalyticsService.get_overview_metrics(household, date_from, date_to)

        kpi_data = [
            [
                Paragraph("TOTAL BILLED", kpi_title),
                Paragraph("TOTAL PAID", kpi_title),
                Paragraph("OUTSTANDING", kpi_title),
                Paragraph("SETTLEMENT RATE", kpi_title),
            ],
            [
                Paragraph(f"{overview['total_billed']} INR", kpi_val),
                Paragraph(f"{overview['total_paid']} INR", kpi_val),
                Paragraph(f"{overview['total_outstanding']} INR", kpi_val),
                Paragraph(f"{overview['settlement_rate_pct']}%", kpi_val),
            ],
        ]
        kpi_table = Table(kpi_data, colWidths=[135, 135, 135, 135])
        kpi_table.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
                ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#E2E8F0")),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ])
        )
        elements.append(kpi_table)
        elements.append(Spacer(1, 14))

        # 3. Category Breakdown Table
        elements.append(Paragraph("Category Spending Breakdown", section_heading))
        cat_metrics = AnalyticsService.get_category_metrics(household, date_from, date_to)

        cat_table_data = [
            [
                Paragraph("Category", cell_bold),
                Paragraph("Bills", cell_bold),
                Paragraph("Total Billed", cell_bold),
                Paragraph("Total Paid", cell_bold),
                Paragraph("Outstanding", cell_bold),
                Paragraph("Share (%)", cell_bold),
            ]
        ]

        for c in cat_metrics:
            cat_table_data.append([
                Paragraph(c["category_display"], cell_text),
                Paragraph(str(c["bill_count"]), cell_text),
                Paragraph(f"{c['total_amount']} INR", cell_text),
                Paragraph(f"{c['total_paid']} INR", cell_text),
                Paragraph(f"{c['outstanding_balance']} INR", cell_text),
                Paragraph(f"{c['percentage_of_total']}%", cell_text),
            ])

        if len(cat_metrics) == 0:
            cat_table_data.append([Paragraph("No bills found for the selected period.", cell_text), "", "", "", "", ""])

        cat_table = Table(cat_table_data, colWidths=[130, 50, 95, 95, 95, 75])
        cat_table.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F172A")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                ("TOPPADDING", (0, 0), (-1, 0), 6),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 1), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
            ])
        )
        elements.append(cat_table)
        elements.append(Spacer(1, 14))

        # 4. Detailed Line Items Table
        elements.append(Paragraph("Individual Bill Line Items", section_heading))
        bills_qs = (
            Bill.objects.filter(household=household)
            .select_related("assigned_to")
        )
        if date_from:
            bills_qs = bills_qs.filter(due_date__gte=date_from)
        if date_to:
            bills_qs = bills_qs.filter(due_date__lte=date_to)

        bills_list = list(bills_qs.order_by("-due_date")[:40])

        bill_table_data = [
            [
                Paragraph("Provider", cell_bold),
                Paragraph("Category", cell_bold),
                Paragraph("Amount", cell_bold),
                Paragraph("Outstanding", cell_bold),
                Paragraph("Status", cell_bold),
                Paragraph("Due Date", cell_bold),
            ]
        ]

        cat_map = dict(BillCategory.choices)
        for b in bills_list:
            bill_table_data.append([
                Paragraph(xml_escape(b.provider_name or "Unknown"), cell_text),
                Paragraph(xml_escape(str(cat_map.get(b.category, b.category))), cell_text),
                Paragraph(f"{b.amount:.2f} {xml_escape(b.currency or 'INR')}", cell_text),
                Paragraph(f"{b.outstanding_balance:.2f} {xml_escape(b.currency or 'INR')}", cell_text),
                Paragraph(xml_escape(b.get_status_display()), cell_text),
                Paragraph(b.due_date.isoformat() if b.due_date else "N/A", cell_text),
            ])

        if not bills_list:
            bill_table_data.append([Paragraph("No bills recorded.", cell_text), "", "", "", "", ""])

        bill_table = Table(bill_table_data, colWidths=[130, 95, 85, 85, 75, 70])
        bill_table.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E293B")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                ("TOPPADDING", (0, 0), (-1, 0), 6),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 1), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 4),
            ])
        )
        elements.append(bill_table)

        # Build PDF
        doc.build(elements)

        pdf_bytes = buffer.getvalue()
        buffer.close()

        timestamp = timezone.now().strftime("%Y%m%d_%H%M%S")
        safe_hh_name = "".join(c for c in household.name if c.isalnum() or c in ("-", "_")).rstrip()
        filename = f"homepilot_statement_{safe_hh_name}_{timestamp}.pdf"

        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    @classmethod
    def export_payments_statement_pdf(
        cls,
        household: Household,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
    ) -> HttpResponse:
        """
        Generates a formal payment receipts statement in PDF format.
        """
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            rightMargin=36,
            leftMargin=36,
            topMargin=36,
            bottomMargin=36,
        )

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "DocTitle",
            parent=styles["Heading1"],
            fontSize=22,
            leading=26,
            textColor=colors.HexColor("#0F172A"),
            spaceAfter=4,
        )
        subtitle_style = ParagraphStyle(
            "DocSubtitle",
            parent=styles["Normal"],
            fontSize=10,
            leading=14,
            textColor=colors.HexColor("#475569"),
            spaceAfter=15,
        )
        cell_bold = ParagraphStyle(
            "CellBold",
            parent=styles["Normal"],
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#0F172A"),
            fontName="Helvetica-Bold",
        )
        cell_text = ParagraphStyle(
            "CellText",
            parent=styles["Normal"],
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#1E293B"),
        )

        elements = []
        elements.append(Paragraph("HomePilot — Household Payment Receipts Statement", title_style))
        date_range_str = "All Time"
        if date_from and date_to:
            date_range_str = f"{date_from.strftime('%b %d, %Y')} – {date_to.strftime('%b %d, %Y')}"
        elif date_from:
            date_range_str = f"From {date_from.strftime('%b %d, %Y')}"
        elif date_to:
            date_range_str = f"Until {date_to.strftime('%b %d, %Y')}"

        elements.append(
            Paragraph(
                f"Household: <b>{xml_escape(household.name)}</b> &nbsp;|&nbsp; Reporting Period: <b>{xml_escape(date_range_str)}</b> "
                f"&nbsp;|&nbsp; Generated: {timezone.now().strftime('%B %d, %Y %H:%M')}",
                subtitle_style,
            )
        )
        elements.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#E2E8F0"), spaceAfter=14))

        payments_qs = (
            Payment.objects.filter(household=household)
            .select_related("bill", "paid_by")
            .order_by("-payment_date", "-id")
        )
        if date_from:
            payments_qs = payments_qs.filter(payment_date__gte=date_from)
        if date_to:
            payments_qs = payments_qs.filter(payment_date__lte=date_to)

        table_data = [
            [
                Paragraph("Payment Date", cell_bold),
                Paragraph("Provider / Bill", cell_bold),
                Paragraph("Amount", cell_bold),
                Paragraph("Method", cell_bold),
                Paragraph("Reference", cell_bold),
                Paragraph("Paid By", cell_bold),
            ]
        ]

        total_settled = Decimal("0.00")
        for p in payments_qs:
            total_settled += p.amount
            provider_title = p.bill.provider_name if p.bill else "Direct"
            table_data.append([
                Paragraph(p.payment_date.isoformat(), cell_text),
                Paragraph(xml_escape(provider_title), cell_text),
                Paragraph(f"{p.amount:.2f} {xml_escape(p.currency or 'INR')}", cell_text),
                Paragraph(xml_escape(p.get_payment_method_display()), cell_text),
                Paragraph(xml_escape(p.transaction_reference or "N/A"), cell_text),
                Paragraph(xml_escape(getattr(p.paid_by, "display_name", "Unknown")), cell_text),
            ])

        if not payments_qs.exists():
            table_data.append([Paragraph("No payments recorded for this period.", cell_text), "", "", "", "", ""])

        pay_table = Table(table_data, colWidths=[80, 130, 85, 75, 100, 90])
        pay_table.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F172A")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                ("TOPPADDING", (0, 0), (-1, 0), 6),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 1), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 4),
            ])
        )
        elements.append(pay_table)

        doc.build(elements)
        pdf_bytes = buffer.getvalue()
        buffer.close()

        timestamp = timezone.now().strftime("%Y%m%d_%H%M%S")
        safe_hh_name = "".join(c for c in household.name if c.isalnum() or c in ("-", "_")).rstrip()
        filename = f"homepilot_payments_{safe_hh_name}_{timestamp}.pdf"

        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response
