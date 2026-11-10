import hashlib
import logging
from decimal import Decimal
from datetime import datetime, timedelta
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.http import FileResponse, Http404

logger = logging.getLogger(__name__)
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.cache import cache
from django.contrib import messages
from django.db.models import Sum, Q
from django.utils import timezone

from apps.bills.models import Bill, BillDocument, BillStatus, BillCategory
from apps.bills.validators import validate_bill_document
from apps.bills.services.document_parser import DocumentParserService
from apps.bills.services.extraction import get_extraction_provider
from apps.bills.services.bill_service import BillService
from apps.payments.services.payment_service import PaymentService
from apps.payments.models import Payment, PaymentMethod, PaymentStatus
from apps.reminders.services.reminder_service import ReminderService
from apps.reminders.models import (
    InAppNotification,
    NotificationPreference,
    NotificationDelivery,
    BillReminder,
)
from apps.reminders.services.channels.whatsapp import WhatsAppChannel
from apps.households.models import Household, HouseholdMembership
from apps.recurring.models import RecurringSchedule, RecurringOccurrence, RecurrenceFrequency
from apps.recurring.services.schedule_service import RecurringScheduleService, calculate_next_due_date
from apps.accounts.models import User
from apps.ai.models import SpendingInsight, AIConversation, AIMessage, AIToolExecution
from apps.ai.services.spending_insights_service import SpendingInsightsService
from apps.ai.services.assistant import AIAssistantService
from apps.analytics.services.analytics_service import AnalyticsService
from apps.analytics.services.export_csv import CSVReportExporter
from apps.analytics.services.export_pdf import PDFReportExporter
from apps.audit.services import AuditService
from apps.audit.models import AuditEvent


def index_redirect_view(request):
    """Root URL redirects to dashboard if logged in, otherwise to login."""
    if request.user.is_authenticated:
        return redirect("dashboard_web:dashboard")
    return redirect("dashboard_web:login")


@login_required(login_url="dashboard_web:login")
def dashboard_view(request):
    """Main responsive household bills dashboard."""
    user = request.user
    today = timezone.localdate()

    user_bills = Bill.objects.filter(
        household__memberships__user=user
    ).select_related("household", "assigned_to").distinct()

    # Metrics
    pending_qs = user_bills.filter(status=BillStatus.PENDING, due_date__gte=today)
    overdue_qs = user_bills.filter(
        Q(status=BillStatus.OVERDUE) | (Q(status=BillStatus.PENDING) & Q(due_date__lt=today))
    )
    paid_qs = user_bills.filter(status=BillStatus.PAID)
    assigned_qs = user_bills.filter(assigned_to=user).exclude(status=BillStatus.PAID)

    upcoming_bills = pending_qs.order_by("due_date")[:5]
    overdue_bills = overdue_qs.order_by("due_date")[:5]
    recently_paid = paid_qs.order_by("-paid_at", "-updated_at")[:5]
    assigned_to_me = assigned_qs.order_by("due_date")[:5]

    household = Household.objects.filter(memberships__user=user).first()
    active_insights = (
        SpendingInsight.objects.filter(household__memberships__user=user, is_dismissed=False)
        .select_related("household")
        .order_by("-created_at")[:3]
    )

    context = {
        "today": today,
        "metrics": {
            "pending_count": pending_qs.count(),
            "pending_total": f"{(pending_qs.aggregate(total=Sum('amount'))['total'] or 0):.2f}",
            "overdue_count": overdue_qs.count(),
            "overdue_total": f"{(overdue_qs.aggregate(total=Sum('amount'))['total'] or 0):.2f}",
            "paid_count": paid_qs.count(),
            "paid_total": f"{(paid_qs.aggregate(total=Sum('amount'))['total'] or 0):.2f}",
            "assigned_to_me_count": assigned_qs.count(),
        },
        "upcoming_bills": upcoming_bills,
        "overdue_bills": overdue_bills,
        "recently_paid": recently_paid,
        "assigned_to_me": assigned_to_me,
        "active_insights": active_insights,
        "household": household,
    }
    return render(request, "dashboard/index.html", context)


@login_required(login_url="dashboard_web:login")
def bills_list_view(request):
    """View all bills with quick filtering."""
    user = request.user
    today = timezone.localdate()
    status_filter = request.GET.get("status", "ALL").upper()

    bills = Bill.objects.filter(
        household__memberships__user=user
    ).select_related("household", "assigned_to").distinct()

    if status_filter == "PENDING":
        bills = bills.filter(status=BillStatus.PENDING, due_date__gte=today)
    elif status_filter == "PARTIALLY_PAID":
        bills = bills.filter(status=BillStatus.PARTIALLY_PAID)
    elif status_filter == "OVERDUE":
        bills = bills.filter(
            Q(status=BillStatus.OVERDUE) | (Q(status__in=[BillStatus.PENDING, BillStatus.PARTIALLY_PAID]) & Q(due_date__lt=today))
        )
    elif status_filter == "PAID":
        bills = bills.filter(status=BillStatus.PAID)

    bills = bills.order_by("due_date")

    context = {
        "bills": bills,
        "current_status": status_filter,
    }
    return render(request, "bills/list.html", context)


@require_POST
@login_required(login_url="dashboard_web:login")
def mark_bill_paid_web(request, bill_id):
    """
    State-changing web action to record payment and mark a bill as paid.
    Enforces POST method with CSRF protection and synchronizes the payments ledger.
    """
    bill = get_object_or_404(
        Bill,
        id=bill_id,
        household__memberships__user=request.user,
    )

    if bill.outstanding_balance > Decimal("0.00"):
        PaymentService.record_payment(
            bill=bill,
            user=request.user,
            amount=bill.outstanding_balance,
            payment_date=timezone.localdate(),
            payment_method=PaymentMethod.OTHER,
            notes="Marked paid via web dashboard quick action.",
        )
    else:
        bill.mark_as_paid()

    AuditService.log(
        action=AuditEvent.Action.BILL_PAID,
        actor=request.user,
        household=bill.household,
        entity=bill,
        description=f"Bill '{bill.provider_name}' marked as paid via web by {request.user.display_name}.",
    )

    messages.success(request, f"Bill '{bill.provider_name}' has been marked as paid!")
    return redirect(request.META.get("HTTP_REFERER", "dashboard_web:dashboard"))


