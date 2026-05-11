from django.core.mail import send_mail
from django.db.models import Q
from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..models import Faculty, Paper, Patent, Project, School, Department
from ..serializers import (
    FacultySerializer,
    PaperSerializer,
    SchoolSerializer,
    DepartmentSerializer,
)
from .utils import _normalize_paper_link


class IsAdminUser(IsAuthenticated):
    """Allows access only to staff/superuser accounts."""
    def has_permission(self, request, view):
        return bool(super().has_permission(request, view) and (request.user.is_staff or request.user.is_superuser))


class AdminSchoolListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/admin/schools/  — list all schools
    POST /api/admin/schools/  — create a new school
    """
    permission_classes = [IsAdminUser]
    serializer_class = SchoolSerializer
    queryset = School.objects.all().order_by("display_order", "name")


class AdminSchoolDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/admin/schools/<id>/  — get school detail
    PATCH  /api/admin/schools/<id>/  — update school (name, code, display_order, is_active)
    DELETE /api/admin/schools/<id>/  — delete school (will fail if departments exist — protected)
    """
    permission_classes = [IsAdminUser]
    serializer_class = SchoolSerializer
    queryset = School.objects.all()


class AdminDepartmentListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/admin/departments/           — list all departments
    GET  /api/admin/departments/?school=1  — filter by school id
    POST /api/admin/departments/           — create a new department
         Body: { "name": "...", "school": <school_id>, "code": "..." }
    """
    permission_classes = [IsAdminUser]
    serializer_class = DepartmentSerializer

    def get_queryset(self):
        qs = Department.objects.select_related("school").order_by("school__name", "name")
        school_id = self.request.query_params.get("school")
        if school_id:
            qs = qs.filter(school_id=school_id)
        return qs


class AdminDepartmentDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/admin/departments/<id>/  — get department detail
    PATCH  /api/admin/departments/<id>/  — update (name, school, code, is_active)
    DELETE /api/admin/departments/<id>/  — delete department
    """
    permission_classes = [IsAdminUser]
    serializer_class = DepartmentSerializer
    queryset = Department.objects.select_related("school")


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_faculty_list(request):
    """
    GET /api/admin/faculty/
    Returns all faculty with a `missing_data` flag indicating incomplete records.

    Query params:
      ?missing=true   — only return faculty with missing department or school
      ?search=<term>  — filter by name or email
    """
    if not (request.user.is_staff or request.user.is_superuser):
        return Response({"detail": "Admin privileges required."}, status=403)
    qs = Faculty.objects.prefetch_related(
        "departments", "schools"
    ).select_related("primary_department", "primary_school").order_by("last_name", "first_name")

    search = request.query_params.get("search", "").strip()
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(email__icontains=search))

    missing_only = request.query_params.get("missing", "").lower() == "true"
    if missing_only:
        qs = qs.filter(primary_department__isnull=True)

    # Pending: self-registered, verified institutional email, not yet approved
    pending_only = request.query_params.get("pending", "").lower() == "true"
    if pending_only:
        qs = qs.filter(user__isnull=False, institutional_email_verified=True, is_approved=False)

    results = []
    for f in qs:
        departments = [d.name for d in f.departments.all()]
        schools = [s.name for s in f.schools.all()]
        results.append({
            "id": f.id,
            "name": f.name or f"{f.first_name or ''} {f.last_name or ''}".strip() or f.faculty_id,
            "email": f.email or "",
            "institutional_email": f.institutional_email or "",
            "institutional_email_verified": f.institutional_email_verified,
            "is_approved": f.is_approved,
            "review_status": f.review_status,
            "confirmed_su_faculty": f.confirmed_su_faculty,
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
        })

    return Response(results)


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
        faculty.delete()
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

    faculty.save()

    if "departments" in data:
        faculty.departments.set(data["departments"])

    if "schools" in data:
        faculty.schools.set(data["schools"])

    return Response({
        "id": faculty.id,
        "primary_department": faculty.primary_department.name if faculty.primary_department else None,
        "primary_school": faculty.primary_school.name if faculty.primary_school else None,
        "review_status": faculty.review_status,
        "confirmed_su_faculty": faculty.confirmed_su_faculty,
    })


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

    # Email the faculty
    recipient = faculty.institutional_email or faculty.email
    if recipient:
        faculty_name = faculty.name or f"{faculty.first_name or ''} {faculty.last_name or ''}".strip() or "Faculty"
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
    faculty.save(update_fields=["is_approved", "institutional_email_verified", "updated_at"])

    recipient = faculty.institutional_email or faculty.email
    if recipient:
        faculty_name = faculty.name or f"{faculty.first_name or ''} {faculty.last_name or ''}".strip() or "Faculty"
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
