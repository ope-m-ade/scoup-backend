#A serializer converts your model (like Faculty) into JSON, so your frontend can read it.

from rest_framework import serializers
from django.contrib.auth.models import User
from .models import Faculty, Paper, Patent, Project

class FacultySerializer(serializers.ModelSerializer):
    class Meta:
        model = Faculty
        fields = "__all__"
        read_only_fields = ["user"]

class FacultyProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = Faculty
        fields = "__all__"
        read_only_fields = ["user", "faculty_id", "created_at", "updated_at"]

class PaperSerializer(serializers.ModelSerializer):
    class Meta:
        model = Paper
        fields = "__all__"
        read_only_fields = ("authors",)

class ProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = '__all__'
        read_only_fields = ('faculty',)

class PatentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Patent
        fields = '__all__'
        read_only_fields = ('faculty',)

class FacultySignupSerializer(serializers.ModelSerializer):
    username = serializers.CharField(write_only=True)
    password = serializers.CharField(write_only=True, min_length=6)
    email = serializers.EmailField(required=True)

    class Meta:
        model = Faculty
        fields = [
            "username", "password", "email",
            "first_name", "last_name", "department", "title", "bio"
        ]

    def create(self, validated_data):
        username = validated_data.pop("username")
        password = validated_data.pop("password")
        email = validated_data.get("email")

        # Create Django user
        user = User.objects.create_user(
            username=username,
            password=password,
            email=email,
            first_name=validated_data.get("first_name", ""),
            last_name=validated_data.get("last_name", "")
        )

        # Create linked faculty record
        faculty = Faculty.objects.create(
            user=user,
            email=email,
            first_name=validated_data.get("first_name", ""),
            last_name=validated_data.get("last_name", ""),
            department=validated_data.get("department", ""),
            title=validated_data.get("title", ""),
            bio=validated_data.get("bio", ""),
            is_approved=False,  # you can auto-approve by setting True
            profile_visibility=True,
        )

        return faculty

from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

class FacultyPhotoUploadView(APIView):
    parser_classes = (MultiPartParser, FormParser)
    permission_classes = [IsAuthenticated]

    def post(self, request):
        faculty = request.user.faculty_profile
        photo = request.data.get("photo")

        if not photo:
            return Response({"error": "No photo uploaded"}, status=400)

        faculty.photo = photo
        faculty.save()

        return Response({
            "photo": request.build_absolute_uri(faculty.photo.url)
        })
