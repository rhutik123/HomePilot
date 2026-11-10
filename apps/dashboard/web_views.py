from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Sum, Q
from django.utils import timezone

from apps.bills.models import Bill, BillDocument, BillStatus, BillCategory
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

    households = Household.objects.filter(memberships__user=user)

    context = {
        "today": today,
        "metrics": {
            "pending_count": pending_qs.count(),
            "pending_total": pending_qs.aggregate(total=Sum("amount"))["total"] or 0,
            "overdue_count": overdue_qs.count(),
            "overdue_total": overdue_qs.aggregate(total=Sum("amount"))["total"] or 0,
            "paid_count": paid_qs.count(),
            "paid_total": paid_qs.aggregate(total=Sum("amount"))["total"] or 0,
            "assigned_to_me_count": assigned_qs.count(),
        },
        "upcoming_bills": upcoming_bills,
        "overdue_bills": overdue_bills,
        "recently_paid": recently_paid,
        "assigned_to_me": assigned_to_me,
        "households": households,
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
