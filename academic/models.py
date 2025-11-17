
# academic/models.py
from django.db import models
from django.contrib.auth.models import User

class Faculty(models.Model):
    # Existing fields (keep yours as-is)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="faculty_profile", null=True, blank=True)
    faculty_id = models.CharField(max_length=100, unique=True)  # we’ll map AcademicMetrics _id here
    first_name = models.CharField(max_length=100, blank=True, null=True)
    last_name  = models.CharField(max_length=100, blank=True, null=True)
    title      = models.CharField(max_length=150, blank=True, null=True)
    department = models.CharField(max_length=150, blank=True, null=True)
    email      = models.EmailField(unique=True, blank=True, null=True)
    office     = models.CharField(max_length=150, blank=True, null=True)
    room       = models.CharField(max_length=100, blank=True, null=True)
    phone      = models.CharField(max_length=20, blank=True, null=True)
    bio        = models.TextField(blank=True, null=True)
    faculty_keywords = models.TextField(blank=True, null=True)
    ai_keywords      = models.TextField(blank=True, null=True)
    profile_visibility = models.BooleanField(default=True)
    is_approved        = models.BooleanField(default=False)
    photo = models.ImageField(upload_to="faculty_photos/", blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # NEW rich fields from AcademicMetrics
    name = models.CharField(max_length=255, blank=True, null=True)  # AcademicMetrics “name”
    total_citations   = models.IntegerField(default=0)
    article_count     = models.IntegerField(default=0)
    average_citations = models.FloatField(default=0.0)

    department_affiliations = models.JSONField(default=list, blank=True)  # list[str]
    dois     = models.JSONField(default=list, blank=True)                 # list[str]
    titles   = models.JSONField(default=list, blank=True)                 # list[str]


    # Raw categories + merged flat keywords for search
    categories           = models.JSONField(default=list, blank=True)  # the raw labels
    keywords             = models.JSONField(default=list, blank=True)  # merged top/mid/low

    def __str__(self):
        return self.name or f"{(self.first_name or '').strip()} {(self.last_name or '').strip()}".strip() or self.faculty_id


class Paper(models.Model):
    # Existing core fields
    doi = models.CharField(max_length=255, unique=True)
    title = models.CharField(max_length=500)
    abstract = models.TextField(blank=True, null=True)
    journal = models.CharField(max_length=255, blank=True, null=True)
    date_published = models.DateField(blank=True, null=True)  # keep for back-compat
    download_url = models.URLField(blank=True, null=True)
    license_url = models.URLField(blank=True, null=True)
    ai_keywords = models.JSONField(blank=True, null=True)
    faculty_keywords = models.JSONField(blank=True, null=True)
    authors = models.ManyToManyField('Faculty', blank=True, related_name='papers')  # we’ll use this link

    # NEW rich paper fields
    tc_count = models.IntegerField(default=0)
    date_published_online = models.DateField(blank=True, null=True)
    date_published_print  = models.DateField(blank=True, null=True)
    url = models.URLField(blank=True, null=True)  # crossref landing URL
    keywords = models.JSONField(default=list, blank=True)  # merged categories
    themes   = models.JSONField(default=list, blank=True)  # AcademicMetrics “themes”

    def __str__(self):
        return self.title

class Project(models.Model):
    title = models.CharField(max_length=300)
    description = models.TextField(blank=True, null=True)
    start_date = models.DateField(blank=True, null=True)
    end_date = models.DateField(blank=True, null=True)
    faculty = models.ManyToManyField('Faculty', related_name='projects', blank=True)
    funding_source = models.CharField(max_length=200, blank=True, null=True)
    status = models.CharField(max_length=100, blank=True, null=True)
    keywords = models.JSONField(blank=True, null=True)
    link = models.URLField(blank=True, null=True)

    def __str__(self):
        return self.title

class Patent(models.Model):
    title = models.CharField(max_length=300)
    abstract = models.TextField(blank=True, null=True)
    patent_number = models.CharField(max_length=100, unique=True)
    filing_date = models.DateField(blank=True, null=True)
    issue_date = models.DateField(blank=True, null=True)
    faculty = models.ManyToManyField('Faculty', related_name='patents', blank=True)
    link = models.URLField(blank=True, null=True)
    aiKeywords = models.JSONField(blank=True, null=True)

    def __str__(self):
        return self.title

class PaperAuthorship(models.Model):
    STATUS_CHOICES = [
        ('pending',  'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]
    paper   = models.ForeignKey(Paper, on_delete=models.CASCADE, related_name='authorships')
    faculty = models.ForeignKey(Faculty, on_delete=models.CASCADE, related_name='authorships')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    decided_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        unique_together = ('paper', 'faculty')

    def __str__(self):
        return f"{self.faculty} - {self.paper.title} ({self.status})"