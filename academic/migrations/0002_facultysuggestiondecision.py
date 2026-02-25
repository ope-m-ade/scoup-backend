from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("academic", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="FacultySuggestionDecision",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "decision",
                    models.CharField(
                        choices=[("approved", "Approved"), ("rejected", "Rejected")],
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "external_faculty",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reviewed_by",
                        to="academic.faculty",
                    ),
                ),
                (
                    "reviewer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="suggestion_decisions",
                        to="academic.faculty",
                    ),
                ),
            ],
            options={
                "unique_together": {("reviewer", "external_faculty")},
            },
        ),
    ]
