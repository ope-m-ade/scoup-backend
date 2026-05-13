from django.core.cache import cache
from django.db.models import Q
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import AdminAuditLog, CollaborationInquiry, Faculty, Project


def _full_name(first_name, last_name, fallback=""):
    return f"{(first_name or '').strip()} {(last_name or '').strip()}".strip() or fallback


def _faculty_display_name(faculty):
    return _full_name(
        faculty.first_name,
        faculty.last_name,
        (faculty.name or "").strip() or faculty.faculty_id,
    )


def _inquiry_payload(inq):
    faculty = inq.from_faculty
    if faculty:
        sender_name = _faculty_display_name(faculty)
        sender_email = faculty.email or ""
        sender_dept = faculty.department or ""
    else:
        sender_name = inq.requester_name
        sender_email = inq.requester_email
        sender_dept = inq.requester_organization

    return {
        "id": inq.id,
        "status": inq.status,
        "source_type": inq.source_type,
        "requester_role": inq.requester_role,
        "created_at": inq.created_at.isoformat(),
        "from_faculty_name": sender_name,
        "from_faculty_email": sender_email,
        "from_faculty_department": sender_dept,
        "target_faculty_name": inq.target_faculty_name,
        "target_faculty_id": inq.target_faculty_id,
        "target_department": inq.target_department,
        "target_school": inq.target_school,
        "target_project_id": inq.target_project_id,
        "target_project_title": inq.target_project_title,
        "shared_keywords": inq.shared_keywords or [],
        "note": inq.note,
        "admin_notes": inq.admin_notes,
        "reviewed_by": inq.reviewed_by,
    }


