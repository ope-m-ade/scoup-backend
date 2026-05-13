
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models


class School(models.Model):
    name = models.CharField(max_length=255, unique=True)
    code = models.CharField(max_length=32, unique=True, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_order", "name"]

    def __str__(self):
        return self.name


class Department(models.Model):
    school = models.ForeignKey(
        School,
        on_delete=models.PROTECT,
        related_name="departments",
        blank=True,
        null=True,
    )
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=32, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["school__display_order", "school__name", "name"]
        unique_together = ("school", "name")

    def __str__(self):
        if self.school:
            return f"{self.name} ({self.school.name})"
        return self.name


class Faculty(models.Model):
    REVIEW_STATUS_CHOICES = [
        ("pending", "Pending Review"),
        ("confirmed_su", "Confirmed SU Faculty"),
        ("external", "External Collaborator"),
        ("archived", "Archived"),
        ("rejected", "Rejected"),
    ]

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="faculty_profile",
        null=True,
        blank=True,
    )
    faculty_id = models.CharField(max_length=100, unique=True)
    first_name = models.CharField(max_length=100, blank=True, null=True)
    last_name = models.CharField(max_length=100, blank=True, null=True)
    title = models.CharField(max_length=255, blank=True, null=True)
    department = models.CharField(max_length=255, blank=True, null=True)
    email = models.EmailField(unique=True, blank=True, null=True)
    office = models.CharField(max_length=255, blank=True, null=True)
    room = models.CharField(max_length=100, blank=True, null=True)
    phone = models.CharField(max_length=20, blank=True, null=True)
    bio = models.TextField(blank=True, null=True)
    research_interests = models.TextField(blank=True, null=True)
    qualifications = models.JSONField(default=list, blank=True)  # [{degree, institution, year}]
    personal_website = models.URLField(max_length=500, blank=True, null=True)
    faculty_keywords = models.TextField(blank=True, null=True)
    ai_keywords = models.TextField(blank=True, null=True)
    profile_visibility = models.BooleanField(default=True)
    is_approved = models.BooleanField(default=False)
    institutional_email = models.EmailField(blank=True, null=True)
    institutional_email_verified = models.BooleanField(default=False)
    photo = models.ImageField(upload_to="faculty_photos/", blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    name = models.CharField(max_length=255, blank=True, null=True)
    total_citations = models.IntegerField(default=0)
    article_count = models.IntegerField(default=0)
    average_citations = models.FloatField(default=0.0)

    primary_school = models.ForeignKey(
        School,
        on_delete=models.SET_NULL,
        related_name="primary_faculty",
        blank=True,
        null=True,
    )
    primary_department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        related_name="primary_faculty",
        blank=True,
        null=True,
    )
    schools = models.ManyToManyField(School, related_name="faculty", blank=True)
    departments = models.ManyToManyField(Department, related_name="faculty", blank=True)
    review_status = models.CharField(
        max_length=24,
        choices=REVIEW_STATUS_CHOICES,
        default="pending",
        db_index=True,
    )
    confirmed_su_faculty = models.BooleanField(default=False, db_index=True)
    cleanup_notes = models.TextField(blank=True, null=True)

    school = models.CharField(max_length=255, blank=True, null=True)
    school_affiliations = models.JSONField(default=list, blank=True)
    department_affiliations = models.JSONField(default=list, blank=True)
    dois = models.JSONField(default=list, blank=True)
    titles = models.JSONField(default=list, blank=True)
    keywords = models.JSONField(default=list, blank=True)
    themes = models.JSONField(default=list, blank=True)
    journals = models.JSONField(default=list, blank=True)

    def __str__(self):
        return (
            self.name
            or f"{(self.first_name or '').strip()} {(self.last_name or '').strip()}".strip()
            or self.faculty_id
        )

    def clean(self):
        super().clean()
        if (
            self.primary_department_id
            and self.primary_school_id
            and self.primary_department.school_id
            and self.primary_department.school_id != self.primary_school_id
        ):
            raise ValidationError(
                {
                    "primary_department": (
                        "Primary department must belong to the selected primary school."
                    )
                }
            )


