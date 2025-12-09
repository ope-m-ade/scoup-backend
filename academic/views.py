
import uuid
from rest_framework import generics
from .models import Faculty, Paper, Patent, Project
from .serializers import (
    FacultySerializer,
    FacultyProfileSerializer,
    PaperSerializer,
    ProjectSerializer,
    PatentSerializer,
    FacultySignupSerializer
)
from django.shortcuts import render
from rest_framework.response import Response
from rest_framework import status
from django.contrib.auth.models import User
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import api_view



# Create your views here.
from django.http import HttpResponse

def home(request):
    return HttpResponse("<h1>Welcome to the Scoup Database!</h1><p>Go to <a href='/admin/'>Admin</a></p>")

class FacultyListCreateView(generics.ListCreateAPIView):
    def get_queryset(self): #returns only verified faculty
        return Faculty.objects.filter(is_approved=True, profile_visibility=True)
        
    serializer_class = FacultySerializer

class PaperListCreateView(generics.ListCreateAPIView):
    queryset = Paper.objects.all()
    serializer_class = PaperSerializer

class ProjectListCreateView(generics.ListCreateAPIView):
    queryset = Project.objects.all()
    serializer_class = ProjectSerializer

class PatentListCreateView(generics.ListCreateAPIView):
    queryset = Patent.objects.all()
    serializer_class = PatentSerializer

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework import status
from django.contrib.auth.models import User
from .models import Faculty

@api_view(["POST"])
@permission_classes([AllowAny])  # allows signup without authentication
def faculty_signup(request):
    data = request.data
    username = data.get("username")
    password = data.get("password")
    email = data.get("email")
    first_name = data.get("first_name")
    last_name = data.get("last_name")

    # Validate required fields
    if not username or not password or not email:
        return Response(
            {"error": "Username, password, and email are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Check for existing username/email
    if User.objects.filter(username=username).exists():
        return Response(
            {"error": "Username already exists."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if User.objects.filter(email=email).exists():
        return Response(
            {"error": "Email already exists."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Create User
    user = User.objects.create_user(
        username=username,
        password=password,
        email=email,
        first_name=first_name or "",
        last_name=last_name or "",
    )

    # Create Faculty profile linked to the user
    generated_faculty_id = data.get("faculty_id") or f"SIGNUP-{uuid.uuid4().hex[:12]}"
    # guarantee uniqueness in case of rare collision
    while Faculty.objects.filter(faculty_id=generated_faculty_id).exists():
        generated_faculty_id = f"SIGNUP-{uuid.uuid4().hex[:12]}"

    Faculty.objects.create(
        user=user,
        faculty_id=generated_faculty_id,
        email=email,
        first_name=first_name or "",
        last_name=last_name or "",
        is_approved=False,  # keep false until admin approves
        profile_visibility=True,
    )

    return Response(
        {"message": "Faculty account created. Awaiting approval."},
        status=status.HTTP_201_CREATED,
    )

class FacultyDashboardView(generics.RetrieveAPIView):
    serializer_class = FacultySerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user.faculty_profile


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def faculty_me(request):
    faculty = request.user.faculty_profile
    serializer = FacultyProfileSerializer(faculty)
    return Response(serializer.data)


# ----------------------------------------
# PAPERS for logged-in faculty
# ----------------------------------------
class MyPapersListCreateView(generics.ListCreateAPIView):
    serializer_class = PaperSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Paper.objects.filter(authors=self.request.user.faculty_profile)

    def perform_create(self, serializer):
        paper = serializer.save()
        paper.authors.add(self.request.user.faculty_profile)
        paper.save()


class MyProjectsListCreateView(generics.ListCreateAPIView):
    serializer_class = ProjectSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Project.objects.filter(faculty=self.request.user.faculty_profile)

    def perform_create(self, serializer):
        serializer.save(faculty=self.request.user.faculty_profile)


class MyPatentsListCreateView(generics.ListCreateAPIView):
    serializer_class = PatentSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Patent.objects.filter(faculty=self.request.user.faculty_profile)

    def perform_create(self, serializer):
        serializer.save(faculty=self.request.user.faculty_profile)


from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

class FacultyPhotoUploadView(APIView):
    parser_classes = (MultiPartParser, FormParser)
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        faculty = request.user.faculty_profile  # FIXED
        photo = request.data.get("photo")

        if not photo:
            return Response({"error": "No photo uploaded"}, status=400)

        faculty.photo = photo
        faculty.save()

        return Response({
            "message": "Photo updated",
            "photo": request.build_absolute_uri(faculty.photo.url)
        })

# ============================
# Upload CV + Extract Papers
# ============================

from rest_framework.parsers import MultiPartParser, FormParser
import pdfplumber

class FacultyUploadCVPapers(APIView):
    parser_classes = (MultiPartParser, FormParser)
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        faculty = request.user.faculty_profile
        file = request.FILES.get("file")

        if not file:
            return Response({"error": "No PDF uploaded"}, status=400)

        # --- Extract text from PDF using pdfplumber ---
        try:
            with pdfplumber.open(file) as pdf:
                full_text = "\n".join([page.extract_text() or "" for page in pdf.pages])
        except Exception as e:
            return Response({"error": f"PDF extract error: {str(e)}"}, status=400)

        # --- VERY basic paper extraction (title + DOI) ---
        import re
        entries = []

        # DOI pattern
        doi_pattern = r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+"

        # Try matching papers (very rough first version)
        for line in full_text.split("\n"):
            doi_match = re.search(doi_pattern, line)
            if doi_match:
                entries.append({
                    "title": line.replace(doi_match.group(), "").strip(),
                    "doi": doi_match.group()
                })

        # Create Paper objects
        created = []
        for item in entries:
            paper, _ = Paper.objects.get_or_create(
                doi=item["doi"],
                defaults={"title": item["title"] or "Untitled Paper"}
            )
            paper.authors.add(faculty)
            created.append({"title": paper.title, "doi": paper.doi})

        return Response({
            "message": "PDF processed",
            "papers_found": len(created),
            "papers": created
        })