@api_view(["POST"])
@permission_classes([AllowAny])
def submit_collaboration_inquiry(request):
    """
    Submit a collaboration inquiry.
    - Authenticated faculty: source_type='faculty', linked via from_faculty FK.
    - Unauthenticated external users: source_type='external', require requester_name + requester_email.

    Rate limit: max 5 submissions per IP per hour for unauthenticated requests.
    """
    if not (request.user and request.user.is_authenticated):
        ip = (
            request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
            or request.META.get("REMOTE_ADDR", "unknown")
        )
        rate_key = f"inquiry_rate_{ip}"
        submission_count = cache.get(rate_key, 0)
        if submission_count >= 5:
            return Response(
                {"detail": "Too many inquiry submissions. Please try again later."},
                status=429,
            )
        cache.set(rate_key, submission_count + 1, timeout=3600)

    target_name = (request.data.get("target_faculty_name") or "").strip()
    target_project = None
    target_project_id = request.data.get("target_project_id")
    if target_project_id:
        target_project = (
            Project.objects.filter(pk=target_project_id, is_open_to_collaboration=True)
            .prefetch_related("faculty")
            .first()
        )
        if not target_project:
            return Response({"detail": "This project is not accepting collaboration inquiries."}, status=400)
        if not target_name:
            lead = target_project.faculty.first()
            target_name = _faculty_display_name(lead) if lead else "Project team"

    if not target_name:
        return Response({"detail": "target_faculty_name is required."}, status=400)

    is_authenticated = request.user and request.user.is_authenticated

    if is_authenticated:
        faculty = Faculty.objects.filter(user=request.user).first()
        inquiry = CollaborationInquiry.objects.create(
            from_faculty=faculty,
            source_type=CollaborationInquiry.SOURCE_FACULTY,
            target_faculty_name=target_name,
            target_faculty_id=str(request.data.get("target_faculty_id", "")).strip(),
            target_department=str(request.data.get("target_department", "")).strip(),
            target_school=str(request.data.get("target_school", "")).strip(),
            target_project=target_project,
            target_project_title=(
                str(request.data.get("target_project_title", "")).strip()
                or (target_project.title if target_project else "")
            ),
            requester_role=str(request.data.get("requester_role", "")).strip(),
            shared_keywords=request.data.get("shared_keywords") or [],
            note=str(request.data.get("note", "")).strip(),
        )
    else:
        requester_name = (request.data.get("requester_name") or "").strip()
        requester_email = (request.data.get("requester_email") or "").strip()
        if not requester_name:
            return Response({"detail": "requester_name is required."}, status=400)
        if not requester_email:
            return Response({"detail": "requester_email is required."}, status=400)
        inquiry = CollaborationInquiry.objects.create(
            from_faculty=None,
            source_type=CollaborationInquiry.SOURCE_EXTERNAL,
            requester_name=requester_name,
            requester_email=requester_email,
            requester_organization=str(request.data.get("requester_organization", "")).strip(),
            target_faculty_name=target_name,
            target_faculty_id=str(request.data.get("target_faculty_id", "")).strip(),
            target_department=str(request.data.get("target_department", "")).strip(),
            target_school=str(request.data.get("target_school", "")).strip(),
            target_project=target_project,
            target_project_title=(
                str(request.data.get("target_project_title", "")).strip()
                or (target_project.title if target_project else "")
            ),
            requester_role=str(request.data.get("requester_role", "")).strip(),
            shared_keywords=request.data.get("shared_keywords") or [],
            note=str(request.data.get("note", "")).strip(),
        )

    return Response(
        {
            "id": inquiry.id,
            "message": "Your inquiry has been submitted.",
        },
        status=201,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def faculty_collaboration_inquiries(request):
    """Logged-in faculty list inquiries aimed at their profile or projects."""
    faculty = Faculty.objects.filter(user=request.user).first()
    if not faculty:
        return Response({"detail": "Faculty profile not found."}, status=404)

    faculty_name = _faculty_display_name(faculty)
    project_ids = list(faculty.projects.values_list("id", flat=True))
    qs = (
        CollaborationInquiry.objects.select_related("from_faculty", "target_project")
        .filter(
            Q(target_faculty_id=str(faculty.id))
            | Q(target_faculty_id=faculty.faculty_id or "")
            | Q(target_faculty_name__iexact=faculty_name)
            | Q(target_project_id__in=project_ids)
        )
        .distinct()
    )
    return Response({"count": qs.count(), "results": [_inquiry_payload(inq) for inq in qs]})


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def faculty_update_inquiry(request, pk):
    """Faculty can mark their own received inquiries as reviewed or closed."""
    faculty = Faculty.objects.filter(user=request.user).first()
    if not faculty:
        return Response({"detail": "Faculty profile not found."}, status=404)

    project_ids = list(faculty.projects.values_list("id", flat=True))
    try:
        inquiry = CollaborationInquiry.objects.filter(
            Q(pk=pk),
            Q(target_faculty_id=str(faculty.id))
            | Q(target_faculty_id=faculty.faculty_id or "")
            | Q(target_faculty_name__iexact=_faculty_display_name(faculty))
            | Q(target_project_id__in=project_ids),
        ).get()
    except CollaborationInquiry.DoesNotExist:
        return Response({"detail": "Not found."}, status=404)

    if request.data.get("status") in (
        CollaborationInquiry.STATUS_REVIEWED,
        CollaborationInquiry.STATUS_CLOSED,
    ):
        inquiry.status = request.data["status"]
        inquiry.save(update_fields=["status", "updated_at"])

    return Response(_inquiry_payload(inquiry))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_collaboration_inquiries(request):
    """Admin lists all collaboration inquiries. Supports ?source=faculty|external filtering."""
    if not request.user.is_staff:
        return Response({"detail": "Forbidden."}, status=403)

    source_filter = request.query_params.get("source", "")
    qs = CollaborationInquiry.objects.select_related("from_faculty", "target_project").all()
    if source_filter in ("faculty", "external"):
        qs = qs.filter(source_type=source_filter)

    data = [_inquiry_payload(inq) for inq in qs]
    return Response({"count": len(data), "results": data})


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def admin_update_inquiry(request, pk):
    """Admin updates the status or adds notes to an inquiry."""
    if not request.user.is_staff:
        return Response({"detail": "Forbidden."}, status=403)
    try:
        inquiry = CollaborationInquiry.objects.get(pk=pk)
    except CollaborationInquiry.DoesNotExist:
        return Response({"detail": "Not found."}, status=404)

    if "status" in request.data:
        inquiry.status = request.data["status"]
        u = request.user
        inquiry.reviewed_by = f"{u.first_name or ''} {u.last_name or ''}".strip() or u.get_username()
        # Audit log
        AdminAuditLog.objects.create(
            admin_username=u.get_username(),
            admin_display_name=inquiry.reviewed_by,
            action=AdminAuditLog.ACTION_UPDATE_INQUIRY,
            target_type="inquiry",
            target_id=inquiry.id,
            target_name=f"{inquiry.target_faculty_name} inquiry",
            notes=f"Status → {inquiry.status}",
        )
    if "admin_notes" in request.data:
        inquiry.admin_notes = request.data["admin_notes"]
    inquiry.save()
    return Response(
        {
            "id": inquiry.id,
            "status": inquiry.status,
            "admin_notes": inquiry.admin_notes,
            "reviewed_by": inquiry.reviewed_by,
        }
    )
