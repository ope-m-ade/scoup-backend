from django.core.cache import cache
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import CollaborationInquiry, Faculty


def _full_name(first_name, last_name, fallback=""):
    return f"{(first_name or '').strip()} {(last_name or '').strip()}".strip() or fallback


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
            shared_keywords=request.data.get("shared_keywords") or [],
            note=str(request.data.get("note", "")).strip(),
        )

    return Response(
        {
            "id": inquiry.id,
            "message": "Your inquiry has been submitted. The SCOUP team will follow up with you shortly.",
        },
        status=201,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_collaboration_inquiries(request):
    """Admin lists all collaboration inquiries. Supports ?source=faculty|external filtering."""
    if not request.user.is_staff:
        return Response({"detail": "Forbidden."}, status=403)

    source_filter = request.query_params.get("source", "")
    qs = CollaborationInquiry.objects.select_related("from_faculty").all()
    if source_filter in ("faculty", "external"):
        qs = qs.filter(source_type=source_filter)

    data = []
    for inq in qs:
        faculty = inq.from_faculty
        if faculty:
            sender_name = _full_name(
                faculty.first_name,
                faculty.last_name,
                (faculty.name or "").strip() or faculty.faculty_id,
            )
            sender_email = faculty.email or ""
            sender_dept = faculty.department or ""
        else:
            sender_name = inq.requester_name
            sender_email = inq.requester_email
            sender_dept = inq.requester_organization
        data.append(
            {
                "id": inq.id,
                "status": inq.status,
                "source_type": inq.source_type,
                "created_at": inq.created_at.isoformat(),
                "from_faculty_name": sender_name,
                "from_faculty_email": sender_email,
                "from_faculty_department": sender_dept,
                "target_faculty_name": inq.target_faculty_name,
                "target_faculty_id": inq.target_faculty_id,
                "target_department": inq.target_department,
                "target_school": inq.target_school,
                "shared_keywords": inq.shared_keywords or [],
                "note": inq.note,
                "admin_notes": inq.admin_notes,
            }
        )
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
    if "admin_notes" in request.data:
        inquiry.admin_notes = request.data["admin_notes"]
    inquiry.save()
    return Response(
        {
            "id": inquiry.id,
            "status": inquiry.status,
            "admin_notes": inquiry.admin_notes,
        }
    )
