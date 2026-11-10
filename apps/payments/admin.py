from django.contrib import admin
from .models import Payment, PaymentReceipt, PaymentAdjustment


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("id", "bill", "household", "paid_by", "amount", "payment_date", "payment_method", "created_at")
    list_filter = ("payment_method", "payment_date", "household")
    search_fields = ("transaction_reference", "notes")


@admin.register(PaymentReceipt)
class PaymentReceiptAdmin(admin.ModelAdmin):
    list_display = ("id", "payment", "file_name", "file_size", "uploaded_at")


@admin.register(PaymentAdjustment)
class PaymentAdjustmentAdmin(admin.ModelAdmin):
    list_display = ("id", "payment", "adjusted_by", "amount", "reason", "created_at")