@login_required(login_url="dashboard_web:login")
def bill_upload_view(request):
    """
    Two-step web flow for adding a household bill.
    Supports Option A (Upload Bill document with AI extraction and review)
    and Option B (Direct manual bill entry without document upload).
    """
    user = request.user
    households = Household.objects.filter(memberships__user=user)

    if not households.exists():
        messages.error(request, "Please create or join a household first.")
        return redirect("dashboard_web:household")

    active_household = households.first()
    members = User.objects.filter(household_memberships__household=active_household)

    mode = request.GET.get("mode", "upload")  # "upload" or "manual"
    step = "upload"  # "upload" or "review"
    extracted_data = {}
    manual_data = {}
    doc_id = None

    # Handle direct review link via GET query param (e.g. from pending queue)
    requested_doc_id = request.GET.get("doc_id")
    if requested_doc_id and request.method == "GET":
        target_doc = BillDocument.objects.filter(
            id=requested_doc_id,
            household=active_household,
            bill__isnull=True,
        ).first()
        if target_doc:
            step = "review"
            doc_id = target_doc.id
            extracted_data = target_doc.extracted_data or {}

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "upload_file":
            uploaded_files = request.FILES.getlist("bill_files") or (
                [request.FILES["bill_file"]] if "bill_file" in request.FILES else []
            )
            if not uploaded_files:
                messages.error(request, "Please select at least one bill file to upload.")
            else:
                processed_docs = []
                for uploaded_file in uploaded_files:
                    try:
                        validate_bill_document(uploaded_file)

                        # Compute SHA-256 hash for duplicate detection
                        content = uploaded_file.read()
                        uploaded_file.seek(0)
                        file_hash = hashlib.sha256(content).hexdigest()

                        # Check for existing document with same checksum in household
                        existing_dup = BillDocument.objects.filter(
                            household=active_household,
                            file_hash=file_hash,
                        ).first()
                        is_duplicate = existing_dup is not None
                        duplicate_warning = (
                            f"Identical file content previously uploaded on "
                            f"{existing_dup.created_at.strftime('%Y-%m-%d')} ({existing_dup.file_name})."
                            if is_duplicate else ""
                        )

                        # Create draft document record
                        doc = BillDocument.objects.create(
                            household=active_household,
                            uploaded_by=user,
                            file=uploaded_file,
                            file_name=uploaded_file.name,
                            file_size=uploaded_file.size,
                            file_hash=file_hash,
                            is_possible_duplicate=is_duplicate,
                            duplicate_warning=duplicate_warning,
                            mime_type=getattr(uploaded_file, "content_type", "application/octet-stream"),
                        )

                        AuditService.log(
                            action=AuditEvent.Action.DOCUMENT_UPLOADED,
                            actor=user,
                            household=active_household,
                            entity=doc,
                            description=f"Document '{uploaded_file.name}' uploaded for analysis.",
                            metadata={
                                "file_name": uploaded_file.name,
                                "file_size": uploaded_file.size,
                                "mime_type": doc.mime_type,
                                "is_duplicate": is_duplicate,
                            },
                        )

                        try:
                            raw_text = DocumentParserService.extract_text(doc.file, doc.mime_type)
                            doc.raw_text = raw_text
                            provider = get_extraction_provider()
                            result = provider.extract(raw_text, doc.mime_type)
                            doc.extracted_data = result.model_dump(mode="json")
                            is_complete = bool(
                                result.amount is not None
                                and result.provider_name
                                and result.due_date is not None
                            )
                            doc.extraction_status = (
                                BillDocument.ExtractionStatus.SUCCESS
                                if is_complete
                                else BillDocument.ExtractionStatus.NEEDS_REVIEW
                            )
                            doc.save()

                            AuditService.log(
                                action=AuditEvent.Action.EXTRACTION_COMPLETED,
                                actor=user,
                                household=active_household,
                                entity=doc,
                                description=f"AI extraction completed for '{doc.file_name}'.",
                                metadata={"missing_fields": result.missing_fields, "provider": provider.__class__.__name__},
                            )

                        except Exception as extraction_err:
                            logger.warning("Extraction failed for doc #%s: %s", doc.id, extraction_err)
                            doc.extraction_status = BillDocument.ExtractionStatus.FAILED
                            doc.extracted_data = {
                                "error": str(extraction_err),
                                "provider_used": getattr(provider, "provider_name", "unknown") if 'provider' in locals() else "unknown",
                                "model_used": "none",
                                "confidence_notes": f"Extraction could not be completed automatically: {str(extraction_err)}",
                            }
                            doc.save(update_fields=["extraction_status", "extracted_data"])

                            AuditService.log(
                                action=AuditEvent.Action.EXTRACTION_FAILED,
                                actor=user,
                                household=active_household,
                                entity=doc,
                                description=f"AI extraction failed for '{doc.file_name}': {str(extraction_err)}",
                                metadata={"error": str(extraction_err)},
                            )

                        processed_docs.append(doc)

                    except Exception as e:
                        messages.error(request, f"Upload failed for '{uploaded_file.name}': {str(e)}")

                if processed_docs:
                    if len(processed_docs) == 1:
                        # Single upload: take user directly to review
                        doc = processed_docs[0]
                        step = "review"
                        doc_id = doc.id
                        extracted_data = doc.extracted_data or {}
                        if doc.extraction_status == BillDocument.ExtractionStatus.FAILED:
                            messages.warning(
                                request,
                                "Could not automatically extract all bill details. You can enter or correct the information manually.",
                            )
                        elif not extracted_data.get("amount") or not extracted_data.get("due_date"):
                            messages.warning(
                                request,
                                "Could not automatically extract all bill details. You can enter or correct the information manually.",
                            )
                        else:
                            messages.success(request, f"Document '{doc.file_name}' parsed! Please review details below.")
                    else:
                        # Multiple uploads: notify user and display review queue
                        messages.success(
                            request,
                            f"Successfully uploaded and analyzed {len(processed_docs)} documents! "
                            "Please review each document below to save your bills.",
                        )
                        step = "upload"

        elif action == "confirm_bill":
            doc_id = request.POST.get("doc_id")
            doc = get_object_or_404(BillDocument, id=doc_id, household__memberships__user=user)

            try:
                amt = request.POST.get("amount")
                due_date_str = request.POST.get("due_date")
                issue_date_str = request.POST.get("issue_date")
                provider_name = request.POST.get("provider_name", "").strip()
                category = request.POST.get("category", BillCategory.OTHER)
                currency = request.POST.get("currency", "INR")
                billing_period = request.POST.get("billing_period", "").strip()
                reference_number = request.POST.get("reference_number", "").strip()
                notes = request.POST.get("notes", "").strip()

                assigned_to_id = request.POST.get("assigned_to")
                assigned_to = None
                if assigned_to_id:
                    assigned_to = User.objects.filter(
                        id=assigned_to_id,
                        household_memberships__household=doc.household,
                    ).first()
                    if not assigned_to:
                        raise ValueError(f"Selected member is not an active member of '{doc.household.name}'.")

                bill = BillService.create_bill(
                    household=doc.household,
                    uploaded_by=user,
                    provider_name=provider_name,
                    amount=amt,
                    due_date=due_date_str,
                    category=category,
                    currency=currency,
                    assigned_to=assigned_to,
                    issue_date=issue_date_str,
                    billing_period=billing_period,
                    reference_number=reference_number,
                    notes=notes,
                    document=doc,
                    source="DOCUMENT_UPLOAD",
                )

                messages.success(request, f"Bill '{bill.provider_name}' saved successfully!")
                return redirect("dashboard_web:dashboard")

            except Exception as e:
                messages.error(request, f"Could not save bill: {str(e)}")
                step = "review"
                extracted_data = request.POST.dict()

        elif action == "manual_bill":
            try:
                amt = request.POST.get("amount")
                due_date_str = request.POST.get("due_date")
                issue_date_str = request.POST.get("issue_date")
                provider_name = request.POST.get("provider_name", "").strip()
                category = request.POST.get("category", BillCategory.OTHER)
                currency = request.POST.get("currency", "INR")
                billing_period = request.POST.get("billing_period", "").strip()
                reference_number = request.POST.get("reference_number", "").strip()
                notes = request.POST.get("notes", "").strip()

                assigned_to_id = request.POST.get("assigned_to")
                assigned_to = None
                if assigned_to_id:
                    assigned_to = User.objects.filter(
                        id=assigned_to_id,
                        household_memberships__household=active_household,
                    ).first()
                    if not assigned_to:
                        raise ValueError(f"Selected member is not an active member of '{active_household.name}'.")

                bill = BillService.create_bill(
                    household=active_household,
                    uploaded_by=user,
                    provider_name=provider_name,
                    amount=amt,
                    due_date=due_date_str,
                    category=category,
                    currency=currency,
                    assigned_to=assigned_to,
                    issue_date=issue_date_str,
                    billing_period=billing_period,
                    reference_number=reference_number,
                    notes=notes,
                    document=None,
                    source="MANUAL",
                )

                messages.success(request, f"Bill '{bill.provider_name}' created successfully!")
                return redirect("dashboard_web:dashboard")

            except Exception as e:
                messages.error(request, f"Could not create bill: {str(e)}")
                mode = "manual"
                manual_data = request.POST.dict()

    unreviewed_docs = BillDocument.objects.filter(
        household=active_household,
        bill__isnull=True,
    ).order_by("-created_at")

    current_doc = None
    if doc_id:
        current_doc = BillDocument.objects.filter(id=doc_id).first()

    context = {
        "step": step,
        "mode": mode,
        "doc_id": doc_id,
        "current_doc": current_doc,
        "unreviewed_docs": unreviewed_docs,
        "extracted_data": extracted_data,
        "manual_data": manual_data,
        "household": active_household,
        "members": members,
        "categories": BillCategory.choices,
    }
    return render(request, "bills/upload.html", context)


