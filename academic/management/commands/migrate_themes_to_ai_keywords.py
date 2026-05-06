"""
Management command: migrate_themes_to_ai_keywords
==================================================
Copies themes data into ai_keywords for every faculty record where:
  - themes is non-empty, AND
  - ai_keywords is empty / None  (don't overwrite genuine AI-generated keywords)

Run:
    python manage.py migrate_themes_to_ai_keywords
    python manage.py migrate_themes_to_ai_keywords --overwrite   # replace existing ai_keywords too
    python manage.py migrate_themes_to_ai_keywords --dry-run     # preview only

After running, ai_keywords will contain the Academic Metrics research themes,
which is what ai_keywords was always intended to hold.
themes can be left in place (it costs nothing) or cleared later with --clear-themes.
"""

from django.core.management.base import BaseCommand
from academic.models import Faculty


def _normalize(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    return []


class Command(BaseCommand):
    help = "Copy themes → ai_keywords for faculty who have themes but no ai_keywords"

    def add_arguments(self, parser):
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Also overwrite ai_keywords when it already has data",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview changes without saving",
        )
        parser.add_argument(
            "--clear-themes",
            action="store_true",
            help="After copying, set themes to [] on migrated records",
        )

    def handle(self, *args, **options):
        overwrite  = options["overwrite"]
        dry_run    = options["dry_run"]
        clear      = options["clear_themes"]

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes will be saved"))

        all_faculty = Faculty.objects.all()
        migrated = skipped = already_done = 0

        for faculty in all_faculty:
            themes   = _normalize(faculty.themes)
            existing = _normalize(faculty.ai_keywords)

            if not themes:
                skipped += 1
                continue

            if existing and not overwrite:
                already_done += 1
                continue

            name = (
                faculty.name
                or f"{faculty.first_name or ''} {faculty.last_name or ''}".strip()
                or faculty.email or f"id={faculty.id}"
            )

            self.stdout.write(
                f"  {'[DRY] ' if dry_run else ''}Migrating {name!r}: "
                f"{len(themes)} themes → ai_keywords"
                + (f" (replacing {len(existing)} existing)" if existing else "")
            )

            if not dry_run:
                faculty.ai_keywords = themes
                if clear:
                    faculty.themes = []
                faculty.save(update_fields=(
                    ["ai_keywords", "themes"] if clear else ["ai_keywords"]
                ))

            migrated += 1

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Done. Migrated: {migrated}  |  Already had ai_keywords: {already_done}  |  No themes: {skipped}"
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING("No changes saved (dry run)."))
