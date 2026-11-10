import logging
from decimal import Decimal
from typing import Optional
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.bills.models import Bill, BillStatus
from apps.payments.models import Payment, PaymentReceipt, PaymentAdjustment, PaymentMethod, PaymentStatus
from apps.audit.services import AuditService
from apps.audit.models import AuditEvent

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
        currency: str = "",
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
                currency=currency or locked_bill.currency,
                status=PaymentStatus.COMPLETED,
                payment_date=payment_date,
                payment_method=payment_method,
                transaction_reference=transaction_reference.strip(),
                notes=notes.strip(),
            )

            # Recalculate bill balance and advance status
            locked_bill.recalculate_balance()

            AuditService.log(
                action=AuditEvent.Action.PAYMENT_RECORDED,
                actor=user,
                household=locked_bill.household,
                entity=payment,
                description=f"Recorded payment of {payment.amount} {locked_bill.currency} for Bill #{bill.id}.",
                metadata={"amount": str(payment.amount), "method": payment.payment_method},
            )

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

            # Preserve historical payment.amount; evaluate net amount through cumulative adjustments
            adj_sum_before = sum((a.amount for a in payment.adjustments.all()), Decimal("0.00"))
            new_net = payment.amount + adj_sum_before + amount

            if new_net < Decimal("0.00"):
                raise ValidationError("Payment amount after adjustment cannot be negative.")
            if new_net > bill.amount:
                raise ValidationError("Payment amount after adjustment cannot exceed total bill amount.")

            adjustment = PaymentAdjustment.objects.create(
                payment=payment,
                adjusted_by=adjusted_by,
                amount=amount,
                reason=reason.strip(),
            )

            # Update status reflecting adjusted state
            if new_net == Decimal("0.00"):
                payment.status = PaymentStatus.VOIDED
            elif new_net < payment.amount:
                payment.status = PaymentStatus.PARTIALLY_REFUNDED
            else:
                payment.status = PaymentStatus.COMPLETED
            payment.save(update_fields=["status", "updated_at"])

            bill.recalculate_balance()

            AuditService.log(
                action=AuditEvent.Action.PAYMENT_ADJUSTED,
                actor=adjusted_by,
                household=bill.household,
                entity=adjustment,
                description=f"Adjustment of {amount} on Payment #{payment.id}. Reason: {reason}.",
                metadata={"amount": str(amount), "reason": reason, "new_net_amount": str(new_net)},
            )

            if payment.status == PaymentStatus.VOIDED:
                AuditService.log(
                    action=AuditEvent.Action.PAYMENT_REVERSED,
                    actor=adjusted_by,
                    household=bill.household,
                    entity=payment,
                    description=f"Payment #{payment.id} reversed/voided. Reason: {reason}.",
                    metadata={"adjustment_id": adjustment.id, "reason": reason, "original_amount": str(payment.amount)},
                )

        return adjustment

    @classmethod
    def void_payment(
        cls,
        payment: Payment,
        user,
        reason: str = "Voided by household owner",
    ) -> PaymentAdjustment:
        """
        Completely voids a payment transaction by applying a full reversal adjustment.
        Preserves complete audit and payment history.
        """
        if not user or not payment.household.is_owner(user):
            raise ValidationError("Only household owners can void payments.")

        current_net = payment.net_amount
        if current_net <= Decimal("0.00"):
            raise ValidationError("Payment has already been fully voided or refunded.")

        return cls.record_adjustment(
            payment=payment,
            adjusted_by=user,
            amount=-current_net,
            reason=reason.strip() or "Voided by household owner",
        )
