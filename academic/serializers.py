"""Serializers for SCOUP academic APIs."""

import uuid
from rest_framework import serializers

from .models import Faculty, Paper, Patent, Project, School, Department, ContactTeamMember, ContactPageSettings


# ---------------------------------------------------------------------------
# School & Department
# ---------------------------------------------------------------------------

class SchoolSerializer(serializers.ModelSerializer):
    department_count = serializers.SerializerMethodField()

    class Meta:
        model = School
        fields = ["id", "name", "code", "is_active", "display_order", "department_count"]

    def get_department_count(self, obj):
        return obj.departments.count()


class DepartmentSerializer(serializers.ModelSerializer):
    school_name = serializers.SerializerMethodField()

    class Meta:
        model = Department
        fields = ["id", "name", "code", "school", "school_name", "is_active"]

    def get_school_name(self, obj):
        return obj.school.name if obj.school else None


class EmptyStringToNoneDateField(serializers.DateField):
    def to_internal_value(self, value):
        if value in ("", None):
            return None
        return super().to_internal_value(value)

FACULTY_API_FIELDS = [
    "id",
    "user",
    "faculty_id",
    "first_name",
    "last_name",
    "name",
    "title",
    "department",
    "email",
    "office",
    "room",
    "phone",
    "bio",
    "research_interests",
    "qualifications",
    "personal_website",
    "faculty_keywords",
    "ai_keywords",
    "profile_visibility",
    "show_email_publicly",
    "show_phone_publicly",
    "allow_messages_through_scoup",
    "is_approved",
    "institutional_email",
    "institutional_email_verified",
    "photo",
    "created_at",
    "updated_at",
    "total_citations",
    "article_count",
    "average_citations",
    "primary_school",
    "primary_department",
    "schools",
    "departments",
    "review_status",
    "confirmed_su_faculty",
    "cleanup_notes",
    "school",
    "school_affiliations",
    "department_affiliations",
    "dois",
    "titles",
    "keywords",
    "themes",
    "journals",
]


class FacultySerializer(serializers.ModelSerializer):
    class Meta:
        model = Faculty
        fields = FACULTY_API_FIELDS
        read_only_fields = ["user"]


class FacultyProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = Faculty
        fields = FACULTY_API_FIELDS
        read_only_fields = ["user", "faculty_id", "created_at", "updated_at"]

PAPER_API_FIELDS = [
    "id",
    "doi",
    "title",
    "abstract",
    "journal",
    "date_published",
    "download_url",
    "license_url",
    "ai_keywords",
    "faculty_keywords",
    "authors",
    "tc_count",
    "date_published_online",
    "date_published_print",
    "url",
    "keywords",
    "themes",
    "year",
    "status",
]


class PaperSerializer(serializers.ModelSerializer):
    doi = serializers.CharField(required=False, allow_blank=True)
    authors = serializers.CharField(write_only=True, required=False, allow_blank=True)
    year = serializers.SerializerMethodField()
    year_input = serializers.CharField(write_only=True, required=False, allow_blank=True, source="year")
    status = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = Paper
        fields = PAPER_API_FIELDS + ["year_input"]
        read_only_fields = ("authors",)

    def get_year(self, obj):
        dp = obj.date_published
        if not dp:
            return None
        if isinstance(dp, str):
            # "YYYY-MM-DD" string — just take the first 4 chars
            try:
                return int(dp[:4])
            except (ValueError, IndexError):
                return None
        return dp.year  # proper date object

    def create(self, validated_data):
        validated_data.pop("authors", None)
        year = validated_data.pop("year", None)  # comes in as year_input via source="year"

        if not validated_data.get("doi"):
            validated_data["doi"] = f"manual:{uuid.uuid4()}"

        if year and not validated_data.get("date_published"):
            try:
                parsed_year = int(str(year))
                validated_data["date_published"] = f"{parsed_year}-01-01"
            except (TypeError, ValueError):
                pass

        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop("authors", None)
        year = validated_data.pop("year", None)

        if year:
            try:
                parsed_year = int(str(year))
                validated_data["date_published"] = f"{parsed_year}-01-01"
            except (TypeError, ValueError):
                pass

        return super().update(instance, validated_data)

class ProjectSerializer(serializers.ModelSerializer):
    start_date = EmptyStringToNoneDateField(required=False, allow_null=True)
    end_date = EmptyStringToNoneDateField(required=False, allow_null=True)
    collaborators = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        allow_empty=True,
        write_only=True,
    )
    funding_amount = serializers.CharField(
        required=False, allow_blank=True, write_only=True
    )
    outcomes = serializers.CharField(required=False, allow_blank=True, write_only=True)

    class Meta:
        model = Project
        fields = '__all__'
        read_only_fields = ('faculty',)

    def create(self, validated_data):
        validated_data.pop("collaborators", None)
        validated_data.pop("funding_amount", None)
        validated_data.pop("outcomes", None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop("collaborators", None)
        validated_data.pop("funding_amount", None)
        validated_data.pop("outcomes", None)
        return super().update(instance, validated_data)

class PatentSerializer(serializers.ModelSerializer):
    patent_number = serializers.CharField(required=False, allow_blank=True)
    filing_date = EmptyStringToNoneDateField(required=False, allow_null=True)
    issue_date = EmptyStringToNoneDateField(required=False, allow_null=True)
    status = serializers.CharField(required=False, allow_blank=True, write_only=True)
    inventors = serializers.CharField(required=False, allow_blank=True, write_only=True)
    application_number = serializers.CharField(
        required=False, allow_blank=True, write_only=True
    )
    assignee = serializers.CharField(required=False, allow_blank=True, write_only=True)
    keywords = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        allow_empty=True,
        write_only=True,
    )

    class Meta:
        model = Patent
        fields = '__all__'
        read_only_fields = ('faculty',)

    def create(self, validated_data):
        validated_data.pop("status", None)
        validated_data.pop("inventors", None)
        validated_data.pop("application_number", None)
        validated_data.pop("assignee", None)
        validated_data.pop("keywords", None)

        if not validated_data.get("patent_number"):
            validated_data["patent_number"] = f"PN-{uuid.uuid4().hex[:12].upper()}"

        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop("status", None)
        validated_data.pop("inventors", None)
        validated_data.pop("application_number", None)
        validated_data.pop("assignee", None)
        validated_data.pop("keywords", None)
        return super().update(instance, validated_data)

class ContactTeamMemberSerializer(serializers.ModelSerializer):
    photo = serializers.SerializerMethodField()

    class Meta:
        model = ContactTeamMember
        fields = "__all__"

    def get_photo(self, obj):
        if not obj.photo:
            return None
        request = self.context.get("request")
        if request:
            return request.build_absolute_uri(obj.photo.url)
        return obj.photo.url


class ContactPageSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContactPageSettings
        fields = "__all__"