@login_required(login_url="dashboard_web:login")
@require_POST
def delete_bill_document_web(request, doc_id):
    """
    Deletes an unconfirmed/draft BillDocument.
    Only permitted if document is not linked to a finalized bill,
    and user is either original uploader or household owner.
    """
    user = request.user
    doc = get_object_or_404(
        BillDocument.objects.select_related("household", "uploaded_by"),
        id=doc_id,
        household__memberships__user=user,
    )

    if doc.bill is not None:
        messages.error(request, "Cannot delete document: it is already linked to a confirmed bill.")
        return redirect("dashboard_web:bill-upload")

    is_uploader = doc.uploaded_by == user
    is_owner = doc.household.is_owner(user)
    if not (is_uploader or is_owner):
        messages.error(request, "You do not have permission to delete this draft document.")
        return redirect("dashboard_web:bill-upload")

    file_name = doc.file_name
    household = doc.household

    if doc.file:
        try:
            doc.file.delete(save=False)
        except Exception as e:
            logger.warning("Failed to delete physical file for doc #%s: %s", doc.id, e)

    doc.delete()

    AuditService.log(
        action=AuditEvent.Action.DOCUMENT_DELETED,
        actor=user,
        household=household,
        entity=None,
        description=f"Draft document '{file_name}' deleted by {user.display_name}.",
        metadata={"file_name": file_name, "document_id": doc_id},
    )

    messages.success(request, f"Draft document '{file_name}' was deleted.")
    return redirect("dashboard_web:bill-upload")


@login_required(login_url="dashboard_web:login")
@require_POST
def retry_document_extraction_web(request, doc_id):
    """
    Retries AI extraction on an unconfirmed draft BillDocument.
    Re-runs OCR and structured extraction service, updating status and extracted data.
    """
    user = request.user
    doc = get_object_or_404(
        BillDocument.objects.select_related("household"),
        id=doc_id,
        household__memberships__user=user,
    )

    if doc.bill is not None:
        messages.error(request, "Document is already linked to a confirmed bill.")
        return redirect("dashboard_web:bill-upload")

    try:
        raw_text = DocumentParserService.extract_text(doc.file, doc.mime_type)
        doc.raw_text = raw_text
        provider = get_extraction_provider()
        result = provider.extract(raw_text, doc.mime_type)
        doc.extracted_data = result.model_dump(mode="json")
        is_complete = bool(
            result.amount is not None
            and result.provider_name
            and result.due_date is not None
        )
        doc.extraction_status = (
            BillDocument.ExtractionStatus.SUCCESS
            if is_complete
            else BillDocument.ExtractionStatus.NEEDS_REVIEW
        )
        doc.save(update_fields=["raw_text", "extracted_data", "extraction_status"])

        AuditService.log(
            action=AuditEvent.Action.EXTRACTION_COMPLETED,
            actor=user,
            household=doc.household,
            entity=doc,
            description=f"AI extraction re-run for '{doc.file_name}'.",
            metadata={"missing_fields": result.missing_fields, "provider": provider.__class__.__name__},
        )
        messages.success(request, f"Extraction re-run successfully for '{doc.file_name}'.")

    except Exception as exc:
        logger.warning("Retry extraction failed for doc #%s: %s", doc.id, exc)
        doc.extraction_status = BillDocument.ExtractionStatus.FAILED
        doc.extracted_data = {"error": str(exc)}
        doc.save(update_fields=["extraction_status", "extracted_data"])

        AuditService.log(
            action=AuditEvent.Action.EXTRACTION_FAILED,
            actor=user,
            household=doc.household,
            entity=doc,
            description=f"AI extraction retry failed for '{doc.file_name}': {str(exc)}",
            metadata={"error": str(exc)},
        )
        messages.error(request, f"Extraction re-run failed: {str(exc)}")

    return redirect(f"{reverse('dashboard_web:bill-upload')}?doc_id={doc.id}")


@login_required(login_url="dashboard_web:login")
def bill_edit_view(request, bill_id):
    """
    Web view for reviewing and modifying an existing bill.
    Preserves audit trail, updates reminder schedules, and maintains multi-tenant isolation.
    """
    user = request.user
    bill = get_object_or_404(
        Bill.objects.select_related("household", "assigned_to", "uploaded_by").prefetch_related("documents"),
        id=bill_id,
        household__memberships__user=user,
    )
    members = User.objects.filter(household_memberships__household=bill.household)

    if request.method == "POST":
        try:
            assigned_to_id = request.POST.get("assigned_to")
            assigned_to = None
            if assigned_to_id:
                assigned_to = User.objects.filter(id=assigned_to_id, household_memberships__household=bill.household).first()
                if not assigned_to:
                    raise ValueError(f"Selected member is not an active member of '{bill.household.name}'.")

            amt = request.POST.get("amount")
            due_date = request.POST.get("due_date")
            issue_date = request.POST.get("issue_date")
            provider_name = request.POST.get("provider_name", "").strip()
            category = request.POST.get("category", BillCategory.OTHER)
            currency = request.POST.get("currency", "INR")
            billing_period = request.POST.get("billing_period", "").strip()
            reference_number = request.POST.get("reference_number", "").strip()
            notes = request.POST.get("notes", "").strip()

            BillService.update_bill(
                bill=bill,
                user=user,
                provider_name=provider_name,
                category=category,
                amount=amt,
                currency=currency,
                due_date=due_date,
                issue_date=issue_date,
                billing_period=billing_period,
                reference_number=reference_number,
                assigned_to=assigned_to,
                notes=notes,
            )
            messages.success(request, f"Bill '{bill.provider_name}' updated successfully.")
            return redirect("dashboard_web:bills-list")
        except Exception as e:
            messages.error(request, f"Could not update bill: {str(e)}")

    context = {
        "bill": bill,
        "members": members,
        "categories": BillCategory.choices,
    }
    return render(request, "bills/edit.html", context)


