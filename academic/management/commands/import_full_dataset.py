import json
from pathlib import Path
from datetime import datetime, date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from academic.models import Faculty, Paper, PaperAuthorship


def parse_date_any(value):
    """
    Accepts 'YYYY-mm-dd' | 'YYYY-mm' | 'YYYY' | None and returns a date or None.
    """
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            dt = datetime.strptime(str(value), fmt)
            if fmt == "%Y":
                return date(dt.year, 1, 1)
            if fmt == "%Y-%m":
                return date(dt.year, dt.month, 1)
            return dt.date()
        except ValueError:
            continue
    return None


def as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def merge_keywords_from_record(rec):
    """
    Merge top/mid/low categories into one deduped list for search.
    """
    merged = []
    for key in (
        "categories",
        "top_level_categories",
        "mid_level_categories",
        "low_level_categories",
    ):
        merged.extend(as_list(rec.get(key, [])))
    # Deduplicate while preserving order
    seen = set()
    out = []
    for k in merged:
        if not isinstance(k, str):
            continue
        s = k.strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


class Command(BaseCommand):
    help = "Import AcademicMetrics faculty + article JSON into Django models and link authorships."

    def add_arguments(self, parser):
        parser.add_argument("--faculty", required=True, help="Path to faculty_data.json")
        parser.add_argument("--papers",  required=True, help="Path to article_data.json")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--reset",   action="store_true", help="Delete existing Faculty/Paper/PaperAuthorship first")
        parser.add_argument("--max",     type=int, default=0, help="Import at most N papers (for testing)")

    def handle(self, *args, **opts):
        fpath = Path(opts["faculty"])
        ppath = Path(opts["papers"])
        dry   = opts["dry_run"]
        reset = opts["reset"]
        max_n = int(opts["max"] or 0)

        if not fpath.exists(): raise CommandError(f"Faculty file not found: {fpath}")
        if not ppath.exists(): raise CommandError(f"Papers file not found: {ppath}")

        try:
            faculty_json = json.loads(fpath.read_text())
            papers_json  = json.loads(ppath.read_text())
        except Exception as e:
            raise CommandError(f"Failed to parse JSON: {e}")

        if not isinstance(faculty_json, list) or not isinstance(papers_json, list):
            raise CommandError("Both JSON files must contain top-level lists")

        # Reset (optional)
        if reset and not dry:
            PaperAuthorship.objects.all().delete()
            Paper.objects.all().delete()
            Faculty.objects.all().delete()
            self.stdout.write(self.style.WARNING("Existing Faculty/Paper/PaperAuthorship deleted."))

        created_fac = updated_fac = 0
        created_pap = updated_pap = 0
        linked = 0

        ctx = transaction.atomic() if not dry else _NullCtx()
        with ctx:
            # 1) FACULTY
            for i, rec in enumerate(faculty_json, 1):
                fid  = (rec.get("_id") or "").strip()  # AcademicMetrics slug
                name = (rec.get("name") or "").strip()
                if not fid or not name:
                    continue

                fac, made = Faculty.objects.get_or_create(faculty_id=fid, defaults={"name": name})
                if made:
                    created_fac += 1
                else:
                    updated_fac += 1

                fac.name = name
                fac.total_citations   = rec.get("total_citations") or 0
                fac.article_count     = rec.get("article_count") or 0
                fac.average_citations = float(rec.get("average_citations") or 0.0)

                fac.department_affiliations = as_list(rec.get("department_affiliations"))
                fac.dois   = as_list(rec.get("dois"))
                fac.titles = as_list(rec.get("titles"))
                fac.categories = as_list(rec.get("categories"))

                # merged flat keywords
                fac.keywords = merge_keywords_from_record(rec)

                # try to backfill first/last if missing
                if not fac.first_name or not fac.last_name:
                    parts = name.split()
                    if len(parts) == 1:
                        fac.last_name = parts[0]
                    elif len(parts) > 1:
                        fac.first_name = " ".join(parts[:-1])
                        fac.last_name  = parts[-1]

                fac.save()

            # Build quick lookup for Faculty by name (case-insensitive)
            # and a DOI->Faculty list map from fac.dois for linking.
            fac_by_name = {}
            doi_to_faculty_ids = {}
            for fac in Faculty.objects.all():
                key = (fac.name or "").strip().lower()
                if key:
                    fac_by_name.setdefault(key, []).append(fac)
                # Map all known DOIs for this faculty
                for d in fac.dois or []:
                    if not d: continue
                    doi_to_faculty_ids.setdefault(d.lower(), set()).add(fac.id)

            # 2) PAPERS
            for j, rec in enumerate(papers_json, 1):
                if max_n and j > max_n:
                    break

                doi = (rec.get("doi") or rec.get("id") or "").strip()
                title_value = rec.get("title")
                if isinstance(title_value, list):
                    title = " ".join(str(t) for t in title_value)
                else:
                    title = str(title_value or "").strip()
                if not doi or not title:
                    continue

                paper, made = Paper.objects.get_or_create(doi=doi, defaults={"title": title[:500]})
                if made:
                    created_pap += 1
                else:
                    updated_pap += 1

                paper.title    = title[:500]
                paper.abstract = rec.get("abstract") or None
                paper.journal  = rec.get("journal") or None
                paper.tc_count = rec.get("tc_count") or 0

                # dates
                paper.date_published_online = parse_date_any(rec.get("date_published_online"))
                paper.date_published_print  = parse_date_any(rec.get("date_published_print"))

                # urls
                paper.license_url = rec.get("license_url") or None
                paper.download_url= rec.get("download_url") or None
                paper.url         = rec.get("url") or None

                # themes + merged keywords
                paper.themes   = as_list(rec.get("themes"))
                paper.keywords = merge_keywords_from_record(rec)

                paper.save()

            # 3) LINKING (two strategies for robustness)

            # (a) By DOI crosswalk from Faculty.dois
            doi_lower_to_paper = {p.doi.lower(): p for p in Paper.objects.all()}
            for dlower, fac_ids in doi_to_faculty_ids.items():
                p = doi_lower_to_paper.get(dlower)
                if not p:
                    continue
                for fid in fac_ids:
                    fac = Faculty.objects.filter(id=fid).first()
                    if not fac: continue
                    p.authors.add(fac)
                    # Ensure a pending authorship record
                    PaperAuthorship.objects.get_or_create(paper=p, faculty=fac, defaults={"status":"pending"})
                    linked += 1

            # (b) By article faculty_members names (if present)
            for rec in papers_json:
                doi_value = rec.get("doi") or rec.get("id")
                if isinstance(doi_value, list):
                    doi = str(doi_value[0]) if doi_value else ""
                else:
                    doi = str(doi_value or "").strip()
                if not doi:
                    continue
                p = Paper.objects.filter(doi=doi).first()
                if not p:
                    continue

                names = as_list(rec.get("faculty_members"))
                for nm in names:
                    key = (nm or "").strip().lower()
                    if not key:
                        continue
                    for fac in fac_by_name.get(key, []):
                        p.authors.add(fac)
                        PaperAuthorship.objects.get_or_create(paper=p, faculty=fac, defaults={"status":"pending"})
                        linked += 1

            if dry:
                raise CommandError("Dry run complete — rolled back.")

        self.stdout.write(self.style.SUCCESS(
            f"DONE. faculty: created={created_fac}, updated={updated_fac} | "
            f"papers: created={created_pap}, updated={updated_pap} | links={linked}"
        ))


class _NullCtx:
    def __enter__(self): return None
    def __exit__(self, *exc): return False