class Paper(models.Model):
    doi = models.CharField(max_length=255, unique=True)
    title = models.CharField(max_length=500)
    abstract = models.TextField(blank=True, null=True)
    journal = models.CharField(max_length=255, blank=True, null=True)
    date_published = models.DateField(blank=True, null=True)
    download_url = models.URLField(blank=True, null=True)
    license_url = models.URLField(blank=True, null=True)
    ai_keywords = models.JSONField(blank=True, null=True)
    faculty_keywords = models.JSONField(blank=True, null=True)
    authors = models.ManyToManyField("Faculty", blank=True, related_name="papers")
    tc_count = models.IntegerField(default=0)
    date_published_online = models.DateField(blank=True, null=True)
    date_published_print = models.DateField(blank=True, null=True)
    url = models.URLField(blank=True, null=True)
    keywords = models.JSONField(default=list, blank=True)
    themes = models.JSONField(default=list, blank=True)
    top_level_categories = models.JSONField(default=list, blank=True)
    mid_level_categories = models.JSONField(default=list, blank=True)
    faculty_members = models.JSONField(default=list, blank=True)
    faculty_affiliations = models.JSONField(default=dict, blank=True)
    engagement_metrics = models.JSONField(default=dict, blank=True)
    paper_embedding = models.JSONField(default=list, blank=True)
    embedding_model = models.CharField(max_length=64, default="", blank=True)
    embedding_updated_at = models.DateTimeField(null=True, blank=True)

    STATUS_DRAFT = "draft"
    STATUS_PUBLISHED = "published"
    STATUS_IN_REVIEW = "in-review"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_PUBLISHED, "Published"),
        (STATUS_IN_REVIEW, "In Review"),
    ]
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PUBLISHED,  # existing imported papers stay published
    )

    def __str__(self):
        return self.title


class Project(models.Model):
    title = models.CharField(max_length=300)
    description = models.TextField(blank=True, null=True)
    start_date = models.DateField(blank=True, null=True)
    end_date = models.DateField(blank=True, null=True)
    faculty = models.ManyToManyField("Faculty", related_name="projects", blank=True)
    funding_source = models.CharField(max_length=200, blank=True, null=True)
    status = models.CharField(max_length=100, blank=True, null=True)
    keywords = models.JSONField(blank=True, null=True)
    link = models.URLField(blank=True, null=True)
    is_open_to_collaboration = models.BooleanField(default=False)
    collaboration_invitation = models.TextField(blank=True)
    allow_student_interest = models.BooleanField(default=True)

    def __str__(self):
        return self.title

class Patent(models.Model):
    title = models.CharField(max_length=300)
    abstract = models.TextField(blank=True, null=True)
    patent_number = models.CharField(max_length=100, unique=True)
    filing_date = models.DateField(blank=True, null=True)
    issue_date = models.DateField(blank=True, null=True)
    faculty = models.ManyToManyField("Faculty", related_name="patents", blank=True)
    link = models.URLField(blank=True, null=True)
    aiKeywords = models.JSONField(blank=True, null=True)

    def __str__(self):
        return self.title