@login_required(login_url="dashboard_web:login")
def bill_detail_view(request, bill_id):
    """
    Detailed bill ledger view displaying:
    - Bill metadata and outstanding balance progress.
    - Attached bill document preview/link.
    - Full payment history with receipts and adjustment ledger.
    - Payment recording form (full or partial).
    - Owner adjustment/reversal action.
    """
    user = request.user
    bill = get_object_or_404(
        Bill.objects.select_related("household", "assigned_to", "uploaded_by").prefetch_related(
            "payments__paid_by",
            "payments__receipt",
            "payments__adjustments__adjusted_by",
            "documents",
        ),
        id=bill_id,
        household__memberships__user=user,
    )

    is_owner = bill.household.is_owner(user)

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "record_payment":
            try:
                amt_str = request.POST.get("amount")
                pay_method = request.POST.get("payment_method", PaymentMethod.UPI)
                tx_ref = request.POST.get("transaction_reference", "").strip()
                notes = request.POST.get("notes", "").strip()
                payment_date_str = request.POST.get("payment_date")
                receipt_file = request.FILES.get("receipt_file")

                payment_date = None
                if payment_date_str:
                    payment_date = datetime.strptime(payment_date_str, "%Y-%m-%d").date()

                payment = PaymentService.record_payment(
                    bill=bill,
                    user=user,
                    amount=Decimal(amt_str),
                    payment_date=payment_date,
                    payment_method=pay_method,
                    transaction_reference=tx_ref,
                    notes=notes,
                )

                if receipt_file:
                    PaymentService.attach_receipt(payment, user, receipt_file)

                bill.refresh_from_db()
                messages.success(
                    request,
                    f"Payment of ₹{payment.amount} recorded successfully! Remaining balance: ₹{bill.outstanding_balance}.",
                )
                return redirect("dashboard_web:bill-detail", bill_id=bill.id)

            except Exception as e:
                messages.error(request, f"Could not record payment: {str(e)}")

        elif action == "adjust_payment":
            if not is_owner:
                messages.error(request, "Only household owners are authorized to adjust payments.")
            else:
                try:
                    payment_id = request.POST.get("payment_id")
                    payment = get_object_or_404(Payment, id=payment_id, bill=bill)
                    adj_amount = Decimal(request.POST.get("amount", "0"))
                    reason = request.POST.get("reason", "").strip()

                    PaymentService.record_adjustment(
                        payment=payment,
                        adjusted_by=user,
                        amount=adj_amount,
                        reason=reason,
                    )
                    bill.refresh_from_db()
                    messages.success(
                        request,
                        f"Adjustment of ₹{adj_amount} recorded for Payment #{payment.id}. Bill balance updated to ₹{bill.outstanding_balance}.",
                    )
                    return redirect("dashboard_web:bill-detail", bill_id=bill.id)

                except Exception as e:
                    messages.error(request, f"Could not adjust payment: {str(e)}")

        elif action == "void_payment":
            if not is_owner:
                messages.error(request, "Only household owners are authorized to void payments.")
            else:
                try:
                    payment_id = request.POST.get("payment_id")
                    payment = get_object_or_404(Payment, id=payment_id, bill=bill)
                    reason = request.POST.get("reason", "Voided via dashboard").strip()

                    PaymentService.void_payment(payment, user, reason=reason)
                    bill.refresh_from_db()
                    messages.success(
                        request,
                        f"Payment #{payment.id} voided successfully. Bill balance restored to ₹{bill.outstanding_balance}.",
                    )
                    return redirect("dashboard_web:bill-detail", bill_id=bill.id)

                except Exception as e:
                    messages.error(request, f"Could not void payment: {str(e)}")

    pct_paid = 0
    if bill.amount > Decimal("0.00"):
        pct_paid = min(100, int((bill.total_paid / bill.amount) * 100))

    payments_list = bill.payments.all().order_by("-payment_date", "-created_at")

    context = {
        "bill": bill,
        "is_owner": is_owner,
        "pct_paid": pct_paid,
        "payments": payments_list,
        "payment_methods": PaymentMethod.choices,
        "today": timezone.localdate(),
    }
    return render(request, "bills/detail.html", context)


@login_required(login_url="dashboard_web:login")
def download_payment_receipt_view(request, payment_id):
    """Securely downloads an uploaded payment receipt after verifying household isolation."""
    payment = get_object_or_404(
        Payment.objects.select_related("household"),
        id=payment_id,
        household__memberships__user=request.user,
    )
    if not hasattr(payment, "receipt") or not payment.receipt.file:
        raise Http404("Receipt file does not exist for this payment.")

    return FileResponse(
        payment.receipt.file.open("rb"),
        content_type=payment.receipt.mime_type or "application/octet-stream",
        as_attachment=True,
        filename=payment.receipt.file_name,
    )


@login_required(login_url="dashboard_web:login")
def household_view(request):
    """View and manage household members."""
    user = request.user
    memberships = HouseholdMembership.objects.filter(
        user=user
    ).select_related("household").order_by("-joined_at")

    if not memberships.exists():
        # Auto-create initial household if user has none
        household = Household.objects.create(name=f"{user.display_name}'s Household", created_by=user)
        membership = HouseholdMembership.objects.create(
            household=household,
            user=user,
            role=HouseholdMembership.Role.OWNER,
        )
        current_membership = membership
    else:
        household_param = request.GET.get("household") or request.session.get("active_household_id")
        current_membership = None
        if household_param:
            current_membership = memberships.filter(household_id=household_param).first()
        if not current_membership:
            current_membership = memberships.first()

    request.session["active_household_id"] = current_membership.household.id
    household = current_membership.household
    is_owner = current_membership.role == HouseholdMembership.Role.OWNER
    members = household.memberships.select_related("user").order_by("-role", "joined_at")

    if request.method == "POST" and is_owner:
        target_ident = request.POST.get("username_or_email", "").strip()
        role = request.POST.get("role", HouseholdMembership.Role.MEMBER)

        target_user = None
        if "@" in target_ident:
            target_user = User.objects.filter(email__iexact=target_ident).first()
        else:
            target_user = User.objects.filter(username__iexact=target_ident).first()

        if not target_user:
            messages.error(request, f"No registered user found with username or email '{target_ident}'.")
        elif household.memberships.filter(user=target_user).exists():
            messages.error(request, f"{target_user.display_name} is already a member of this household.")
        else:
            m = HouseholdMembership.objects.create(
                household=household,
                user=target_user,
                role=role,
            )
            AuditService.log(
                action=AuditEvent.Action.MEMBER_ADDED,
                actor=user,
                household=household,
                entity=m,
                description=f"Added {target_user.display_name} as {role} to '{household.name}'.",
            )
            messages.success(request, f"Successfully added {target_user.display_name} as {role}!")
            return redirect(f"{reverse('dashboard_web:household')}?household={household.id}")

    context = {
        "household": household,
        "is_owner": is_owner,
        "members": members,
        "all_memberships": memberships,
        "current_membership": current_membership,
    }
    return render(request, "households/index.html", context)


@login_required(login_url="dashboard_web:login")
@require_POST
def household_change_role_web(request, membership_id):
    """
    Changes a member's role (OWNER <-> MEMBER).
    Enforces authorization: caller must be an OWNER in that household.
    Guards against removing the last owner.
    """
    user = request.user
    membership = get_object_or_404(
        HouseholdMembership.objects.select_related("household", "user"),
        id=membership_id,
    )
    household = membership.household

    if not household.is_owner(user):
        messages.error(request, "Only household owners can change member roles.")
        return redirect(f"{reverse('dashboard_web:household')}?household={household.id}")

    new_role = request.POST.get("role")
    if new_role not in (HouseholdMembership.Role.OWNER, HouseholdMembership.Role.MEMBER):
        messages.error(request, "Invalid role selected.")
        return redirect(f"{reverse('dashboard_web:household')}?household={household.id}")

    # Guard: If demoting an owner, ensure at least one other owner remains
    if membership.role == HouseholdMembership.Role.OWNER and new_role == HouseholdMembership.Role.MEMBER:
        owner_count = HouseholdMembership.objects.filter(
            household=household,
            role=HouseholdMembership.Role.OWNER,
        ).count()
        if owner_count <= 1:
            messages.error(request, "Cannot demote the only owner of this household. Promote another member first.")
            return redirect(f"{reverse('dashboard_web:household')}?household={household.id}")

    old_role = membership.role
    membership.role = new_role
    membership.save(update_fields=["role"])

    AuditService.log(
        action=AuditEvent.Action.ROLE_CHANGED,
        actor=user,
        household=household,
        entity=membership,
        description=f"Changed role of {membership.user.display_name} from {old_role} to {new_role}.",
        metadata={"old_role": old_role, "new_role": new_role, "member_id": membership.user.id},
    )

    messages.success(request, f"Updated role for {membership.user.display_name} to {new_role}.")
    return redirect(f"{reverse('dashboard_web:household')}?household={household.id}")


