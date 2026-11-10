"""
URL configuration for HomePilot project.
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/", include("apps.accounts.urls")),
    path("api/households/", include("apps.households.urls")),
    path("api/bills/", include("apps.bills.urls")),
    path("api/reminders/", include("apps.reminders.urls")),
    path("api/dashboard/", include("apps.dashboard.urls")),
    path("", include("apps.dashboard.web_urls")),  # Frontend template routes
]

# Serve media and static files in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
