from django.apps import AppConfig


class RecurringConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.recurring"
    verbose_name = "Recurring Bills"