@login_required(login_url="dashboard_web:login")
@require_POST
def household_remove_member_web(request, membership_id):
    """
    Removes a member from a household.
    Enforces authorization: caller must be an OWNER in that household.
    Guards against removing the last owner.
    Never deletes the user account, only the membership link.
    """
    user = request.user
    membership = get_object_or_404(
        HouseholdMembership.objects.select_related("household", "user"),
        id=membership_id,
    )
    household = membership.household

    if not household.is_owner(user):
        messages.error(request, "Only household owners can remove members.")
        return redirect(f"{reverse('dashboard_web:household')}?household={household.id}")

    # Guard: Cannot remove the last owner
    if membership.role == HouseholdMembership.Role.OWNER:
        owner_count = HouseholdMembership.objects.filter(
            household=household,
            role=HouseholdMembership.Role.OWNER,
        ).count()
        if owner_count <= 1:
            messages.error(request, "Cannot remove the only owner of this household. Transfer ownership first.")
            return redirect(f"{reverse('dashboard_web:household')}?household={household.id}")

    target_user_name = membership.user.display_name
    target_user_id = membership.user.id

    membership.delete()

    AuditService.log(
        action=AuditEvent.Action.MEMBER_REMOVED,
        actor=user,
        household=household,
        entity=None,
        description=f"Removed {target_user_name} from household '{household.name}'.",
        metadata={"removed_user_id": target_user_id, "removed_user_name": target_user_name},
    )

    messages.success(request, f"Removed {target_user_name} from '{household.name}'.")
    return redirect(f"{reverse('dashboard_web:household')}?household={household.id}")


@login_required(login_url="dashboard_web:login")
def recurring_schedules_list_view(request):
    """
    Overview of household recurring bill schedules (subscriptions and periodic utilities).
    Supports pausing, resuming, triggering, and catch-up operations.
    """
    user = request.user
    active_household_id = request.session.get("active_household_id")
    memberships = HouseholdMembership.objects.filter(user=user).select_related("household")

    if not memberships.exists():
        messages.warning(request, "Please create or join a household first.")
        return redirect("dashboard_web:household")

    active_household = None
    if active_household_id:
        active_household = Household.objects.filter(id=active_household_id, memberships__user=user).first()
    if not active_household:
        active_household = memberships.first().household
        request.session["active_household_id"] = active_household.id

    if request.method == "POST":
        action = request.POST.get("action")
        schedule_id = request.POST.get("schedule_id")
        schedule = get_object_or_404(RecurringSchedule, id=schedule_id, household=active_household)

        try:
            if action == "pause":
                RecurringScheduleService.pause_schedule(schedule, user)
                messages.success(request, f"Schedule for '{schedule.provider_name}' has been paused.")
            elif action == "resume":
                RecurringScheduleService.resume_schedule(schedule, user)
                messages.success(request, f"Schedule for '{schedule.provider_name}' has been resumed.")
            elif action == "trigger":
                _, bill = RecurringScheduleService.trigger_occurrence(schedule, user)
                messages.success(request, f"Generated bill '{bill.provider_name}' (₹{bill.amount}) for period {bill.billing_period}!")
            elif action == "catch_up":
                bills = RecurringScheduleService.catch_up_missed_occurrences(schedule, user)
                messages.success(request, f"Generated {len(bills)} catch-up bills for '{schedule.provider_name}'.")
        except Exception as e:
            messages.error(request, f"Action failed: {str(e)}")

        return redirect("dashboard_web:recurring-list")

    filter_status = request.GET.get("status", "ALL").upper()
    schedules = RecurringSchedule.objects.filter(household=active_household).select_related(
        "assigned_to", "created_by"
    ).prefetch_related("occurrences__bill").order_by("next_due_date")

    if filter_status == "ACTIVE":
        schedules = schedules.filter(is_active=True)
    elif filter_status == "PAUSED":
        schedules = schedules.filter(is_active=False)

    active_count = RecurringSchedule.objects.filter(household=active_household, is_active=True).count()
    paused_count = RecurringSchedule.objects.filter(household=active_household, is_active=False).count()

    # Calculate estimated monthly commitment
    monthly_sum = Decimal("0.00")
    for s in RecurringSchedule.objects.filter(household=active_household, is_active=True):
        if s.frequency == RecurrenceFrequency.MONTHLY:
            monthly_sum += s.expected_amount / Decimal(str(s.interval_count))
        elif s.frequency == RecurrenceFrequency.QUARTERLY:
            monthly_sum += s.expected_amount / Decimal("3.0")
        elif s.frequency == RecurrenceFrequency.YEARLY:
            monthly_sum += s.expected_amount / Decimal("12.0")

    missed_list = RecurringScheduleService.detect_missed_schedules(active_household)

    context = {
        "schedules": schedules,
        "filter_status": filter_status,
        "active_count": active_count,
        "paused_count": paused_count,
        "monthly_estimate": round(monthly_sum, 2),
        "missed_schedules": missed_list,
        "household": active_household,
        "today": timezone.localdate(),
    }
    return render(request, "recurring/list.html", context)


@login_required(login_url="dashboard_web:login")
def recurring_schedule_form_view(request, schedule_id=None):
    """Create or edit a recurring bill schedule."""
    user = request.user
    active_household_id = request.session.get("active_household_id")
    household = Household.objects.filter(
        id=active_household_id, memberships__user=user
    ).first() or Household.objects.filter(memberships__user=user).first()

    if not household:
        messages.warning(request, "Please create or join a household first.")
        return redirect("dashboard_web:household")

    schedule = None
    if schedule_id:
        schedule = get_object_or_404(RecurringSchedule, id=schedule_id, household=household)

    members = User.objects.filter(household_memberships__household=household)

    if request.method == "POST":
        provider_name = request.POST.get("provider_name", "").strip()
        category = request.POST.get("category", BillCategory.OTHER)
        expected_amount_str = request.POST.get("expected_amount")
        frequency = request.POST.get("frequency", RecurrenceFrequency.MONTHLY)
        interval_count = request.POST.get("interval_count", "1")
        day_of_month = request.POST.get("day_of_month", "1")
        next_due_date_str = request.POST.get("next_due_date")
        assigned_to_id = request.POST.get("assigned_to")
        is_active = request.POST.get("is_active") == "on"
        notes = request.POST.get("notes", "").strip()

        assigned_to = None
        if assigned_to_id:
            assigned_to = members.filter(id=assigned_to_id).first()

        next_due_date = None
        if next_due_date_str:
            try:
                next_due_date = datetime.strptime(next_due_date_str, "%Y-%m-%d").date()
            except Exception:
                pass

        try:
            amt = Decimal(expected_amount_str)
            if schedule:
                RecurringScheduleService.update_schedule(
                    schedule=schedule,
                    user=user,
                    provider_name=provider_name,
                    category=category,
                    expected_amount=amt,
                    frequency=frequency,
                    next_due_date=next_due_date,
                    day_of_month=int(day_of_month),
                    interval_count=int(interval_count),
                    assigned_to=assigned_to,
                    is_active=is_active,
                    notes=notes,
                )
                messages.success(request, f"Recurring schedule for '{schedule.provider_name}' updated successfully.")
            else:
                schedule = RecurringScheduleService.create_schedule(
                    household=household,
                    created_by=user,
                    provider_name=provider_name,
                    category=category,
                    expected_amount=amt,
                    frequency=frequency,
                    next_due_date=next_due_date,
                    day_of_month=int(day_of_month),
                    interval_count=int(interval_count),
                    assigned_to=assigned_to,
                    is_active=is_active,
                    notes=notes,
                )
                messages.success(request, f"Recurring schedule for '{schedule.provider_name}' created successfully!")
            return redirect("dashboard_web:recurring-detail", schedule_id=schedule.id)

        except Exception as e:
            messages.error(request, f"Could not save recurring schedule: {str(e)}")

    context = {
        "schedule": schedule,
        "categories": BillCategory.choices,
        "frequencies": RecurrenceFrequency.choices,
        "members": members,
        "household": household,
        "today": timezone.localdate(),
    }
    return render(request, "recurring/form.html", context)


