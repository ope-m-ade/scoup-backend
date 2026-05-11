"""
Management command to import Academic Metrics data into the SCOUP database.

Safe to run on an existing database — it only adds/updates academic data
and never touches User accounts, Faculty login accounts, or Inquiries.

What it does:
  1. Updates existing Paper records with taxonomy categories and themes
  2. Creates new Paper records for papers not yet in the DB
  3. Updates existing Faculty records with citation stats, categories, themes
  4. Links papers to faculty authors by name matching

Usage:
    python manage.py import_academic_metrics
    python manage.py import_academic_metrics --dry-run
"""

import json
import os
from datetime import date
from django.core.management.base import BaseCommand
from django.conf import settings

from academic.models import Faculty, Paper


DATA_DIR = os.path.join(settings.BASE_DIR, "data")
ARTICLE_DATA = os.path.join(DATA_DIR, "article_data.json")
FACULTY_DATA = os.path.join(DATA_DIR, "faculty_data.json")


def normalize_name(name):
    return " ".join((name or "").strip().lower().split())


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def import_papers(articles, stdout, dry_run=False):
    created = 0
    updated = 0
    skipped = 0

    for article in articles:
        doi = (article.get("doi") or article.get("_id") or "").strip()
        title_list = article.get("title", [])
        title = title_list[0] if isinstance(title_list, list) else title_list
        title = (title or "").strip()

        if not title:
            skipped += 1
            continue

        # Find existing paper by DOI or title
        paper = None
        if doi:
            paper = Paper.objects.filter(doi__iexact=doi).first()
        if not paper and title:
            paper = Paper.objects.filter(title__iexact=title).first()

        themes = article.get("themes", [])
        categories = article.get("categories", [])
        top_cats = article.get("top_level_categories", [])
        mid_cats = article.get("mid_level_categories", [])
        abstract = (article.get("abstract") or "").strip()
        journal = (article.get("journal") or "").strip()
        faculty_members = article.get("faculty_members", [])
        faculty_affiliations = article.get("faculty_affiliations", {})

        if paper:
            if not dry_run:
                if themes:
                    paper.themes = themes
                if categories:
                    paper.keywords = categories
                    paper.ai_keywords = categories
                if top_cats:
                    paper.top_level_categories = top_cats
                if mid_cats:
                    paper.mid_level_categories = mid_cats
                if abstract and not paper.abstract:
                    paper.abstract = abstract
                if journal and not paper.journal:
                    paper.journal = journal
                paper.faculty_members = faculty_members
                paper.faculty_affiliations = faculty_affiliations
                paper.save()
            updated += 1
        else:
            if not doi:
                doi = f"am-import-{title[:40].replace(' ', '-').lower()}"

            date_published = None
            date_str = (
                article.get("date_published_online")
                or article.get("date_published_print")
                or ""
            )
            if date_str:
                try:
                    parts = date_str.split("-")
                    year = int(parts[0])
                    month = int(parts[1]) if len(parts) > 1 else 1
                    day = int(parts[2]) if len(parts) > 2 else 1
                    date_published = date(year, month, day)
                except Exception:
                    pass

            if not dry_run:
                paper = Paper.objects.create(
                    doi=doi,
                    title=title,
                    abstract=abstract,
                    journal=journal,
                    date_published=date_published,
                    download_url=article.get("download_url") or "",
                    themes=themes,
                    keywords=categories,
                    ai_keywords=categories,
                    top_level_categories=top_cats,
                    mid_level_categories=mid_cats,
                    faculty_members=faculty_members,
                    faculty_affiliations=faculty_affiliations,
                    tc_count=article.get("tc_count") or 0,
                )
            created += 1

        # Link faculty authors by name
        if not dry_run and paper:
            for faculty_name in faculty_members:
                faculty_qs = Faculty.objects.filter(name__iexact=faculty_name)
                if not faculty_qs.exists():
                    parts = faculty_name.strip().split()
                    if len(parts) >= 2:
                        faculty_qs = Faculty.objects.filter(
                            first_name__iexact=parts[0],
                            last_name__iexact=parts[-1],
                        )
                for faculty in faculty_qs:
                    paper.authors.add(faculty)

    return created, updated, skipped


def import_faculty(faculty_list, stdout, dry_run=False):
    updated = 0
    skipped = 0

    for record in faculty_list:
        name = (record.get("name") or "").strip()
        if not name:
            skipped += 1
            continue

        faculty = Faculty.objects.filter(name__iexact=name).first()
        if not faculty:
            parts = name.split()
            if len(parts) >= 2:
                faculty = Faculty.objects.filter(
                    first_name__iexact=parts[0],
                    last_name__iexact=parts[-1],
                ).first()

        if not faculty:
            skipped += 1
            continue

        if not dry_run:
            faculty.total_citations = record.get("total_citations") or 0
            faculty.article_count = record.get("article_count") or 0
            faculty.average_citations = record.get("average_citations") or 0.0

            categories = record.get("categories", [])
            themes = record.get("themes", [])
            journals = record.get("journals", [])
            dois = record.get("dois", [])
            titles = record.get("titles", [])

            if categories:
                faculty.keywords = categories
            if themes:
                faculty.themes = themes
            if journals:
                faculty.journals = journals
            if dois:
                faculty.dois = dois
            if titles:
                faculty.titles = titles

            all_keywords = list(set(categories + themes))
            faculty.ai_keywords = ", ".join(all_keywords[:50])
            faculty.save()

        updated += 1

    return updated, skipped


class Command(BaseCommand):
    help = (
        "Import Academic Metrics data (articles + faculty stats) from data/ folder. "
        "Safe to run on an existing database — never touches user accounts or inquiries."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be imported without writing to the database.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes will be written.\n"))

        # Verify data files exist
        for path, label in [(ARTICLE_DATA, "article_data.json"), (FACULTY_DATA, "faculty_data.json")]:
            if not os.path.exists(path):
                self.stderr.write(self.style.ERROR(f"Missing: {path}"))
                self.stderr.write("Make sure data/ folder is present in the project root.")
                return

        self.stdout.write("Loading data files...")
        articles = load_json(ARTICLE_DATA)
        faculty_list = load_json(FACULTY_DATA)
        self.stdout.write(f"  {len(articles)} articles, {len(faculty_list)} faculty records\n")

        self.stdout.write("Importing papers...")
        created, updated, skipped = import_papers(articles, self.stdout, dry_run=dry_run)
        self.stdout.write(self.style.SUCCESS(f"  Created : {created}"))
        self.stdout.write(self.style.SUCCESS(f"  Updated : {updated}"))
        self.stdout.write(f"  Skipped : {skipped} (no title)\n")

        self.stdout.write("Importing faculty metrics...")
        f_updated, f_skipped = import_faculty(faculty_list, self.stdout, dry_run=dry_run)
        self.stdout.write(self.style.SUCCESS(f"  Updated : {f_updated}"))
        self.stdout.write(f"  No match: {f_skipped}\n")

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run complete — nothing written."))
        else:
            self.stdout.write(self.style.SUCCESS("Done. Browse Categories should now be populated."))
