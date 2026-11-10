from decimal import Decimal
from rest_framework import serializers
from .models import RecurringSchedule, RecurringOccurrence, RecurrenceFrequency
from apps.accounts.serializers import UserSerializer
from apps.households.models import Household
from apps.bills.models import BillCategory


class RecurringOccurrenceSerializer(serializers.ModelSerializer):
    """Serializer representing an individual generated bill occurrence."""
    bill_provider = serializers.CharField(source="bill.provider_name", read_only=True)
    bill_amount = serializers.DecimalField(source="bill.amount", max_digits=10, decimal_places=2, read_only=True)
    bill_due_date = serializers.DateField(source="bill.due_date", read_only=True)
    bill_status = serializers.CharField(source="bill.status", read_only=True)

    class Meta:
        model = RecurringOccurrence
        fields = (
            "id",
            "schedule",
            "bill",
            "bill_provider",
            "bill_amount",
            "bill_due_date",
            "bill_status",
            "period_identifier",
            "generated_at",
        )
        read_only_fields = fields


class RecurringScheduleSerializer(serializers.ModelSerializer):
    """Detailed serializer for managing recurring bill templates."""
    created_by = UserSerializer(read_only=True)
    assigned_to_user = UserSerializer(source="assigned_to", read_only=True)
    household_name = serializers.CharField(source="household.name", read_only=True)
    occurrences_count = serializers.SerializerMethodField()

    class Meta:
        model = RecurringSchedule
        fields = (
            "id",
            "household",
            "household_name",
            "created_by",
            "assigned_to",
            "assigned_to_user",
            "provider_name",
            "category",
            "expected_amount",
            "currency",
            "frequency",
            "interval_count",
            "day_of_month",
            "next_due_date",
            "is_active",
            "notes",
            "occurrences_count",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_by", "household_name", "occurrences_count", "created_at", "updated_at")

    def get_occurrences_count(self, obj):
        return obj.occurrences.count()

    def validate_expected_amount(self, value):
        if value <= Decimal("0.00"):
            raise serializers.ValidationError("Expected amount must be greater than zero.")
        return value

    def validate_day_of_month(self, value):
        if value < 1 or value > 31:
            raise serializers.ValidationError("Day of month must be between 1 and 31.")
        return value

    def validate(self, attrs):
        request = self.context.get("request")
        household = attrs.get("household") or (self.instance.household if self.instance else None)
        assigned_to = attrs.get("assigned_to") if "assigned_to" in attrs else (self.instance.assigned_to if self.instance else None)

        if not self.instance and household and request and request.user.is_authenticated:
            if not household.is_member(request.user):
                raise serializers.ValidationError({"household": "You do not belong to this household."})

        if household and assigned_to:
            if not household.is_member(assigned_to):
                raise serializers.ValidationError(
                    {"assigned_to": f"User {assigned_to.display_name} is not a member of {household.name}."}
                )

        return attrs