class PaperAuthorship(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]
    paper = models.ForeignKey(Paper, on_delete=models.CASCADE, related_name="authorships")
    faculty = models.ForeignKey(
        Faculty, on_delete=models.CASCADE, related_name="authorships"
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    decided_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        unique_together = ("paper", "faculty")

    def __str__(self):
        return f"{self.faculty} - {self.paper.title} ({self.status})"


class FacultySuggestionDecision(models.Model):
    DECISION_CHOICES = [
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]

    reviewer = models.ForeignKey(
        Faculty, on_delete=models.CASCADE, related_name="suggestion_decisions"
    )
    external_faculty = models.ForeignKey(
        Faculty, on_delete=models.CASCADE, related_name="reviewed_by"
    )
    decision = models.CharField(max_length=16, choices=DECISION_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("reviewer", "external_faculty")

    def __str__(self):
        return f"{self.reviewer_id}->{self.external_faculty_id}:{self.decision}"

class ContactTeamMember(models.Model):
    name = models.CharField(max_length=200)
    role = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    email = models.EmailField(blank=True)
    linkedin_url = models.URLField(blank=True)
    photo = models.ImageField(upload_to="contact_photos/", blank=True, null=True)
    order = models.PositiveIntegerField(default=0)
    is_visible = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "name"]
        
    def __str__(self):
        return self.name
    
class ContactPageSettings(models.Model):
    general_email = models.EmailField(blank=True)
    support_email = models.EmailField(blank=True)
    github_url = models.URLField(blank=True)
    linkedin_url = models.URLField(blank=True)
    address_line_1 = models.CharField(max_length=200, blank=True)
    address_line_2 = models.CharField(max_length=200, blank=True)
    address_line_3 = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "Contact Page Settings"


class CollaborationInquiry(models.Model):
    STATUS_PENDING = "pending"
    STATUS_REVIEWED = "reviewed"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_REVIEWED, "Reviewed"),
        (STATUS_CLOSED, "Closed"),
    ]
    SOURCE_FACULTY = "faculty"
    SOURCE_EXTERNAL = "external"
    SOURCE_CHOICES = [
        (SOURCE_FACULTY, "Faculty (internal)"),
        (SOURCE_EXTERNAL, "External stakeholder"),
    ]

    # Who sent it — either a logged-in faculty or an anonymous external user
    from_faculty = models.ForeignKey(
        Faculty,
        on_delete=models.CASCADE,
        related_name="sent_collaboration_inquiries",
        null=True,
        blank=True,
    )
    # For external (unauthenticated) senders
    requester_name = models.CharField(max_length=255, blank=True)
    requester_email = models.EmailField(blank=True)
    requester_organization = models.CharField(max_length=255, blank=True)
    source_type = models.CharField(
        max_length=16, choices=SOURCE_CHOICES, default=SOURCE_FACULTY
    )

    target_faculty_name = models.CharField(max_length=255)
    target_faculty_id = models.CharField(max_length=64, blank=True)
    target_department = models.CharField(max_length=255, blank=True)
    target_school = models.CharField(max_length=255, blank=True)
    target_project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        related_name="collaboration_inquiries",
        null=True,
        blank=True,
    )
    target_project_title = models.CharField(max_length=300, blank=True)
    requester_role = models.CharField(max_length=64, blank=True)
    shared_keywords = models.JSONField(default=list, blank=True)
    note = models.TextField(blank=True)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    admin_notes = models.TextField(blank=True)
    reviewed_by = models.CharField(max_length=150, blank=True)  # admin display name at time of review
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        sender = self.requester_name or (str(self.from_faculty) if self.from_faculty else "unknown")
        return f"Inquiry: {sender} → {self.target_faculty_name} [{self.status}]"


class AdminAuditLog(models.Model):
    ACTION_APPROVE_FACULTY = "approve_faculty"
    ACTION_REJECT_FACULTY = "reject_faculty"
    ACTION_BULK_APPROVE = "bulk_approve"
    ACTION_BULK_REJECT = "bulk_reject"
    ACTION_TOGGLE_VISIBILITY = "toggle_visibility"
    ACTION_DELETE_FACULTY = "delete_faculty"
    ACTION_UPDATE_INQUIRY = "update_inquiry"

    ACTION_CHOICES = [
        (ACTION_APPROVE_FACULTY, "Approved Faculty"),
        (ACTION_REJECT_FACULTY, "Rejected Faculty"),
        (ACTION_BULK_APPROVE, "Bulk Approved Faculty"),
        (ACTION_BULK_REJECT, "Bulk Rejected Faculty"),
        (ACTION_TOGGLE_VISIBILITY, "Toggled Profile Visibility"),
        (ACTION_DELETE_FACULTY, "Deleted Faculty"),
        (ACTION_UPDATE_INQUIRY, "Updated Inquiry"),
    ]

    admin_username = models.CharField(max_length=150)
    admin_display_name = models.CharField(max_length=255, blank=True)
    action = models.CharField(max_length=32, choices=ACTION_CHOICES)
    target_type = models.CharField(max_length=32, blank=True)   # "faculty" | "inquiry"
    target_id = models.IntegerField(null=True, blank=True)
    target_name = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.admin_username} → {self.action} ({self.target_name})"
