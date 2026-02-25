from django.contrib import admin
from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path

from academic import views

urlpatterns = [
    path("", views.home),
    path("admin/", admin.site.urls),
    path("api/", include("academic.urls")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
