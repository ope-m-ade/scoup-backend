from django.contrib import admin

from .models import Department, Faculty, Paper, Patent, Project, School, ContactTeamMember, ContactPageSettings


@admin.register(School)
class SchoolAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "is_active", "display_order")
    list_filter = ("is_active",)
    search_fields = ("name", "code")
    ordering = ("display_order", "name")


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("name", "school", "code", "is_active")
    list_filter = ("is_active", "school")
    search_fields = ("name", "code", "school__name")
    autocomplete_fields = ("school",)


@admin.register(Faculty)
class FacultyAdmin(admin.ModelAdmin):
    list_display = (
        "display_name",
        "email",
        "primary_department",
        "primary_school",
        "review_status",
        "confirmed_su_faculty",
        "profile_visibility",
        "is_approved",
    )
    list_filter = (
        "review_status",
        "confirmed_su_faculty",
        "profile_visibility",
        "is_approved",
        "primary_school",
        "primary_department",
    )
    search_fields = (
        "name",
        "first_name",
        "last_name",
        "email",
        "department",
        "school",
        "faculty_id",
    )
    autocomplete_fields = ("primary_school", "primary_department", "schools", "departments")
    filter_horizontal = ("schools", "departments")
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (
            "Identity",
            {
                "fields": (
                    "user",
                    "faculty_id",
                    "name",
                    "first_name",
                    "last_name",
                    "email",
                    "title",
                    "photo",
                )
            },
        ),
        (
            "Clean SU affiliation",
            {
                "fields": (
                    "review_status",
                    "confirmed_su_faculty",
                    "primary_school",
                    "primary_department",
                    "schools",
                    "departments",
                    "cleanup_notes",
                )
            },
        ),
        (
            "Legacy/imported affiliation",
            {
                "classes": ("collapse",),
                "fields": (
                    "school",
                    "department",
                    "school_affiliations",
                    "department_affiliations",
                ),
            },
        ),
        (
            "Profile",
            {
                "fields": (
                    "office",
                    "room",
                    "phone",
                    "bio",
                    "faculty_keywords",
                    "ai_keywords",
                    "keywords",
                    "profile_visibility",
                    "is_approved",
                )
            },
        ),
        (
            "Metrics and imported evidence",
            {
                "classes": ("collapse",),
                "fields": (
                    "total_citations",
                    "article_count",
                    "average_citations",
                    "dois",
                    "titles",
                    "themes",
                    "journals",
                ),
            },
        ),
        ("Timestamps", {"classes": ("collapse",), "fields": ("created_at", "updated_at")}),
    )
    actions = (
        "mark_confirmed_su",
        "mark_pending_review",
        "mark_external",
        "archive_profiles",
    )

    @admin.display(description="Name")
    def display_name(self, obj):
        return str(obj)

    @admin.action(description="Mark selected faculty as confirmed SU faculty")
    def mark_confirmed_su(self, request, queryset):
        queryset.update(
            review_status="confirmed_su",
            confirmed_su_faculty=True,
            is_approved=True,
            profile_visibility=True,
        )

    @admin.action(description="Mark selected faculty as pending review")
    def mark_pending_review(self, request, queryset):
        queryset.update(review_status="pending", confirmed_su_faculty=False)

    @admin.action(description="Mark selected faculty as external collaborators")
    def mark_external(self, request, queryset):
        queryset.update(
            review_status="external",
            confirmed_su_faculty=False,
            profile_visibility=False,
        )

    @admin.action(description="Archive selected faculty profiles")
    def archive_profiles(self, request, queryset):
        queryset.update(
            review_status="archived",
            confirmed_su_faculty=False,
            profile_visibility=False,
            is_approved=False,
        )


@admin.register(Paper)
class PaperAdmin(admin.ModelAdmin):
    list_display = ("title", "journal", "date_published", "tc_count")
    search_fields = ("title", "doi", "journal", "authors__name", "authors__email")
    filter_horizontal = ("authors",)
    list_filter = ("journal",)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("title", "status", "start_date", "end_date")
    search_fields = ("title", "description", "faculty__name", "faculty__email")
    filter_horizontal = ("faculty",)
    list_filter = ("status",)


@admin.register(Patent)
class PatentAdmin(admin.ModelAdmin):
    list_display = ("title", "patent_number", "issue_date")
    search_fields = ("title", "patent_number", "faculty__name", "faculty__email")
    filter_horizontal = ("faculty",)

@admin.register(ContactTeamMember)
class ContactTeamMemberAdmin(admin.ModelAdmin):
    list_display = ("name", "role", "email", "order", "is_visible")
    list_editable = ("order", "is_visible")
    search_fields = ("name", "role", "email")
    ordering = ("order", "name")


@admin.register(ContactPageSettings)
class ContactPageSettingsAdmin(admin.ModelAdmin):
    pass