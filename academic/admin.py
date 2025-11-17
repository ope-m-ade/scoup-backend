from django.contrib import admin

# Register your models here.
from django.contrib import admin
from .models import Faculty, Paper, Project, Patent

admin.site.register(Faculty)
admin.site.register(Paper)
admin.site.register(Project)
admin.site.register(Patent)