@login_required(login_url="dashboard_web:login")
def recurring_schedule_detail_view(request, schedule_id):
    """Inspect schedule configuration and historical generated bill occurrences."""
    user = request.user
    schedule = get_object_or_404(
        RecurringSchedule.objects.select_related("household", "assigned_to", "created_by").prefetch_related(
            "occurrences__bill"
        ),
        id=schedule_id,
        household__memberships__user=user,
    )

    occurrences = schedule.occurrences.select_related("bill").order_by("-generated_at")

    context = {
        "schedule": schedule,
        "occurrences": occurrences,
        "today": timezone.localdate(),
    }
    return render(request, "recurring/detail.html", context)


def web_login_view(request):
    """Web login view with brute-force rate-limiting."""
    if request.user.is_authenticated:
        return redirect("dashboard_web:dashboard")

    if request.method == "POST":
        client_ip = request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR", "127.0.0.1")).split(",")[0].strip()
        cache_key = f"web_login_attempts_{client_ip}"
        failed_attempts = cache.get(cache_key, 0)
        if failed_attempts >= 5:
            messages.error(request, "Too many failed login attempts. Please wait 1 minute before trying again.")
            return render(request, "accounts/login.html")

        ident = request.POST.get("username_or_email", "").strip()
        pwd = request.POST.get("password", "")

        username = ident
        if "@" in ident:
            user_obj = User.objects.filter(email__iexact=ident).first()
            if user_obj:
                username = user_obj.username

        user = authenticate(username=username, password=pwd)
        if user:
            cache.delete(cache_key)
            login(request, user)
            return redirect("dashboard_web:dashboard")

        cache.set(cache_key, failed_attempts + 1, timeout=60)
        messages.error(request, "Invalid username/email or password.")

    return render(request, "accounts/login.html")


def web_logout_view(request):
    """Web logout view. Enforces POST request for safe state change."""
    if request.method == "POST":
        logout(request)
        messages.info(request, "You have been logged out.")
        return redirect("dashboard_web:login")
    messages.warning(request, "Logout must be performed via POST.")
    return redirect("dashboard_web:dashboard")


def web_register_view(request):
    """Web registration view with strict password validation and rate-limiting."""
    if request.user.is_authenticated:
        return redirect("dashboard_web:dashboard")

    if request.method == "POST":
        client_ip = request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR", "127.0.0.1")).split(",")[0].strip()
        reg_cache_key = f"web_register_attempts_{client_ip}"
        reg_attempts = cache.get(reg_cache_key, 0)
        if reg_attempts >= 10:
            messages.error(request, "Too many registration attempts. Please wait 1 minute before trying again.")
            return render(request, "accounts/register.html")

        username = request.POST.get("username", "").strip()
        email = request.POST.get("email", "").strip()
        pwd = request.POST.get("password", "")
        pwd_confirm = request.POST.get("password_confirm", "")

        if not username or not email or not pwd:
            messages.error(request, "All fields are required.")
        elif pwd != pwd_confirm:
            messages.error(request, "Passwords do not match.")
        elif User.objects.filter(username=username).exists():
            messages.error(request, "Username is already taken.")
        elif User.objects.filter(email=email).exists():
            messages.error(request, "Email is already registered.")
        else:
            dummy_user = User(username=username, email=email)
            try:
                validate_password(pwd, user=dummy_user)
            except DjangoValidationError as err:
                for msg in err.messages:
                    messages.error(request, msg)
                return render(request, "accounts/register.html")

            try:
                user = User.objects.create_user(username=username, email=email, password=pwd)
                cache.set(reg_cache_key, reg_attempts + 1, timeout=60)
                AuditService.log(
                    action="USER_REGISTERED",
                    actor=user,
                    description=f"New user '{user.username}' registered via web form.",
                    ip_address=client_ip,
                )
                login(request, user)
                messages.success(request, "Registration successful! Welcome to HomePilot.")
                return redirect("dashboard_web:dashboard")
            except Exception as e:
                messages.error(request, f"Error creating account: {e}")

    return render(request, "accounts/register.html")


@login_required(login_url="dashboard_web:login")
def notifications_inbox_view(request):
    """
    In-app notification center inbox.
    Displays all alerts for the user, with filters for unread/all and mark-as-read actions.
    """
    user = request.user
    filter_status = request.GET.get("filter", "all")

    queryset = InAppNotification.objects.filter(user=user).select_related("bill", "reminder")
    unread_count = InAppNotification.objects.filter(user=user, is_read=False).count()

    if filter_status == "unread":
        notifications = queryset.filter(is_read=False)
    else:
        notifications = queryset

    context = {
        "notifications": notifications,
        "unread_count": unread_count,
        "filter_status": filter_status,
    }
    return render(request, "reminders/inbox.html", context)


@require_POST
@login_required(login_url="dashboard_web:login")
def notification_mark_read_view(request, notification_id):
    """Marks a single in-app alert as read."""
    notification = get_object_or_404(InAppNotification, id=notification_id, user=request.user)
    notification.is_read = True
    notification.save(update_fields=["is_read"])
    messages.success(request, "Notification marked as read.")
    return redirect("dashboard_web:notifications-inbox")


@require_POST
@login_required(login_url="dashboard_web:login")
def notification_mark_all_read_view(request):
    """Marks all in-app alerts as read for the authenticated user."""
    count = InAppNotification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    messages.success(request, f"Marked {count} notifications as read.")
    return redirect("dashboard_web:notifications-inbox")


@login_required(login_url="dashboard_web:login")
def notification_preferences_view(request):
    """
    Allows user to configure communication channels (Email, In-App, WhatsApp)
    and milestone scheduling preferences (7d, 3d, 1d, due date).
    """
    user = request.user
    prefs, _ = NotificationPreference.objects.get_or_create(
        user=user,
        defaults={
            "email_enabled": True,
            "in_app_enabled": True,
            "whatsapp_enabled": False,
            "remind_7_days_before": True,
            "remind_3_days_before": True,
            "remind_1_day_before": True,
            "remind_on_due_date": True,
        },
    )

    if request.method == "POST":
        email_enabled = "email_enabled" in request.POST
        in_app_enabled = "in_app_enabled" in request.POST
        whatsapp_enabled = "whatsapp_enabled" in request.POST
        whatsapp_phone = request.POST.get("whatsapp_phone_number", "").strip()

        remind_7_days_before = "remind_7_days_before" in request.POST
        remind_3_days_before = "remind_3_days_before" in request.POST
        remind_1_day_before = "remind_1_day_before" in request.POST
        remind_on_due_date = "remind_on_due_date" in request.POST

        # Validate phone if WhatsApp is enabled
        if whatsapp_enabled and not WhatsAppChannel.validate_phone_number(whatsapp_phone):
            messages.error(
                request,
                "WhatsApp is enabled, but the phone number is invalid. "
                "Please provide an E.164 formatted number (e.g. +919876543210 or +14155552671).",
            )
        else:
            prefs.email_enabled = email_enabled
            prefs.in_app_enabled = in_app_enabled
            prefs.whatsapp_enabled = whatsapp_enabled
            prefs.whatsapp_phone_number = whatsapp_phone
            prefs.remind_7_days_before = remind_7_days_before
            prefs.remind_3_days_before = remind_3_days_before
            prefs.remind_1_day_before = remind_1_day_before
            prefs.remind_on_due_date = remind_on_due_date
            prefs.save()

            AuditService.log(
                action="NOTIFICATION_PREFERENCES_UPDATED",
                actor=user,
                description=f"User '{user.username}' updated notification preferences.",
            )
            messages.success(request, "Notification preferences saved successfully.")
            return redirect("dashboard_web:notification-preferences")

    context = {
        "preferences": prefs,
    }
    return render(request, "reminders/preferences.html", context)


