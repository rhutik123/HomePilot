"""
URL configuration for HomePilot project.
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path("admin/", admin.site.urls),
    # Versioned v1 API routes
    path("api/v1/documents/", include("apps.documents.urls")),
    path("api/v1/payments/", include("apps.payments.urls")),
    path("api/v1/recurring/", include("apps.recurring.urls")),
    path("api/v1/reminders/", include("apps.reminders.urls")),
    path("api/v1/ai/", include("apps.ai.urls")),
    path("api/v1/analytics/", include("apps.analytics.urls")),
    # Standard API routes
    path("api/auth/", include("apps.accounts.urls")),
    path("api/households/", include("apps.households.urls")),
    path("api/bills/", include("apps.bills.urls")),
    path("api/dashboard/", include("apps.dashboard.urls")),
    path("", include("apps.dashboard.web_urls")),  # Frontend template routes
]

# Serve static files in development (Media files are protected and served only via authenticated endpoints)
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
