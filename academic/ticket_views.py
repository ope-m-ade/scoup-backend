from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import Faculty, SupportTicket


def _ticket_payload(ticket):
    faculty = ticket.faculty
    return {
        "id": ticket.id,
        "ticket_type": ticket.ticket_type,
        "subject": ticket.subject,
        "description": ticket.description,
        "status": ticket.status,
        "admin_notes": ticket.admin_notes,
        "resolved_by": ticket.resolved_by,
        "created_at": ticket.created_at.isoformat(),
        "updated_at": ticket.updated_at.isoformat(),
        "faculty_name": (
            f"{(faculty.first_name or '').strip()} {(faculty.last_name or '').strip()}".strip()
            or faculty.name
            or faculty.faculty_id
        ) if faculty else "",
        "faculty_email": faculty.email or "" if faculty else "",
        "faculty_id": faculty.id if faculty else None,
        "submitter_name": ticket.submitter_name,
        "submitter_email": ticket.submitter_email,
    }


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def faculty_submit_ticket(request):
    """Faculty submits a support ticket."""
    faculty = Faculty.objects.filter(user=request.user).first()
    if not faculty:
        return Response({"detail": "Faculty profile not found."}, status=404)

    subject = (request.data.get("subject") or "").strip()
    description = (request.data.get("description") or "").strip()
    ticket_type = (request.data.get("ticket_type") or SupportTicket.TYPE_OTHER).strip()

    if not subject:
        return Response({"detail": "Subject is required."}, status=400)
    if not description:
        return Response({"detail": "Description is required."}, status=400)
    if ticket_type not in dict(SupportTicket.TYPE_CHOICES):
        ticket_type = SupportTicket.TYPE_OTHER

    ticket = SupportTicket.objects.create(
        faculty=faculty,
        ticket_type=ticket_type,
        subject=subject,
        description=description,
    )
    return Response(_ticket_payload(ticket), status=201)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def faculty_list_tickets(request):
    """Faculty lists their own support tickets."""
    faculty = Faculty.objects.filter(user=request.user).first()
    if not faculty:
        return Response({"detail": "Faculty profile not found."}, status=404)

    tickets = SupportTicket.objects.filter(faculty=faculty)
    return Response({"count": tickets.count(), "results": [_ticket_payload(t) for t in tickets]})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_list_tickets(request):
    """Admin lists all support tickets. Supports ?status= and ?type= filters."""
    if not request.user.is_staff:
        return Response({"detail": "Forbidden."}, status=403)

    qs = SupportTicket.objects.select_related("faculty").all()
    status_filter = request.query_params.get("status", "")
    type_filter = request.query_params.get("type", "")
    if status_filter:
        qs = qs.filter(status=status_filter)
    if type_filter:
        qs = qs.filter(ticket_type=type_filter)

    return Response({"count": qs.count(), "results": [_ticket_payload(t) for t in qs]})


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def admin_update_ticket(request, pk):
    """Admin updates a support ticket status or adds notes."""
    if not request.user.is_staff:
        return Response({"detail": "Forbidden."}, status=403)

    try:
        ticket = SupportTicket.objects.get(pk=pk)
    except SupportTicket.DoesNotExist:
        return Response({"detail": "Not found."}, status=404)

    if "status" in request.data:
        ticket.status = request.data["status"]
        if ticket.status in (SupportTicket.STATUS_RESOLVED, SupportTicket.STATUS_CLOSED):
            u = request.user
            ticket.resolved_by = f"{u.first_name or ''} {u.last_name or ''}".strip() or u.get_username()
    if "admin_notes" in request.data:
        ticket.admin_notes = request.data["admin_notes"]
    ticket.save()
    return Response(_ticket_payload(ticket))


@api_view(["POST"])
@permission_classes([AllowAny])
def public_submit_ticket(request):
    """Submit a support ticket without an account. Requires submitter_name and submitter_email."""
    subject = (request.data.get("subject") or "").strip()
    description = (request.data.get("description") or "").strip()
    ticket_type = (request.data.get("ticket_type") or SupportTicket.TYPE_OTHER).strip()
    submitter_name = (request.data.get("submitter_name") or "").strip()
    submitter_email = (request.data.get("submitter_email") or "").strip()

    if not subject:
        return Response({"detail": "Subject is required."}, status=400)
    if not description:
        return Response({"detail": "Description is required."}, status=400)
    if not submitter_name:
        return Response({"detail": "Your name is required."}, status=400)
    if not submitter_email:
        return Response({"detail": "Your email is required."}, status=400)
    if ticket_type not in dict(SupportTicket.TYPE_CHOICES):
        ticket_type = SupportTicket.TYPE_OTHER

    # If the requester is actually logged in as faculty, link them
    faculty = None
    if request.user and request.user.is_authenticated:
        from .models import Faculty
        faculty = Faculty.objects.filter(user=request.user).first()

    ticket = SupportTicket.objects.create(
        faculty=faculty,
        submitter_name=submitter_name,
        submitter_email=submitter_email,
        ticket_type=ticket_type,
        subject=subject,
        description=description,
    )
    return Response({"id": ticket.id, "message": "Ticket submitted. We'll be in touch."}, status=201)
