
import uuid
from rest_framework import generics
from .models import Faculty, Paper, Patent, Project
from .serializers import FacultySerializer, PaperSerializer, ProjectSerializer, PatentSerializer, FacultySignupSerializer
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


@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def faculty_me(request):
    try:
        faculty = Faculty.objects.get(user=request.user)
    except Faculty.DoesNotExist:
        return Response({"error": "Faculty profile not found"}, status=404)

    # --- GET: return profile ---
    if request.method == "GET":
        serializer = FacultySerializer(faculty)
        return Response(serializer.data)

    # --- PATCH: update profile ---
    if request.method == "PATCH":
        serializer = FacultySerializer(
            faculty,
            data=request.data,
            partial=True   # allows partial update
        )
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=400)


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