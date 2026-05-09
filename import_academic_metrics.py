"""
Import Academic Metrics data into SCOUP database.

This script:
1. Updates existing Paper records with taxonomy categories, themes from article_data.json
2. Updates existing Faculty records with citation stats, categories, themes from faculty_data.json
3. Creates new Paper records for any papers not yet in the DB
4. Links papers to faculty by name matching

Run from the scoupdb directory:
    python import_academic_metrics.py
"""

import os
import json
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "scoupdb.settings")
django.setup()

from academic.models import Faculty, Paper

ARTICLE_DATA = "/Users/opeade/codeStuff/SCOUP_FINAL/2024Fall-COSC425-AcademicMetrics-master/src/academic_metrics/DB/article_data.json"
FACULTY_DATA = "/Users/opeade/codeStuff/SCOUP_FINAL/2024Fall-COSC425-AcademicMetrics-master/src/academic_metrics/DB/faculty_data.json"


def normalize_name(name):
    return " ".join((name or "").strip().lower().split())


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def import_papers(articles):
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
        low_cats = article.get("low_level_categories", [])
        abstract = (article.get("abstract") or "").strip()
        journal = (article.get("journal") or "").strip()
        faculty_members = article.get("faculty_members", [])
        faculty_affiliations = article.get("faculty_affiliations", {})

        if paper:
            # Update existing paper with taxonomy data
            if themes:
                paper.themes = themes
            if categories:
                paper.keywords = categories  # store full category list as searchable keywords
                paper.ai_keywords = categories
            if abstract and not paper.abstract:
                paper.abstract = abstract
            if journal and not paper.journal:
                paper.journal = journal
            paper.faculty_members = faculty_members
            paper.faculty_affiliations = faculty_affiliations
            paper.save()
            updated += 1
        else:
            # Create new paper
            if not doi:
                doi = f"am-import-{title[:40].replace(' ', '-').lower()}"

            date_published = None
            date_str = article.get("date_published_online") or article.get("date_published_print") or ""
            if date_str:
                try:
                    parts = date_str.split("-")
                    year = int(parts[0])
                    month = int(parts[1]) if len(parts) > 1 else 1
                    day = int(parts[2]) if len(parts) > 2 else 1
                    from datetime import date
                    date_published = date(year, month, day)
                except Exception:
                    pass

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
                faculty_members=faculty_members,
                faculty_affiliations=faculty_affiliations,
                tc_count=article.get("tc_count") or 0,
            )
            created += 1

        # Link faculty authors by name
        for faculty_name in faculty_members:
            norm = normalize_name(faculty_name)
            # Try exact name match first
            faculty_qs = Faculty.objects.filter(name__iexact=faculty_name)
            if not faculty_qs.exists():
                # Try first + last name split
                parts = faculty_name.strip().split()
                if len(parts) >= 2:
                    faculty_qs = Faculty.objects.filter(
                        first_name__iexact=parts[0],
                        last_name__iexact=parts[-1]
                    )
            for faculty in faculty_qs:
                paper.authors.add(faculty)

    return created, updated, skipped


def import_faculty(faculty_list):
    updated = 0
    skipped = 0

    for record in faculty_list:
        name = (record.get("name") or "").strip()
        if not name:
            skipped += 1
            continue

        # Try to find faculty by name
        faculty = Faculty.objects.filter(name__iexact=name).first()
        if not faculty:
            parts = name.split()
            if len(parts) >= 2:
                faculty = Faculty.objects.filter(
                    first_name__iexact=parts[0],
                    last_name__iexact=parts[-1]
                ).first()

        if not faculty:
            skipped += 1
            continue

        # Update with metrics and taxonomy
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

        # Build merged keyword string for search
        all_keywords = list(set(categories + themes))
        faculty.ai_keywords = ", ".join(all_keywords[:50])

        faculty.save()
        updated += 1

    return updated, skipped


if __name__ == "__main__":
    print("Loading data files...")
    articles = load_json(ARTICLE_DATA)
    faculty_list = load_json(FACULTY_DATA)

    print(f"Found {len(articles)} articles and {len(faculty_list)} faculty records")
    print()

    print("Importing papers...")
    created, updated, skipped = import_papers(articles)
    print(f"  ✓ Created: {created}")
    print(f"  ✓ Updated: {updated}")
    print(f"  ✗ Skipped: {skipped}")
    print()

    print("Importing faculty metrics...")
    f_updated, f_skipped = import_faculty(faculty_list)
    print(f"  ✓ Updated: {f_updated}")
    print(f"  ✗ Not matched: {f_skipped}")
    print()

    print("Done. Run a search to verify results.")
