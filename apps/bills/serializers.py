from rest_framework import serializers
from .models import Bill, BillDocument, BillCategory, BillStatus
from apps.accounts.serializers import UserSerializer
from apps.households.models import Household


class BillDocumentSerializer(serializers.ModelSerializer):
    """Serializer for bill documents and extraction results."""
    class Meta:
        model = BillDocument
        fields = (
            "id",
            "file",
            "file_name",
            "file_size",
            "mime_type",
            "raw_text",
            "extraction_status",
            "extracted_data",
            "created_at",
        )
        read_only_fields = (
            "id",
            "file_name",
            "file_size",
            "mime_type",
            "raw_text",
            "extraction_status",
            "extracted_data",
            "created_at",
        )


class BillSerializer(serializers.ModelSerializer):
    """Detailed representation of a confirmed bill."""
    uploaded_by = UserSerializer(read_only=True)
    assigned_to_user = UserSerializer(source="assigned_to", read_only=True)
    documents = BillDocumentSerializer(many=True, read_only=True)
    is_overdue = serializers.BooleanField(read_only=True)
    household_name = serializers.CharField(source="household.name", read_only=True)

    class Meta:
        model = Bill
        fields = (
            "id",
            "household",
            "household_name",
            "uploaded_by",
            "assigned_to",
            "assigned_to_user",
            "provider_name",
            "category",
            "amount",
            "currency",
            "billing_period",
            "issue_date",
            "due_date",
            "reference_number",
            "notes",
            "status",
            "paid_at",
            "is_overdue",
            "documents",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "uploaded_by", "paid_at", "created_at", "updated_at")

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Bill amount must be greater than zero.")
        return value

    def validate(self, attrs):
        household = attrs.get("household") or (self.instance.household if self.instance else None)
        assigned_to = attrs.get("assigned_to")

        # Ensure assigned user is an actual member of the household
        if household and assigned_to:
            if not household.is_member(assigned_to):
                raise serializers.ValidationError(
                    {"assigned_to": f"User {assigned_to.display_name} is not a member of {household.name}."}
                )

        # Ensure issue_date is not after due_date if both are present
        issue_date = attrs.get("issue_date") or (self.instance.issue_date if self.instance else None)
        due_date = attrs.get("due_date") or (self.instance.due_date if self.instance else None)
        if issue_date and due_date and issue_date > due_date:
            raise serializers.ValidationError(
                {"due_date": "Due date cannot be earlier than the bill issue date."}
            )

        return attrs


class BillDocumentUploadSerializer(serializers.ModelSerializer):
    """
    Serializer specifically for uploading a bill document file.
    Validates that the user belongs to the target household.
    """
    household_id = serializers.PrimaryKeyRelatedField(
        queryset=Household.objects.all(),
        source="household",
        write_only=True,
    )

    class Meta:
        model = BillDocument
        fields = ("id", "household_id", "file", "created_at")
        read_only_fields = ("id", "created_at")

    def validate_household_id(self, household):
        request = self.context.get("request")
        if not household.is_member(request.user):
            raise serializers.ValidationError("You do not have access to upload documents for this household.")
        return household

    def create(self, validated_data):
        request = self.context.get("request")
        uploaded_file = validated_data["file"]

        document = BillDocument.objects.create(
            household=validated_data["household"],
            uploaded_by=request.user,
            file=uploaded_file,
            file_name=uploaded_file.name,
            file_size=uploaded_file.size,
            mime_type=getattr(uploaded_file, "content_type", "application/octet-stream"),
        )
        return document
