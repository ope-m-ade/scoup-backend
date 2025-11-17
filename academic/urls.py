from django.urls import path
from . import views
from django.urls import path
from .views import FacultyListCreateView, faculty_me, MyPapersListCreateView
from django.urls import path
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

urlpatterns = [
	path('', views.home, name='home'),
	path('faculty/', FacultyListCreateView.as_view(), name='faculty-list'),
	path('faculty/me/', faculty_me, name='faculty_me'),
	path('papers/', views.PaperListCreateView.as_view(), name='paper-list-create'),
	path('projects/', views.ProjectListCreateView.as_view(), name='project-list'),
	path('patents/', views.PatentListCreateView.as_view(), name='patent-list'),
	path("faculty/signup/", views.faculty_signup, name="faculty_signup"),
	path("token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
	path("faculty/papers/", MyPapersListCreateView.as_view(), name="my-papers"),
]