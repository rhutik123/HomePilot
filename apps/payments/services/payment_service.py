import logging
from decimal import Decimal
from typing import Optional
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.bills.models import Bill, BillStatus
from apps.payments.models import Payment, PaymentReceipt, PaymentAdjustment, PaymentMethod

logger = logging.getLogger(__name__)


class PaymentService:
    """
    Encapsulates all financial operations for recording, updating,
    and adjusting bill payments with atomic precision.
    """
    @classmethod
    def record_payment(
        cls,
        bill: Bill,
        user,
        amount: Decimal,
        payment_date=None,
        payment_method: str = PaymentMethod.UPI,
        transaction_reference: str = "",
        notes: str = "",
    ) -> Payment:
        """
        Atomically records a payment against a bill and recalculates the outstanding balance.
        """
        if not user or not user.is_authenticated or not bill.household.is_member(user):
            raise ValidationError("You do not have permission to record payments for this household.")

        if not isinstance(amount, Decimal):
            try:
                amount = Decimal(str(amount))
            except Exception:
                raise ValidationError("Payment amount must be a valid decimal number.")

        if amount <= Decimal("0.00"):
            raise ValidationError("Payment amount must be greater than zero.")

        payment_date = payment_date or timezone.localdate()

        with transaction.atomic():
            # Acquire row lock on the bill to prevent concurrent race conditions
            locked_bill = Bill.objects.select_for_update().get(id=bill.id)

            if locked_bill.status == BillStatus.PAID and locked_bill.outstanding_balance <= Decimal("0.00"):
                raise ValidationError(f"Bill '{locked_bill.provider_name}' has already been paid in full.")

            if amount > locked_bill.outstanding_balance:
                raise ValidationError(
                    f"Payment amount ({amount}) cannot exceed the outstanding balance ({locked_bill.outstanding_balance})."
                )

            payment = Payment.objects.create(
                bill=locked_bill,
                household=locked_bill.household,
                paid_by=user,
                amount=amount,
                payment_date=payment_date,
                payment_method=payment_method,
                transaction_reference=transaction_reference.strip(),
                notes=notes.strip(),
            )

            # Recalculate bill balance and advance status
            locked_bill.recalculate_balance()

        logger.info(
            f"Recorded payment #{payment.id} of {payment.amount} for Bill #{bill.id}. "
            f"Remaining balance: {locked_bill.outstanding_balance}."
        )
        return payment

    @classmethod
    def attach_receipt(cls, payment: Payment, user, file_obj) -> PaymentReceipt:
        """Attaches an uploaded receipt image or PDF to a payment record."""
        if not user or not payment.household.is_member(user):
            raise ValidationError("You do not have access to attach receipts to this payment.")

        receipt, _ = PaymentReceipt.objects.update_or_create(
            payment=payment,
            defaults={
                "file": file_obj,
                "file_name": file_obj.name,
                "file_size": file_obj.size,
                "mime_type": getattr(file_obj, "content_type", "application/octet-stream"),
            },
        )
        return receipt

    @classmethod
    def record_adjustment(
        cls,
        payment: Payment,
        adjusted_by,
        amount: Decimal,
        reason: str,
    ) -> PaymentAdjustment:
        """
        Records an adjustment or partial reversal to a payment and updates bill balance.
        """
        if not adjusted_by or not payment.household.is_owner(adjusted_by):
            raise ValidationError("Only household owners can perform payment adjustments.")

        if not isinstance(amount, Decimal):
            amount = Decimal(str(amount))

        if not reason or not reason.strip():
            raise ValidationError("A clear reason is required for payment adjustments.")

        with transaction.atomic():
            bill = Bill.objects.select_for_update().get(id=payment.bill_id)

            # Update payment amount
            payment.amount = payment.amount + amount
            if payment.amount < Decimal("0.00"):
                raise ValidationError("Payment amount after adjustment cannot be negative.")
            payment.save(update_fields=["amount", "updated_at"])

            adjustment = PaymentAdjustment.objects.create(
                payment=payment,
                adjusted_by=adjusted_by,
                amount=amount,
                reason=reason.strip(),
            )

            bill.recalculate_balance()

        return adjustment
