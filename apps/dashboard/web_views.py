from decimal import Decimal
from datetime import datetime
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Sum, Q
from django.utils import timezone

from apps.bills.models import Bill, BillDocument, BillStatus, BillCategory
from apps.bills.validators import validate_bill_document
from apps.bills.services.document_parser import DocumentParserService
from apps.bills.services.extraction import get_extraction_provider
from apps.reminders.services.reminder_service import ReminderService
from apps.households.models import Household, HouseholdMembership
from apps.accounts.models import User


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
    elif status_filter == "OVERDUE":
        bills = bills.filter(
            Q(status=BillStatus.OVERDUE) | (Q(status=BillStatus.PENDING) & Q(due_date__lt=today))
        )
    elif status_filter == "PAID":
        bills = bills.filter(status=BillStatus.PAID)

    bills = bills.order_by("due_date")

    context = {
        "bills": bills,
        "current_status": status_filter,
    }
    return render(request, "bills/list.html", context)


@login_required(login_url="dashboard_web:login")
def mark_bill_paid_web(request, bill_id):
    """Quick web action to mark a bill as paid from the UI."""
    bill = get_object_or_404(
        Bill,
        id=bill_id,
        household__memberships__user=request.user,
    )
    bill.mark_as_paid()
    messages.success(request, f"Bill '{bill.provider_name}' has been marked as paid!")
    return redirect(request.META.get("HTTP_REFERER", "dashboard_web:dashboard"))


@login_required(login_url="dashboard_web:login")
def bill_upload_view(request):
    """
    Two-step web flow for uploading a bill document, running AI extraction,
    and presenting a review/correction screen before final saving.
    """
    user = request.user
    households = Household.objects.filter(memberships__user=user)

    if not households.exists():
        messages.error(request, "Please create or join a household first.")
        return redirect("dashboard_web:household")

    active_household = households.first()
    members = User.objects.filter(household_memberships__household=active_household)

    step = "upload"  # or "review"
    extracted_data = {}
    doc_id = None

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "upload_file":
            uploaded_file = request.FILES.get("bill_file")
            if not uploaded_file:
                messages.error(request, "Please select a file to upload.")
            else:
                try:
                    validate_bill_document(uploaded_file)
                    # Create draft document
                    doc = BillDocument.objects.create(
                        household=active_household,
                        uploaded_by=user,
                        file=uploaded_file,
                        file_name=uploaded_file.name,
                        file_size=uploaded_file.size,
                        mime_type=getattr(uploaded_file, "content_type", "application/octet-stream"),
                    )

                    # Extract text and run provider
                    raw_text = DocumentParserService.extract_text(doc.file, doc.mime_type)
                    doc.raw_text = raw_text
                    provider = get_extraction_provider()
                    result = provider.extract(raw_text, doc.mime_type)
                    doc.extracted_data = result.model_dump(mode="json")
                    doc.extraction_status = BillDocument.ExtractionStatus.SUCCESS
                    doc.save()

                    step = "review"
                    doc_id = doc.id
                    extracted_data = doc.extracted_data
                    messages.success(request, "Document parsed! Please review the details below.")
                except Exception as e:
                    messages.error(request, f"Upload failed: {str(e)}")

        elif action == "confirm_bill":
            doc_id = request.POST.get("doc_id")
            doc = get_object_or_404(BillDocument, id=doc_id, household__memberships__user=user)

            try:
                amt = Decimal(request.POST.get("amount", "0"))
                if amt <= 0:
                    raise ValueError("Amount must be greater than zero.")

                due_date_str = request.POST.get("due_date")
                if not due_date_str:
                    raise ValueError("Due date is required.")

                due_date = datetime.strptime(due_date_str, "%Y-%m-%d").date()

                issue_date_str = request.POST.get("issue_date")
                issue_date = datetime.strptime(issue_date_str, "%Y-%m-%d").date() if issue_date_str else None

                assigned_to_id = request.POST.get("assigned_to")
                assigned_to = User.objects.filter(id=assigned_to_id).first() if assigned_to_id else None

                bill = Bill.objects.create(
                    household=doc.household,
                    uploaded_by=user,
                    assigned_to=assigned_to,
                    provider_name=request.POST.get("provider_name", "Utility Provider").strip(),
                    category=request.POST.get("category", BillCategory.OTHER),
                    amount=amt,
                    currency=request.POST.get("currency", "INR"),
                    billing_period=request.POST.get("billing_period", "").strip(),
                    due_date=due_date,
                    issue_date=issue_date,
                    reference_number=request.POST.get("reference_number", "").strip(),
                    notes=request.POST.get("notes", "").strip(),
                )

                doc.bill = bill
                doc.save(update_fields=["bill"])

                ReminderService.create_reminders_for_bill(bill)
                messages.success(request, f"Bill '{bill.provider_name}' saved successfully!")
                return redirect("dashboard_web:dashboard")

            except Exception as e:
                messages.error(request, f"Could not save bill: {str(e)}")
                step = "review"

    context = {
        "step": step,
        "doc_id": doc_id,
        "extracted_data": extracted_data,
        "household": active_household,
        "members": members,
        "categories": BillCategory.choices,
    }
    return render(request, "bills/upload.html", context)


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
        current_membership = memberships.first()

    household = current_membership.household
    is_owner = current_membership.role == HouseholdMembership.Role.OWNER
    members = household.memberships.select_related("user").all()

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
            HouseholdMembership.objects.create(
                household=household,
                user=target_user,
                role=role,
            )
            messages.success(request, f"Successfully added {target_user.display_name} as {role}!")
            return redirect("dashboard_web:household")

    context = {
        "household": household,
        "is_owner": is_owner,
        "members": members,
    }
    return render(request, "households/index.html", context)


def web_login_view(request):
    """Simple web login view."""
    if request.user.is_authenticated:
        return redirect("dashboard_web:dashboard")

    if request.method == "POST":
        ident = request.POST.get("username_or_email", "").strip()
        pwd = request.POST.get("password", "")

        username = ident
        if "@" in ident:
            user_obj = User.objects.filter(email__iexact=ident).first()
            if user_obj:
                username = user_obj.username

        user = authenticate(username=username, password=pwd)
        if user:
            login(request, user)
            return redirect("dashboard_web:dashboard")
        messages.error(request, "Invalid username/email or password.")

    return render(request, "accounts/login.html")


def web_logout_view(request):
    """Web logout view."""
    logout(request)
    messages.info(request, "You have been logged out.")
    return redirect("dashboard_web:login")


def web_register_view(request):
    """Simple web registration view."""
    if request.user.is_authenticated:
        return redirect("dashboard_web:dashboard")

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        email = request.POST.get("email", "").strip()
        pwd = request.POST.get("password", "")
        pwd_confirm = request.POST.get("password_confirm", "")

        if pwd != pwd_confirm:
            messages.error(request, "Passwords do not match.")
        elif User.objects.filter(username=username).exists():
            messages.error(request, "Username is already taken.")
        elif User.objects.filter(email=email).exists():
            messages.error(request, "Email is already registered.")
        else:
            try:
                user = User.objects.create_user(username=username, email=email, password=pwd)
                login(request, user)
                messages.success(request, "Registration successful! Welcome to HomePilot.")
                return redirect("dashboard_web:dashboard")
            except Exception as e:
                messages.error(request, f"Error creating account: {e}")

    return render(request, "accounts/register.html")
