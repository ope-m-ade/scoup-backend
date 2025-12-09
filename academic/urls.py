from django.urls import path
from django.conf import settings
from django.conf.urls.static import static
from . import views
from .views import (
    FacultyListCreateView,
    faculty_me,
    MyPapersListCreateView,
    MyProjectsListCreateView,
    MyPatentsListCreateView,
    FacultyPhotoUploadView,
    FacultyUploadCVPapers,
)
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)
urlpatterns = [
    path('', views.home, name='home'),

    path('faculty/', FacultyListCreateView.as_view(), name='faculty-list'),
    path('faculty/me/', faculty_me, name='faculty_me'),
    path("papers/", views.MyPapersListCreateView.as_view(), name="my-papers"),
    path("projects/", views.MyProjectsListCreateView.as_view(), name="my-projects"),
    path("patents/", views.MyPatentsListCreateView.as_view(), name="my-patents"),
    path("faculty/signup/", views.faculty_signup, name="faculty_signup"),
    path("token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("faculty/papers/", MyPapersListCreateView.as_view(), name="my-papers"),
    path("faculty/projects/", MyProjectsListCreateView.as_view()),
    path("faculty/patents/", MyPatentsListCreateView.as_view()),
    path("faculty/upload-photo/", FacultyPhotoUploadView.as_view()),
path("faculty/upload-cv-papers/", FacultyUploadCVPapers.as_view(), name="upload-cv-papers"),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
