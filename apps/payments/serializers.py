from decimal import Decimal
from rest_framework import serializers
from .models import Payment, PaymentReceipt, PaymentAdjustment, PaymentMethod
from apps.accounts.serializers import UserSerializer
from apps.bills.models import Bill, BillStatus


class PaymentReceiptSerializer(serializers.ModelSerializer):
    """Serializer for payment proof receipts."""
    class Meta:
        model = PaymentReceipt
        fields = (
            "id",
            "file",
            "file_name",
            "file_size",
            "mime_type",
            "uploaded_at",
        )
        read_only_fields = fields


class PaymentAdjustmentSerializer(serializers.ModelSerializer):
    """Serializer for payment adjustments and reversals."""
    adjusted_by = UserSerializer(read_only=True)

    class Meta:
        model = PaymentAdjustment
        fields = (
            "id",
            "adjusted_by",
            "amount",
            "reason",
            "created_at",
        )
        read_only_fields = fields


class PaymentSerializer(serializers.ModelSerializer):
    """Detailed representation of a payment record."""
    paid_by = UserSerializer(read_only=True)
    bill_provider = serializers.CharField(source="bill.provider_name", read_only=True)
    bill_amount = serializers.DecimalField(source="bill.amount", max_digits=10, decimal_places=2, read_only=True)
    receipt = PaymentReceiptSerializer(read_only=True)
    adjustments = PaymentAdjustmentSerializer(many=True, read_only=True)

    class Meta:
        model = Payment
        fields = (
            "id",
            "bill",
            "bill_provider",
            "bill_amount",
            "household",
            "paid_by",
            "amount",
            "payment_date",
            "payment_method",
            "transaction_reference",
            "notes",
            "receipt",
            "adjustments",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "household", "paid_by", "created_at", "updated_at")


class PaymentCreateSerializer(serializers.ModelSerializer):
    """Serializer for recording a new payment against a bill."""
    bill_id = serializers.PrimaryKeyRelatedField(
        queryset=Bill.objects.all(),
        source="bill",
        write_only=True,
    )
    receipt_file = serializers.FileField(required=False, write_only=True)

    class Meta:
        model = Payment
        fields = (
            "id",
            "bill_id",
            "amount",
            "payment_date",
            "payment_method",
            "transaction_reference",
            "notes",
            "receipt_file",
        )
        read_only_fields = ("id",)

    def validate_amount(self, value):
        if value <= Decimal("0.00"):
            raise serializers.ValidationError("Payment amount must be greater than zero.")
        return value

    def validate(self, attrs):
        request = self.context.get("request")
        bill = attrs.get("bill")
        amount = attrs.get("amount")

        if bill and request and not bill.household.is_member(request.user):
            raise serializers.ValidationError({"bill_id": "You do not have access to this bill's household."})

        if bill and bill.status == BillStatus.PAID and bill.outstanding_balance <= Decimal("0.00"):
            raise serializers.ValidationError({"bill_id": f"Bill '{bill.provider_name}' has already been paid in full."})

        if bill and amount and amount > bill.outstanding_balance:
            raise serializers.ValidationError(
                {"amount": f"Payment amount ({amount}) exceeds the bill's outstanding balance ({bill.outstanding_balance})."}
            )

        return attrs
