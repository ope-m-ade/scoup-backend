from django.urls import path
from django.conf import settings
from django.conf.urls.static import static
from . import views
from .auth import EmailOrUsernameTokenObtainPairView
from .views import (
    admin_me,
    approve_faculty_suggestion,
    FacultyListCreateView,
    FacultyDetailView,
    faculty_me,
    faculty_me_suggestions,
    faculty_suggestion_preview,
    public_search_data,
    semantic_paper_search,
    reject_faculty_suggestion,
    PaperListCreateView,
    PaperDetailView,
    ProjectListCreateView,
    ProjectDetailView,
    PatentListCreateView,
    PatentDetailView,
    FacultyPhotoUploadView,
    FacultyUploadCVPapers,
)
from rest_framework_simplejwt.views import (
    TokenRefreshView,
)
urlpatterns = [
    path('', views.home, name='home'),
    path("public/search-data/", public_search_data, name="public-search-data"),
    path("semantic/papers/", semantic_paper_search, name="semantic-paper-search"),

    path('faculty/', FacultyListCreateView.as_view(), name='faculty-list'),
    path('faculty/<int:pk>/', FacultyDetailView.as_view(), name='faculty-detail'),
    path('faculty/me/', faculty_me, name='faculty_me'),
    path('admin/me/', admin_me, name='admin_me'),
    path("faculty/me/suggestions/", faculty_me_suggestions, name="faculty-me-suggestions"),
    path(
        "faculty/me/suggestions/<int:external_faculty_id>/approve/",
        approve_faculty_suggestion,
        name="faculty-suggestion-approve",
    ),
    path(
        "faculty/me/suggestions/<int:external_faculty_id>/preview/",
        faculty_suggestion_preview,
        name="faculty-suggestion-preview",
    ),
    path(
        "faculty/me/suggestions/<int:external_faculty_id>/reject/",
        reject_faculty_suggestion,
        name="faculty-suggestion-reject",
    ),
    path("papers/", PaperListCreateView.as_view(), name="paper-list"),
    path("papers/<int:pk>/", PaperDetailView.as_view(), name="paper-detail"),
    path("projects/", ProjectListCreateView.as_view(), name="project-list"),
    path("projects/<int:pk>/", ProjectDetailView.as_view(), name="project-detail"),
    path("patents/", PatentListCreateView.as_view(), name="patent-list"),
    path("patents/<int:pk>/", PatentDetailView.as_view(), name="patent-detail"),
    path("faculty/signup/", views.faculty_signup, name="faculty_signup"),
    path("token/", EmailOrUsernameTokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("faculty/upload-photo/", FacultyPhotoUploadView.as_view()),
    path("faculty/upload-cv-papers/", FacultyUploadCVPapers.as_view(), name="upload-cv-papers"),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
