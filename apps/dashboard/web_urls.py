from django.urls import path
from .web_views import (
    index_redirect_view,
    dashboard_view,
    bills_list_view,
    bill_upload_view,
    mark_bill_paid_web,
    household_view,
    web_login_view,
    web_logout_view,
    web_register_view,
)

app_name = "dashboard_web"

urlpatterns = [
    path("", index_redirect_view, name="index"),
    path("dashboard/", dashboard_view, name="dashboard"),
    path("bills/", bills_list_view, name="bills-list"),
    path("bills/upload/", bill_upload_view, name="bill-upload"),
    path("bills/<int:bill_id>/mark-paid/", mark_bill_paid_web, name="mark-paid"),
    path("household/", household_view, name="household"),
    path("login/", web_login_view, name="login"),
    path("logout/", web_logout_view, name="logout"),
    path("register/", web_register_view, name="register"),
]
