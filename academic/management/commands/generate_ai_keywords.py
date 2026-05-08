from django.core.management.base import BaseCommand, CommandError

from academic.ai_keywords import (
    DEFAULT_KEYWORD_MODEL,
    faculty_queryset_for_keyword_generation,
    generate_faculty_ai_keywords,
    has_keyword_generation_evidence,
)
from academic.search_engine import merge_unique_list, normalize_keyword_list


class Command(BaseCommand):
    help = "Generate faculty ai_keywords from profile, papers, patents, and projects using OpenAI."

    def add_arguments(self, parser):
        parser.add_argument(
            "--model",
            default=DEFAULT_KEYWORD_MODEL,
            help="OpenAI model used for keyword generation",
        )
        parser.add_argument(
            "--faculty-id",
            action="append",
            default=[],
            help="Generate keywords for one faculty id. Can be provided multiple times.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Generate for at most N faculty records.",
        )
        parser.add_argument(
            "--max-keywords",
            type=int,
            default=12,
            help="Maximum generated keywords per faculty member.",
        )
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Replace existing ai_keywords instead of skipping records that already have them.",
        )
        parser.add_argument(
            "--merge",
            action="store_true",
            help="Merge generated keywords with existing ai_keywords instead of replacing them. Implies --overwrite.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Call the model and print generated keywords, but do not save.",
        )

    def handle(self, *args, **options):
        model = str(options["model"] or DEFAULT_KEYWORD_MODEL).strip()
        faculty_ids = [str(value).strip() for value in options["faculty_id"] if str(value).strip()]
        limit = max(0, int(options["limit"] or 0))
        max_keywords = max(3, min(int(options["max_keywords"] or 12), 20))
        overwrite = bool(options["overwrite"] or options["merge"])
        merge = bool(options["merge"])
        dry_run = bool(options["dry_run"])

        queryset = faculty_queryset_for_keyword_generation()
        if faculty_ids:
            queryset = queryset.filter(id__in=faculty_ids)
        if not overwrite:
            queryset = queryset.filter(ai_keywords__in=["", None])

        faculty_members = [
            faculty for faculty in queryset
            if has_keyword_generation_evidence(faculty)
        ]
        if limit:
            faculty_members = faculty_members[:limit]

        if not faculty_members:
            self.stdout.write(self.style.WARNING("No faculty records require AI keyword generation."))
            return

        self.stdout.write(
            f"Generating AI keywords for {len(faculty_members)} faculty record(s) with model={model}"
        )

        updated = 0
        for faculty in faculty_members:
            name = (
                faculty.name
                or f"{faculty.first_name or ''} {faculty.last_name or ''}".strip()
                or faculty.email
                or str(faculty.id)
            )

            try:
                generated = generate_faculty_ai_keywords(
                    faculty,
                    model=model,
                    max_keywords=max_keywords,
                )
            except RuntimeError as exc:
                raise CommandError(str(exc)) from exc
            except Exception as exc:
                raise CommandError(f"Keyword generation failed for {name}: {exc}") from exc

            if not generated:
                self.stdout.write(self.style.WARNING(f"{name}: no keywords generated"))
                continue

            existing = normalize_keyword_list(faculty.ai_keywords)
            next_keywords = merge_unique_list(existing, generated) if merge else generated

            self.stdout.write(f"{name}: {', '.join(next_keywords)}")
            if dry_run:
                continue

            faculty.ai_keywords = ", ".join(next_keywords)
            faculty.save(update_fields=["ai_keywords"])
            updated += 1

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN complete. No faculty records were updated."))
            return

        self.stdout.write(self.style.SUCCESS(f"DONE. Updated {updated} faculty record(s)."))
