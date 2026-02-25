from django.contrib import admin
from .models import Faculty, Paper, Project, Patent

admin.site.register(Faculty)
admin.site.register(Paper)
admin.site.register(Project)
admin.site.register(Patent)