@login_required(login_url="dashboard_web:login")
def notification_deliveries_view(request):
    """
    Audit log of all multi-channel delivery attempts across the user's households.
    """
    user = request.user
    deliveries = (
        NotificationDelivery.objects.filter(
            Q(bill__household__memberships__user=user) | Q(recipient=user)
        )
        .select_related("bill", "recipient", "reminder")
        .distinct()[:100]
    )

    context = {
        "deliveries": deliveries,
    }
    return render(request, "reminders/deliveries.html", context)


@login_required(login_url="dashboard_web:login")
def spending_insights_view(request):
    """
    Household AI Spending Insights & Intelligence View.
    Calculates deterministic financial analytics in Django/Postgres,
    and displays structured executive narratives, category trends,
    provider rate increases, and forecast projections.
    """
    user = request.user
    household_id = request.GET.get("household")

    if household_id:
        household = get_object_or_404(Household, id=household_id, memberships__user=user)
    else:
        household = Household.objects.filter(memberships__user=user).first()
        if not household:
            messages.warning(request, "Please create or join a household to view spending insights.")
            return redirect("dashboard_web:dashboard")

    # Fetch active insights for this household
    active_insights = (
        SpendingInsight.objects.filter(household=household, is_dismissed=False)
        .order_by("-created_at")
    )

    # Compute full deterministic spending summary
    summary_data = SpendingInsightsService.get_full_spending_summary(household)

    # Generate or fetch executive AI explanation
    ai_narrative = SpendingInsightsService.generate_ai_explanation(
        summary_data, household, user=user
    )

    context = {
        "household": household,
        "summary": summary_data,
        "ai_narrative": ai_narrative,
        "active_insights": active_insights,
    }
    return render(request, "ai/insights.html", context)


@require_POST
@login_required(login_url="dashboard_web:login")
def dismiss_spending_insight_web(request, insight_id):
    """Dismisses an active spending insight."""
    insight = get_object_or_404(
        SpendingInsight,
        id=insight_id,
        household__memberships__user=request.user,
    )
    insight.is_dismissed = True
    insight.save(update_fields=["is_dismissed"])
    messages.success(request, f"Insight '{insight.title}' dismissed.")
    return redirect("dashboard_web:spending-insights")


@require_POST
@login_required(login_url="dashboard_web:login")
def generate_spending_insights_web(request):
    """Manually triggers anomaly and trend analysis for user's household."""
    household_id = request.POST.get("household_id")
    if household_id:
        household = get_object_or_404(Household, id=household_id, memberships__user=request.user)
    else:
        household = Household.objects.filter(memberships__user=request.user).first()

    if household:
        new_insights = SpendingInsightsService.detect_and_save_insights(household)
        messages.success(request, f"Spending analysis complete! {len(new_insights)} new insights discovered.")
    return redirect("dashboard_web:spending-insights")


@login_required(login_url="dashboard_web:login")
def ai_assistant_view(request, conversation_id=None):
    """
    Conversational AI Assistant interface for household bills.
    Enforces household permissions, displays multi-turn chat history,
    and shows controlled backend tool execution badges.
    """
    user = request.user
    user_households = Household.objects.filter(memberships__user=user)

    if not user_households.exists():
        messages.warning(request, "Please create or join a household to chat with HomePilot AI Assistant.")
        return redirect("dashboard_web:dashboard")

    household_id = request.GET.get("household")
    if household_id:
        household = get_object_or_404(user_households, id=household_id)
    else:
        household = user_households.first()

    # User's conversations for this household
    conversations = AIConversation.objects.filter(
        household=household,
        user=user,
    ).order_by("-updated_at")

    if conversation_id:
        active_conversation = get_object_or_404(
            AIConversation,
            id=conversation_id,
            household=household,
        )
        if active_conversation.user != user and not household.is_owner(user):
            messages.error(request, "You do not have permission to access this conversation.")
            return redirect("dashboard_web:ai-assistant")
    else:
        active_conversation = conversations.first()
        if not active_conversation:
            active_conversation = AIConversation.objects.create(
                household=household,
                user=user,
                title="Household Bill Assistant",
            )
            conversations = AIConversation.objects.filter(household=household, user=user)

    chat_messages = active_conversation.messages.prefetch_related("tool_executions").order_by("created_at")

    context = {
        "household": household,
        "all_households": user_households,
        "conversations": conversations,
        "active_conversation": active_conversation,
        "chat_messages": chat_messages,
    }
    return render(request, "ai/assistant.html", context)


@require_POST
@login_required(login_url="dashboard_web:login")
def ai_assistant_send_message_web(request, conversation_id):
    """
    Receives user prompt from web form, executes controlled tools within
    the household boundary, and records assistant response.
    """
    user = request.user
    conversation = get_object_or_404(
        AIConversation.objects.select_related("household"),
        id=conversation_id,
        household__memberships__user=user,
    )

    if conversation.user != user and not conversation.household.is_owner(user):
        messages.error(request, "You do not have permission to post messages to this conversation.")
        return redirect("dashboard_web:ai-assistant")

    prompt = request.POST.get("prompt", "").strip()
    if not prompt:
        messages.warning(request, "Please enter a message or question.")
        return redirect("dashboard_web:ai-assistant-conversation", conversation_id=conversation.id)

    try:
        assistant_msg, tool_executions = AIAssistantService.send_message(
            conversation=conversation,
            user=user,
            prompt=prompt,
        )
    except Exception as exc:
        logger.exception(f"Error in assistant chat: {exc}")
        messages.error(request, f"Could not process assistant request: {str(exc)}")

    return redirect("dashboard_web:ai-assistant-conversation", conversation_id=conversation.id)


@require_POST
@login_required(login_url="dashboard_web:login")
def ai_assistant_new_conversation_web(request):
    """Creates a new conversational thread for the household."""
    user = request.user
    household_id = request.POST.get("household_id")
    if household_id:
        household = get_object_or_404(Household, id=household_id, memberships__user=user)
    else:
        household = Household.objects.filter(memberships__user=user).first()

    if not household:
        messages.warning(request, "Household not found.")
        return redirect("dashboard_web:dashboard")

    count = AIConversation.objects.filter(household=household, user=user).count()
    new_conv = AIConversation.objects.create(
        household=household,
        user=user,
        title=f"Chat #{count + 1}",
    )
    return redirect("dashboard_web:ai-assistant-conversation", conversation_id=new_conv.id)


