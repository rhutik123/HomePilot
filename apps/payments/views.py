from decimal import Decimal
from django.shortcuts import get_object_or_404
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import viewsets, status, generics
from rest_framework.views import APIView
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

from .models import Payment, PaymentReceipt, PaymentAdjustment
from .serializers import (
    PaymentSerializer,
    PaymentCreateSerializer,
    PaymentReceiptSerializer,
    PaymentAdjustmentSerializer,
)
from .services.payment_service import PaymentService
from apps.bills.models import Bill


class PaymentViewSet(viewsets.ModelViewSet):
    """
    ViewSet for inspecting and recording household bill payments.
    Enforces multi-tenant isolation and atomic balance adjustments.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_queryset(self):
        queryset = (
            Payment.objects.filter(household__memberships__user=self.request.user)
            .select_related("bill", "paid_by", "household")
            .prefetch_related("receipt", "adjustments")
            .distinct()
        )

        bill_id = self.request.query_params.get("bill")
        if bill_id:
            queryset = queryset.filter(bill_id=bill_id)

        household_id = self.request.query_params.get("household")
        if household_id:
            queryset = queryset.filter(household_id=household_id)

        method = self.request.query_params.get("payment_method")
        if method:
            queryset = queryset.filter(payment_method=method.upper())

        return queryset

    def get_serializer_class(self):
        if self.action == "create":
            return PaymentCreateSerializer
        return PaymentSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        bill = serializer.validated_data["bill"]
        amount = serializer.validated_data["amount"]
        payment_date = serializer.validated_data.get("payment_date")
        payment_method = serializer.validated_data.get("payment_method")
        tx_ref = serializer.validated_data.get("transaction_reference", "")
        notes = serializer.validated_data.get("notes", "")
        receipt_file = serializer.validated_data.get("receipt_file")

        try:
            payment = PaymentService.record_payment(
                bill=bill,
                user=request.user,
                amount=amount,
                payment_date=payment_date,
                payment_method=payment_method,
                transaction_reference=tx_ref,
                notes=notes,
            )

            if receipt_file:
                PaymentService.attach_receipt(payment, request.user, receipt_file)

            return Response(
                {
                    "message": f"Payment of {payment.amount} recorded successfully.",
                    "payment": PaymentSerializer(payment).data,
                    "bill_remaining_balance": str(payment.bill.outstanding_balance),
                    "bill_status": payment.bill.status,
                },
                status=status.HTTP_201_CREATED,
            )
        except DjangoValidationError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser, FormParser])
    def upload_receipt(self, request, pk=None):
        """Attaches an uploaded receipt image or PDF to an existing payment record."""
        payment = self.get_object()
        receipt_file = request.FILES.get("receipt_file") or request.FILES.get("file")

        if not receipt_file:
            return Response({"error": "No receipt file provided."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            receipt = PaymentService.attach_receipt(payment, request.user, receipt_file)
            return Response(
                {
                    "message": "Payment receipt attached successfully.",
                    "receipt": PaymentReceiptSerializer(receipt).data,
                },
                status=status.HTTP_201_CREATED,
            )
        except DjangoValidationError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"])
    def adjust(self, request, pk=None):
        """Records an adjustment or partial reversal on a payment (restricted to owners)."""
        payment = self.get_object()
        amount_raw = request.data.get("amount")
        reason = request.data.get("reason", "")

        if amount_raw is None:
            return Response({"error": "Adjustment amount is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            amount = Decimal(str(amount_raw))
            adjustment = PaymentService.record_adjustment(
                payment=payment,
                adjusted_by=request.user,
                amount=amount,
                reason=reason,
            )
            payment.refresh_from_db()
            payment.bill.refresh_from_db()
            return Response(
                {
                    "message": "Payment adjustment recorded.",
                    "adjustment": PaymentAdjustmentSerializer(adjustment).data,
                    "new_payment_amount": str(payment.amount),
                    "bill_remaining_balance": str(payment.bill.outstanding_balance),
                    "bill_status": payment.bill.status,
                },
                status=status.HTTP_200_OK,
            )
        except (DjangoValidationError, Exception) as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class BillPaymentListCreateAPIView(generics.ListCreateAPIView):
    """
    Nested endpoint for listing and creating payments specifically for a given bill:
    GET/POST /api/v1/bills/{bill_id}/payments/
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_bill(self):
        bill_id = self.kwargs.get("bill_id")
        return get_object_or_404(
            Bill.objects.select_related("household"),
            id=bill_id,
            household__memberships__user=self.request.user,
        )

    def get_queryset(self):
        bill = self.get_bill()
        return Payment.objects.filter(bill=bill).select_related("paid_by", "receipt").prefetch_related("adjustments")

    def get_serializer_class(self):
        if self.request.method == "POST":
            return PaymentCreateSerializer
        return PaymentSerializer

    def create(self, request, *args, **kwargs):
        bill = self.get_bill()
        data = request.data.copy()
        data["bill_id"] = bill.id

        serializer = PaymentCreateSerializer(data=data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        amount = serializer.validated_data["amount"]
        payment_date = serializer.validated_data.get("payment_date")
        payment_method = serializer.validated_data.get("payment_method")
        tx_ref = serializer.validated_data.get("transaction_reference", "")
        notes = serializer.validated_data.get("notes", "")
        receipt_file = serializer.validated_data.get("receipt_file")

        try:
            payment = PaymentService.record_payment(
                bill=bill,
                user=request.user,
                amount=amount,
                payment_date=payment_date,
                payment_method=payment_method,
                transaction_reference=tx_ref,
                notes=notes,
            )

            if receipt_file:
                PaymentService.attach_receipt(payment, request.user, receipt_file)

            return Response(
                {
                    "message": f"Payment of {payment.amount} recorded for '{bill.provider_name}'.",
                    "payment": PaymentSerializer(payment).data,
                    "bill_remaining_balance": str(payment.bill.outstanding_balance),
                    "bill_status": payment.bill.status,
                },
                status=status.HTTP_201_CREATED,
            )
        except DjangoValidationError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
