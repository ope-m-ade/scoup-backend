from django.core.mail import send_mail
from django.db.models import Count, Q
from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..models import (
    AdminAuditLog,
    CollaborationInquiry,
    Department,
    Faculty,
    Paper,
    Patent,
    Project,
    School,
)
from ..serializers import (
    DepartmentSerializer,
    FacultySerializer,
    PaperSerializer,
    SchoolSerializer,
)
from .utils import _normalize_paper_link


# ---------------------------------------------------------------------------
# Permission helper
# ---------------------------------------------------------------------------

class IsAdminUser(IsAuthenticated):
    """Allows access only to staff/superuser accounts."""
    def has_permission(self, request, view):
        return bool(
            super().has_permission(request, view)
            and (request.user.is_staff or request.user.is_superuser)
        )


# ---------------------------------------------------------------------------
# Audit log helper
# ---------------------------------------------------------------------------

def _log_action(user, action, target_type="", target_id=None, target_name="", notes=""):
    display_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or user.get_username()
    AdminAuditLog.objects.create(
        admin_username=user.get_username(),
        admin_display_name=display_name,
        action=action,
        target_type=target_type,
        target_id=target_id,
        target_name=target_name,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Schools & Departments (class-based)
# ---------------------------------------------------------------------------

class AdminSchoolListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = SchoolSerializer
    queryset = School.objects.all().order_by("display_order", "name")


class AdminSchoolDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = SchoolSerializer
    queryset = School.objects.all()


class AdminDepartmentListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = DepartmentSerializer

    def get_queryset(self):
        qs = Department.objects.select_related("school").order_by("school__name", "name")
        school_id = self.request.query_params.get("school")
        if school_id:
            qs = qs.filter(school_id=school_id)
        return qs


class AdminDepartmentDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = DepartmentSerializer
    queryset = Department.objects.select_related("school")


# ---------------------------------------------------------------------------
# Faculty list
# ---------------------------------------------------------------------------

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_faculty_list(request):
    """
    GET /api/admin/faculty/
    Returns all faculty with full admin fields.

    Query params:
      ?search=<term>      — filter by name or email
      ?status=approved|pending|rejected|unverified|all
      ?department=<name>  — filter by department name (case-insensitive)
      ?missing=true       — only faculty with no primary department
    """
    if not (request.user.is_staff or request.user.is_superuser):
        return Response({"detail": "Admin privileges required."}, status=403)

    qs = Faculty.objects.prefetch_related(
        "departments", "schools"
    ).select_related("primary_department", "primary_school").order_by("last_name", "first_name", "name")

    # Text search
    search = request.query_params.get("search", "").strip()
    if search:
        qs = qs.filter(
            Q(name__icontains=search)
            | Q(first_name__icontains=search)
            | Q(last_name__icontains=search)
            | Q(email__icontains=search)
        )

    # Status filter
    status_filter = request.query_params.get("status", "all").lower()
    if status_filter == "approved":
        qs = qs.filter(is_approved=True)
    elif status_filter == "pending":
        qs = qs.filter(institutional_email_verified=True, is_approved=False)
    elif status_filter == "rejected":
        qs = qs.filter(review_status="rejected")
    elif status_filter == "unverified":
        qs = qs.filter(user__isnull=False, institutional_email_verified=False)

    # Legacy: ?pending=true
    if request.query_params.get("pending", "").lower() == "true":
        qs = qs.filter(user__isnull=False, institutional_email_verified=True, is_approved=False)

    # Department filter
    dept_filter = request.query_params.get("department", "").strip()
    if dept_filter:
        qs = qs.filter(
            Q(primary_department__name__iexact=dept_filter)
            | Q(departments__name__iexact=dept_filter)
        ).distinct()

    # Missing department
    if request.query_params.get("missing", "").lower() == "true":
        qs = qs.filter(primary_department__isnull=True)

    results = []
    for f in qs:
        departments = [d.name for d in f.departments.all()]
        schools = [s.name for s in f.schools.all()]

        photo_url = None
        if f.photo:
            try:
                photo_url = request.build_absolute_uri(f.photo.url)
            except Exception:
                pass

        results.append({
            "id": f.id,
            "name": f.name or f"{f.first_name or ''} {f.last_name or ''}".strip() or f.faculty_id,
            "first_name": f.first_name or "",
            "last_name": f.last_name or "",
            "title": f.title or "",
            "email": f.email or "",
            "institutional_email": f.institutional_email or "",
            "institutional_email_verified": f.institutional_email_verified,
            "is_approved": f.is_approved,
            "profile_visibility": f.profile_visibility,
            "review_status": f.review_status,
            "confirmed_su_faculty": f.confirmed_su_faculty,
            "article_count": f.article_count,
            "total_citations": f.total_citations,
            "photo": photo_url,
            "primary_department": {
                "id": f.primary_department.id,
                "name": f.primary_department.name,
            } if f.primary_department else None,
            "primary_school": {
                "id": f.primary_school.id,
                "name": f.primary_school.name,
            } if f.primary_school else None,
            "departments": departments,
            "schools": schools,
            "missing_data": not f.primary_department_id,
            "has_user": f.user_id is not None,
        })

    return Response(results)


# ---------------------------------------------------------------------------
# Faculty detail (PATCH / DELETE)
# ---------------------------------------------------------------------------

@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def admin_faculty_detail(request, pk):
    if not (request.user.is_staff or request.user.is_superuser):
        return Response({"detail": "Admin privileges required."}, status=403)
    try:
        faculty = Faculty.objects.get(pk=pk)
    except Faculty.DoesNotExist:
        return Response({"detail": "Faculty not found."}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "DELETE":
        faculty_name = faculty.name or f"{faculty.first_name or ''} {faculty.last_name or ''}".strip()
        faculty.delete()
        _log_action(request.user, AdminAuditLog.ACTION_DELETE_FACULTY, "faculty", pk, faculty_name)
        return Response({"detail": "Faculty record deleted."}, status=status.HTTP_204_NO_CONTENT)

    data = request.data

    if "primary_department" in data:
        dept_id = data["primary_department"]
        if dept_id is None:
            faculty.primary_department = None
        else:
            try:
                faculty.primary_department = Department.objects.get(pk=dept_id)
            except Department.DoesNotExist:
                return Response({"detail": f"Department {dept_id} not found."}, status=400)

    if "primary_school" in data:
        school_id = data["primary_school"]
        if school_id is None:
            faculty.primary_school = None
        else:
            try:
                faculty.primary_school = School.objects.get(pk=school_id)
            except School.DoesNotExist:
                return Response({"detail": f"School {school_id} not found."}, status=400)

    if "review_status" in data:
        faculty.review_status = data["review_status"]

    if "confirmed_su_faculty" in data:
        faculty.confirmed_su_faculty = bool(data["confirmed_su_faculty"])

    if "profile_visibility" in data:
        faculty.profile_visibility = bool(data["profile_visibility"])
        faculty_name = faculty.name or f"{faculty.first_name or ''} {faculty.last_name or ''}".strip()
        _log_action(
            request.user,
            AdminAuditLog.ACTION_TOGGLE_VISIBILITY,
            "faculty",
            faculty.id,
            faculty_name,
            f"visibility set to {faculty.profile_visibility}",
        )

    if "is_approved" in data:
        faculty.is_approved = bool(data["is_approved"])

    faculty.save()

    if "departments" in data:
        faculty.departments.set(data["departments"])

    if "schools" in data:
        faculty.schools.set(data["schools"])

    return Response({
        "id": faculty.id,
        "is_approved": faculty.is_approved,
        "profile_visibility": faculty.profile_visibility,
        "primary_department": faculty.primary_department.name if faculty.primary_department else None,
        "primary_school": faculty.primary_school.name if faculty.primary_school else None,
        "review_status": faculty.review_status,
        "confirmed_su_faculty": faculty.confirmed_su_faculty,
    })


# ---------------------------------------------------------------------------
# Faculty approve / reject (individual)
# ---------------------------------------------------------------------------

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_approve_faculty(request, pk):
    if not (request.user.is_staff or request.user.is_superuser):
        return Response({"detail": "Admin privileges required."}, status=403)
    try:
        faculty = Faculty.objects.get(pk=pk)
    except Faculty.DoesNotExist:
        return Response({"detail": "Faculty not found."}, status=status.HTTP_404_NOT_FOUND)

    faculty.is_approved = True
    faculty.save(update_fields=["is_approved", "updated_at"])

    faculty_name = faculty.name or f"{faculty.first_name or ''} {faculty.last_name or ''}".strip() or "Faculty"
    _log_action(request.user, AdminAuditLog.ACTION_APPROVE_FACULTY, "faculty", faculty.id, faculty_name)

    recipient = faculty.institutional_email or faculty.email
    if recipient:
        try:
            send_mail(
                subject="SCOUP — Your Profile Has Been Approved!",
                message=(
                    f"Hi {faculty_name},\n\n"
                    f"Great news! Your SCOUP profile has been reviewed and approved.\n\n"
                    f"You are now visible in SCOUP's faculty search and collaboration network.\n\n"
                    f"Log in to your dashboard to complete your profile and start connecting.\n\n"
                    f"— The SCOUP Team"
                ),
                from_email=None,
                recipient_list=[recipient],
                fail_silently=True,
            )
        except Exception:
            pass

    return Response({"detail": "Faculty approved and notified."})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_reject_faculty(request, pk):
    if not (request.user.is_staff or request.user.is_superuser):
        return Response({"detail": "Admin privileges required."}, status=403)
    try:
        faculty = Faculty.objects.get(pk=pk)
    except Faculty.DoesNotExist:
        return Response({"detail": "Faculty not found."}, status=status.HTTP_404_NOT_FOUND)

    reason = (request.data.get("reason") or "").strip()
    faculty.is_approved = False
    faculty.institutional_email_verified = False
    faculty.review_status = "rejected"
    faculty.save(update_fields=["is_approved", "institutional_email_verified", "review_status", "updated_at"])

    faculty_name = faculty.name or f"{faculty.first_name or ''} {faculty.last_name or ''}".strip() or "Faculty"
    _log_action(request.user, AdminAuditLog.ACTION_REJECT_FACULTY, "faculty", faculty.id, faculty_name, reason)

    recipient = faculty.institutional_email or faculty.email
    if recipient:
        reason_line = f"\nReason: {reason}\n" if reason else ""
        try:
            send_mail(
                subject="SCOUP — Profile Verification Update",
                message=(
                    f"Hi {faculty_name},\n\n"
                    f"We were unable to verify your SCOUP faculty profile at this time.{reason_line}\n"
                    f"If you believe this is an error or have questions, please contact us at scoupteam@gmail.com.\n\n"
                    f"— The SCOUP Team"
                ),
                from_email=None,
                recipient_list=[recipient],
                fail_silently=True,
            )
        except Exception:
            pass

    return Response({"detail": "Faculty rejected and notified."})


# ---------------------------------------------------------------------------
# Bulk faculty action
# ---------------------------------------------------------------------------

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_bulk_faculty_action(request):
    """
    POST /api/admin/faculty/bulk-action/
    Body: { "action": "approve"|"reject", "ids": [1,2,3], "reason": "..." }
    """
    if not (request.user.is_staff or request.user.is_superuser):
        return Response({"detail": "Admin privileges required."}, status=403)

    action = (request.data.get("action") or "").strip()
    ids = request.data.get("ids") or []
    reason = (request.data.get("reason") or "").strip()

    if action not in ("approve", "reject"):
        return Response({"detail": "action must be 'approve' or 'reject'."}, status=400)
    if not ids:
        return Response({"detail": "ids list is required."}, status=400)

    qs = Faculty.objects.filter(id__in=ids)
    count = qs.count()

    if action == "approve":
        qs.update(is_approved=True)
        _log_action(
            request.user,
            AdminAuditLog.ACTION_BULK_APPROVE,
            "faculty",
            None,
            f"{count} faculty members",
        )
    else:
        qs.update(is_approved=False, institutional_email_verified=False, review_status="rejected")
        _log_action(
            request.user,
            AdminAuditLog.ACTION_BULK_REJECT,
            "faculty",
            None,
            f"{count} faculty members",
            reason,
        )

    return Response({"detail": f"Successfully {action}d {count} faculty member(s)."})


# ---------------------------------------------------------------------------
# Platform stats
# ---------------------------------------------------------------------------

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_stats(request):
    """GET /api/admin/stats/"""
    if not (request.user.is_staff or request.user.is_superuser):
        return Response({"detail": "Admin privileges required."}, status=403)

    total_faculty = Faculty.objects.count()
    approved_faculty = Faculty.objects.filter(is_approved=True).count()
    pending_faculty = Faculty.objects.filter(
        institutional_email_verified=True, is_approved=False
    ).exclude(review_status="rejected").count()
    rejected_faculty = Faculty.objects.filter(review_status="rejected").count()
    unverified_faculty = Faculty.objects.filter(
        user__isnull=False, institutional_email_verified=False
    ).count()
    hidden_faculty = Faculty.objects.filter(is_approved=True, profile_visibility=False).count()

    total_inquiries = CollaborationInquiry.objects.count()
    new_inquiries = CollaborationInquiry.objects.filter(status="pending").count()
    reviewed_inquiries = CollaborationInquiry.objects.filter(status="reviewed").count()
    closed_inquiries = CollaborationInquiry.objects.filter(status="closed").count()
    faculty_inquiries = CollaborationInquiry.objects.filter(source_type="faculty").count()
    external_inquiries = CollaborationInquiry.objects.filter(source_type="external").count()

    total_papers = Paper.objects.count()
    total_patents = Patent.objects.count()
    total_projects = Project.objects.count()

    dept_breakdown = list(
        Department.objects.annotate(
            fac_count=Count("primary_faculty", distinct=True)
        )
        .filter(fac_count__gt=0)
        .order_by("-fac_count")
        .values("name", "fac_count")[:12]
    )

    return Response({
        "faculty": {
            "total": total_faculty,
            "approved": approved_faculty,
            "pending": pending_faculty,
            "rejected": rejected_faculty,
            "unverified": unverified_faculty,
            "hidden": hidden_faculty,
        },
        "inquiries": {
            "total": total_inquiries,
            "new": new_inquiries,
            "reviewed": reviewed_inquiries,
            "closed": closed_inquiries,
            "from_faculty": faculty_inquiries,
            "from_external": external_inquiries,
        },
        "content": {
            "papers": total_papers,
            "patents": total_patents,
            "projects": total_projects,
        },
        "department_breakdown": [
            {"name": d["name"], "faculty_count": d["fac_count"]}
            for d in dept_breakdown
        ],
    })


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_audit_log(request):
    """GET /api/admin/audit-log/ — returns the 100 most recent admin actions."""
    if not (request.user.is_staff or request.user.is_superuser):
        return Response({"detail": "Admin privileges required."}, status=403)

    logs = AdminAuditLog.objects.all()[:100]
    data = [
        {
            "id": log.id,
            "admin": log.admin_display_name or log.admin_username,
            "action": log.get_action_display(),
            "action_key": log.action,
            "target_type": log.target_type,
            "target_id": log.target_id,
            "target_name": log.target_name,
            "notes": log.notes,
            "created_at": log.created_at.isoformat(),
        }
        for log in logs
    ]
    return Response({"count": len(data), "results": data})


# ---------------------------------------------------------------------------
# Papers, Projects, Patents admin (unchanged)
# ---------------------------------------------------------------------------

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_paper_list(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return Response({"detail": "Admin privileges required."}, status=403)
    qs = Paper.objects.prefetch_related("authors").order_by("-id")

    search = request.query_params.get("search", "").strip()
    if search:
        qs = qs.filter(Q(title__icontains=search) | Q(doi__icontains=search))

    unlinked_only = request.query_params.get("unlinked", "").lower() == "true"
    if unlinked_only:
        qs = qs.filter(authors__isnull=True)

    results = []
    for p in qs:
        authors = list(p.authors.all())
        results.append({
            "id": p.id,
            "title": p.title,
            "doi": p.doi or "",
            "journal": p.journal or "",
            "year": p.date_published.year if p.date_published else None,
            "link": _normalize_paper_link(p.download_url, p.url, p.license_url, p.doi),
            "abstract": (p.abstract or "")[:300],
            "author_count": len(authors),
            "authors": [
                a.name or f"{a.first_name or ''} {a.last_name or ''}".strip()
                for a in authors
            ],
            "missing_data": len(authors) == 0 or not p.abstract,
        })

    return Response({"count": len(results), "results": results})


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def admin_paper_detail(request, pk):
    if not (request.user.is_staff or request.user.is_superuser):
        return Response({"detail": "Admin privileges required."}, status=403)
    try:
        paper = Paper.objects.get(pk=pk)
    except Paper.DoesNotExist:
        return Response({"detail": "Paper not found."}, status=status.HTTP_404_NOT_FOUND)
    paper.delete()
    return Response({"detail": "Paper deleted."}, status=status.HTTP_204_NO_CONTENT)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def admin_project_detail(request, pk):
    if not (request.user.is_staff or request.user.is_superuser):
        return Response({"detail": "Admin privileges required."}, status=403)
    try:
        project = Project.objects.get(pk=pk)
    except Project.DoesNotExist:
        return Response({"detail": "Project not found."}, status=status.HTTP_404_NOT_FOUND)
    project.delete()
    return Response({"detail": "Project deleted."}, status=status.HTTP_204_NO_CONTENT)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def admin_patent_detail(request, pk):
    if not (request.user.is_staff or request.user.is_superuser):
        return Response({"detail": "Admin privileges required."}, status=403)
    try:
        patent = Patent.objects.get(pk=pk)
    except Patent.DoesNotExist:
        return Response({"detail": "Patent not found."}, status=status.HTTP_404_NOT_FOUND)
    patent.delete()
    return Response({"detail": "Patent deleted."}, status=status.HTTP_204_NO_CONTENT)