@login_required(login_url="dashboard_web:login")
@require_POST
def ai_assistant_delete_conversation_web(request, conversation_id):
    """
    Deletes an AI conversation thread.
    Enforces authorization: User must be creator or household owner.
    Logs AuditEvent.Action.CONVERSATION_DELETED.
    """
    user = request.user
    conversation = get_object_or_404(
        AIConversation.objects.select_related("household", "user"),
        id=conversation_id,
        household__memberships__user=user,
    )

    if conversation.user != user and not conversation.household.is_owner(user):
        messages.error(request, "You do not have permission to delete this conversation.")
        return redirect("dashboard_web:ai-assistant-conversation", conversation_id=conversation.id)

    title = conversation.title
    household = conversation.household
    conv_id_str = str(conversation.id)

    AuditService.log(
        action=AuditEvent.Action.CONVERSATION_DELETED,
        actor=user,
        household=household,
        entity=conversation,
        description=f"AI conversation '{title}' deleted by {user.display_name}.",
        metadata={"conversation_id": conv_id_str, "title": title},
    )

    conversation.delete()
    messages.success(request, f"Conversation '{title}' deleted successfully.")
    return redirect("dashboard_web:ai-assistant")


@login_required(login_url="dashboard_web:login")
def reports_view(request):
    """
    Household Analytics & Reports Hub.
    Presents interactive summaries for monthly spending, category distribution,
    provider trends, obligations, recurring expenses, and exports to CSV/PDF.
    """
    user = request.user
    user_households = Household.objects.filter(memberships__user=user)

    if not user_households.exists():
        messages.warning(request, "Please create or join a household to view financial reports.")
        return redirect("dashboard_web:dashboard")

    household_id = request.GET.get("household")
    if household_id:
        household = get_object_or_404(user_households, id=household_id)
    else:
        household = user_households.first()

    today = timezone.localdate()
    preset = request.GET.get("preset", "last_6_months")

    date_from = None
    date_to = None

    date_from_str = request.GET.get("date_from")
    date_to_str = request.GET.get("date_to")

    if date_from_str:
        try:
            date_from = datetime.strptime(date_from_str.strip(), "%Y-%m-%d").date()
        except ValueError:
            date_from = None

    if date_to_str:
        try:
            date_to = datetime.strptime(date_to_str.strip(), "%Y-%m-%d").date()
        except ValueError:
            date_to = None

    # Apply presets if custom dates not provided
    if not date_from and not date_to:
        if preset == "this_month":
            date_from = today.replace(day=1)
            date_to = today
        elif preset == "last_3_months":
            date_from = today - timedelta(days=90)
            date_to = today
        elif preset == "this_year":
            date_from = today.replace(month=1, day=1)
            date_to = today
        else:  # default: last_6_months
            preset = "last_6_months"
            date_from = today - timedelta(days=180)
            date_to = today

    # Calculate analytical metrics
    overview = AnalyticsService.get_overview_metrics(household, date_from, date_to)
    categories = AnalyticsService.get_category_metrics(household, date_from, date_to)
    providers = AnalyticsService.get_provider_metrics(household, date_from, date_to)
    trends = AnalyticsService.get_spending_trends(household, months=6)
    recurring = AnalyticsService.get_recurring_metrics(household)
    obligations = AnalyticsService.get_obligations_metrics(household)
    payments_data = AnalyticsService.get_payments_metrics(household, date_from, date_to, limit=8)

    context = {
        "household": household,
        "all_households": user_households,
        "preset": preset,
        "date_from": date_from.isoformat() if date_from else "",
        "date_to": date_to.isoformat() if date_to else "",
        "overview": overview,
        "categories": categories,
        "providers": providers,
        "trends": trends,
        "recurring": recurring,
        "obligations": obligations,
        "payments_data": payments_data,
    }
    return render(request, "analytics/reports.html", context)


@login_required(login_url="dashboard_web:login")
def export_report_web(request):
    """
    Downloads household financial statements in PDF or CSV format.
    Strictly enforces household tenant authorization.
    """
    user = request.user
    household_id = request.GET.get("household")
    if household_id:
        household = get_object_or_404(Household, id=household_id, memberships__user=user)
    else:
        household = Household.objects.filter(memberships__user=user).first()

    if not household:
        messages.error(request, "Household not found.")
        return redirect("dashboard_web:dashboard")

    export_format = request.GET.get("format", "pdf").lower().strip()
    report_type = request.GET.get("type", "statement").lower().strip()

    date_from = None
    date_to = None
    date_from_str = request.GET.get("date_from")
    date_to_str = request.GET.get("date_to")

    if date_from_str:
        try:
            date_from = datetime.strptime(date_from_str.strip(), "%Y-%m-%d").date()
        except ValueError:
            pass

    if date_to_str:
        try:
            date_to = datetime.strptime(date_to_str.strip(), "%Y-%m-%d").date()
        except ValueError:
            pass

    if export_format == "csv":
        if report_type == "payments":
            return CSVReportExporter.export_payments_csv(household, date_from, date_to)
        elif report_type == "recurring":
            return CSVReportExporter.export_recurring_csv(household)
        elif report_type == "overdue":
            return CSVReportExporter.export_overdue_bills_csv(household)
        elif report_type == "upcoming":
            try:
                days = int(request.GET.get("days", 30))
            except ValueError:
                days = 30
            return CSVReportExporter.export_upcoming_obligations_csv(household, days=days)
        else:
            category = request.GET.get("category")
            bill_status = request.GET.get("status")
            return CSVReportExporter.export_bills_csv(
                household=household,
                date_from=date_from,
                date_to=date_to,
                category=category,
                status=bill_status,
            )
    else:
        # PDF format
        if report_type == "payments":
            return PDFReportExporter.export_payments_statement_pdf(household, date_from, date_to)
        else:
            return PDFReportExporter.export_financial_statement_pdf(household, date_from, date_to)


@login_required(login_url="dashboard_web:login")
def audit_log_view(request):
    """
    Transparent household compliance and audit trail view.
    Displays all security, billing, extraction, and payment events.
    """
    user = request.user
    active_household_id = request.session.get("active_household_id")
    memberships = HouseholdMembership.objects.filter(user=user).select_related("household")

    if not memberships.exists():
        messages.warning(request, "Please create or join a household first.")
        return redirect("dashboard_web:household")

    active_household = None
    if active_household_id:
        active_household = Household.objects.filter(id=active_household_id, memberships__user=user).first()
    if not active_household:
        active_household = memberships.first().household
        request.session["active_household_id"] = active_household.id

    events = AuditEvent.objects.filter(household=active_household).select_related("actor")

    # Filters
    action_filter = request.GET.get("action", "").strip()
    actor_filter = request.GET.get("actor", "").strip()
    entity_filter = request.GET.get("entity_type", "").strip()
    date_from_str = request.GET.get("date_from", "").strip()
    date_to_str = request.GET.get("date_to", "").strip()

    if action_filter:
        events = events.filter(action=action_filter)
    if actor_filter:
        events = events.filter(actor_id=actor_filter)
    if entity_filter:
        events = events.filter(entity_type__iexact=entity_filter)
    if date_from_str:
        try:
            d_from = datetime.strptime(date_from_str, "%Y-%m-%d").date()
            events = events.filter(created_at__date__gte=d_from)
        except ValueError:
            pass
    if date_to_str:
        try:
            d_to = datetime.strptime(date_to_str, "%Y-%m-%d").date()
            events = events.filter(created_at__date__lte=d_to)
        except ValueError:
            pass

    events = events.order_by("-created_at")

    # Pagination
    from django.core.paginator import Paginator
    paginator = Paginator(events, 25)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    members = User.objects.filter(household_memberships__household=active_household)
    entity_types = ["Bill", "Payment", "BillDocument", "Document", "RecurringSchedule", "Household", "User", "AIConversation"]

    context = {
        "household": active_household,
        "events": page_obj,
        "page_obj": page_obj,
        "actions": AuditEvent.Action.choices,
        "selected_action": action_filter,
        "selected_actor": actor_filter,
        "selected_entity": entity_filter,
        "selected_date_from": date_from_str,
        "selected_date_to": date_to_str,
        "members": members,
        "entity_types": entity_types,
    }
    return render(request, "audit/index.html", context)


