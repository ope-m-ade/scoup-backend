import json
from datetime import date, datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from academic.models import Faculty, Paper, PaperAuthorship


def parse_date_any(value):
    """Accepts YYYY-mm-dd | YYYY-mm | YYYY and returns a date or None."""
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


def as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def as_dict(value):
    if isinstance(value, dict):
        return value
    return {}


def normalize_doi(value):
    """Normalize DOI-like values to a stable lowercase key form."""
    if isinstance(value, list):
        value = value[0] if value else ""
    s = str(value or "").strip()
    if not s:
        return ""

    lower = s.lower()
    if lower.startswith("https://doi.org/"):
        s = s[16:]
    elif lower.startswith("http://dx.doi.org/"):
        s = s[18:]
    elif lower.startswith("https://dx.doi.org/"):
        s = s[19:]

    return s.strip()


def dedupe_str_list(items):
    seen = set()
    output = []
    for item in items:
        if item is None:
            continue
        text = str(item).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(text)
    return output


def merge_keywords_from_record(article_rec, classified_rec=None):
    """Merge category hierarchy + themes + extra_context keywords for search."""
    merged = []
    for rec in (article_rec or {}, classified_rec or {}):
        for key in (
            "categories",
            "top_level_categories",
            "mid_level_categories",
            "low_level_categories",
            "themes",
        ):
            value = rec.get(key, [])
            if key == "categories" and isinstance(value, dict):
                merged.extend(as_list(value.get("top", [])))
                merged.extend(as_list(value.get("mid", [])))
                merged.extend(as_list(value.get("low", [])))
            else:
                merged.extend(as_list(value))

        extra_context = as_dict(rec.get("extra_context"))
        merged.extend(as_list(extra_context.get("keywords", [])))

    return dedupe_str_list(merged)


def parse_crossref_date(record, key):
    """Extract date string from Crossref date-parts."""
    parts = as_dict(record.get(key)).get("date-parts", [])
    if not parts or not parts[0]:
        return None
    return "-".join(str(x) for x in parts[0])


def index_by_doi(records):
    indexed = {}
    for rec in as_list(records):
        if not isinstance(rec, dict):
            continue
        doi = normalize_doi(
            rec.get("doi")
            or rec.get("DOI")
            or rec.get("id")
            or rec.get("_id")
        )
        if doi:
            indexed[doi.lower()] = rec
    return indexed


def first_non_empty(*values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned:
                return cleaned
            continue
        return value
    return None


def first_int(*values, default=0):
    for value in values:
        if value is None or value == "":
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return default


def extract_source_metadata(article_rec, classified_rec, raw_rec):
    """Curated high-value Crossref/source fields for analytics and drilldown."""
    meta = {}

    for rec in (classified_rec or {}, raw_rec or {}):
        if not isinstance(rec, dict):
            continue

        for key in (
            "publisher",
            "issue",
            "volume",
            "page",
            "type",
            "language",
            "prefix",
            "member",
            "source",
            "score",
            "reference-count",
            "references-count",
            "ISSN",
            "issn-type",
            "indexed",
            "created",
            "deposited",
            "issued",
            "published",
            "published-online",
            "published-print",
            "published-other",
            "journal-issue",
            "resource",
            "link",
            "license",
            "content-domain",
        ):
            value = rec.get(key)
            if value not in (None, "", [], {}):
                meta[key] = value

    # Persist article-layer context as well so FE can render without reparsing source blobs.
    article_context = {
        "category_urls": as_list(article_rec.get("category_urls")),
        "top_category_urls": as_list(article_rec.get("top_category_urls")),
        "mid_category_urls": as_list(article_rec.get("mid_category_urls")),
        "low_category_urls": as_list(article_rec.get("low_category_urls")),
    }
    article_context = {
        k: v for k, v in article_context.items() if v not in (None, "", [], {})
    }
    if article_context:
        meta["article_context"] = article_context

    return meta


def extract_engagement_metrics(article_rec, classified_rec, raw_rec):
    """Normalize engagement metrics emitted through extra_context payloads."""
    extra_context = as_dict(article_rec.get("extra_context"))
    if not extra_context:
        extra_context = as_dict(classified_rec.get("extra_context"))
    if not extra_context:
        extra_context = as_dict(raw_rec.get("extra_context"))

    if not extra_context:
        return {}

    metrics = {}

    metrics["total_views"] = first_int(
        extra_context.get("total_views"),
        extra_context.get("views"),
        extra_context.get("view_count"),
        default=0,
    )
    metrics["pdf_downloads"] = first_int(
        extra_context.get("pdf_downloads"),
        extra_context.get("downloads"),
        extra_context.get("download_count"),
        default=0,
    )
    metrics["citations"] = first_int(
        extra_context.get("citations"),
        extra_context.get("total_citations"),
        classified_rec.get("is-referenced-by-count") if classified_rec else None,
        raw_rec.get("is-referenced-by-count") if raw_rec else None,
        default=0,
    )

    ctx_date = first_non_empty(extra_context.get("date"))
    if ctx_date:
        metrics["date"] = str(ctx_date)

    metrics["raw"] = extra_context
    return metrics


class Command(BaseCommand):
    help = (
        "Import AcademicMetrics faculty + article JSON into Django models, "
        "preserving rich metadata and linking authorships."
    )

    def add_arguments(self, parser):
        parser.add_argument("--faculty", required=True, help="Path to faculty_data.json")
        parser.add_argument("--papers", required=True, help="Path to article_data.json")
        parser.add_argument(
            "--classified",
            required=False,
            help="Optional path to classified_data.json (for richer source metadata)",
        )
        parser.add_argument(
            "--raw",
            required=False,
            help="Optional path to raw_results.json or Crossref raw list",
        )
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete existing Faculty/Paper/PaperAuthorship first",
        )
        parser.add_argument(
            "--max", type=int, default=0, help="Import at most N papers (for testing)"
        )

    def handle(self, *args, **opts):
        fpath = Path(opts["faculty"])
        ppath = Path(opts["papers"])
        classified_path = Path(opts["classified"]) if opts.get("classified") else None
        raw_path = Path(opts["raw"]) if opts.get("raw") else None

        dry = opts["dry_run"]
        reset = opts["reset"]
        max_n = int(opts["max"] or 0)

        if not fpath.exists():
            raise CommandError(f"Faculty file not found: {fpath}")
        if not ppath.exists():
            raise CommandError(f"Papers file not found: {ppath}")
        if classified_path and not classified_path.exists():
            raise CommandError(f"Classified file not found: {classified_path}")
        if raw_path and not raw_path.exists():
            raise CommandError(f"Raw file not found: {raw_path}")

        try:
            faculty_json = json.loads(fpath.read_text())
            papers_json = json.loads(ppath.read_text())
            classified_json = (
                json.loads(classified_path.read_text()) if classified_path else []
            )
            raw_json = json.loads(raw_path.read_text()) if raw_path else []
        except Exception as exc:
            raise CommandError(f"Failed to parse JSON: {exc}")

        if not isinstance(faculty_json, list) or not isinstance(papers_json, list):
            raise CommandError("Both faculty/papers JSON files must be top-level lists")
        if not isinstance(classified_json, list):
            raise CommandError("Classified JSON must be a top-level list")
        if not isinstance(raw_json, list):
            raise CommandError("Raw JSON must be a top-level list")

        classified_by_doi = index_by_doi(classified_json)
        raw_by_doi = index_by_doi(raw_json)

        self.stdout.write(
            f"Loaded source files: faculty={len(faculty_json)} papers={len(papers_json)} "
            f"classified={len(classified_json)} raw={len(raw_json)}"
        )

        if reset and not dry:
            PaperAuthorship.objects.all().delete()
            Paper.objects.all().delete()
            Faculty.objects.all().delete()
            self.stdout.write(
                self.style.WARNING(
                    "Existing Faculty/Paper/PaperAuthorship deleted (per --reset)."
                )
            )

        created_fac = updated_fac = 0
        created_pap = updated_pap = 0
        linked = 0

        ctx = transaction.atomic() if not dry else _NullCtx()
        with ctx:
            # 1) FACULTY
            for rec in faculty_json:
                fid = str(rec.get("_id") or "").strip()
                name = str(rec.get("name") or "").strip()
                if not fid or not name:
                    continue

                fac, made = Faculty.objects.get_or_create(
                    faculty_id=fid,
                    defaults={"name": name},
                )
                if made:
                    created_fac += 1
                else:
                    updated_fac += 1

                fac.name = name
                fac.is_approved = True
                fac.profile_visibility = True
                fac.total_citations = first_int(rec.get("total_citations"), default=0)
                fac.article_count = first_int(rec.get("article_count"), default=0)
                try:
                    fac.average_citations = float(rec.get("average_citations") or 0.0)
                except (TypeError, ValueError):
                    fac.average_citations = 0.0

                fac.department_affiliations = dedupe_str_list(
                    as_list(rec.get("department_affiliations"))
                )
                fac.dois = dedupe_str_list(as_list(rec.get("dois")))
                fac.titles = dedupe_str_list(as_list(rec.get("titles")))
                fac.categories = dedupe_str_list(as_list(rec.get("categories")))
                fac.top_level_categories = dedupe_str_list(
                    as_list(rec.get("top_level_categories"))
                )
                fac.mid_level_categories = dedupe_str_list(
                    as_list(rec.get("mid_level_categories"))
                )
                fac.low_level_categories = dedupe_str_list(
                    as_list(rec.get("low_level_categories"))
                )
                fac.category_urls = dedupe_str_list(as_list(rec.get("category_urls")))
                fac.top_category_urls = dedupe_str_list(
                    as_list(rec.get("top_category_urls"))
                )
                fac.mid_category_urls = dedupe_str_list(
                    as_list(rec.get("mid_category_urls"))
                )
                fac.low_category_urls = dedupe_str_list(
                    as_list(rec.get("low_category_urls"))
                )
                fac.themes = dedupe_str_list(as_list(rec.get("themes")))
                fac.journals = dedupe_str_list(as_list(rec.get("journals")))
                fac.keywords = merge_keywords_from_record(rec)

                fac.source_profile = {
                    k: v
                    for k, v in {
                        "source_id": rec.get("_id"),
                        "category_urls": as_list(rec.get("category_urls")),
                        "top_category_urls": as_list(rec.get("top_category_urls")),
                        "mid_category_urls": as_list(rec.get("mid_category_urls")),
                        "low_category_urls": as_list(rec.get("low_category_urls")),
                    }.items()
                    if v not in (None, "", [], {})
                }

                if not fac.first_name or not fac.last_name:
                    parts = name.split()
                    if len(parts) == 1:
                        fac.last_name = parts[0]
                    elif len(parts) > 1:
                        fac.first_name = " ".join(parts[:-1])
                        fac.last_name = parts[-1]

                fac.save()

            # Build lookup maps for author linking
            fac_by_name = {}
            doi_to_faculty_ids = {}
            for fac in Faculty.objects.all():
                key = (fac.name or "").strip().lower()
                if key:
                    fac_by_name.setdefault(key, []).append(fac)

                for doi in fac.dois or []:
                    dnorm = normalize_doi(doi)
                    if not dnorm:
                        continue
                    doi_to_faculty_ids.setdefault(dnorm.lower(), set()).add(fac.id)

            # 2) PAPERS
            for j, article_rec in enumerate(papers_json, 1):
                if max_n and j > max_n:
                    break

                doi = normalize_doi(
                    article_rec.get("doi")
                    or article_rec.get("id")
                    or article_rec.get("_id")
                )
                if not doi:
                    continue

                classified_rec = classified_by_doi.get(doi.lower(), {})
                raw_rec = raw_by_doi.get(doi.lower(), {})

                title_value = first_non_empty(
                    article_rec.get("title"),
                    classified_rec.get("title"),
                    raw_rec.get("title"),
                )
                if isinstance(title_value, list):
                    title = " ".join(str(t) for t in title_value if str(t).strip())
                else:
                    title = str(title_value or "").strip()
                if not title:
                    continue

                paper, made = Paper.objects.get_or_create(
                    doi=doi,
                    defaults={"title": title[:500]},
                )
                if made:
                    created_pap += 1
                else:
                    updated_pap += 1

                paper.title = title[:500]
                paper.abstract = first_non_empty(
                    article_rec.get("abstract"),
                    classified_rec.get("abstract"),
                    raw_rec.get("abstract"),
                )

                journal_value = first_non_empty(
                    article_rec.get("journal"),
                    as_list(classified_rec.get("container-title"))[0]
                    if as_list(classified_rec.get("container-title"))
                    else None,
                    as_list(raw_rec.get("container-title"))[0]
                    if as_list(raw_rec.get("container-title"))
                    else None,
                )
                paper.journal = journal_value

                paper.tc_count = first_int(
                    article_rec.get("tc_count"),
                    classified_rec.get("is-referenced-by-count"),
                    raw_rec.get("is-referenced-by-count"),
                    default=0,
                )

                online_date = first_non_empty(
                    article_rec.get("date_published_online"),
                    parse_crossref_date(classified_rec, "published-online"),
                    parse_crossref_date(raw_rec, "published-online"),
                )
                print_date = first_non_empty(
                    article_rec.get("date_published_print"),
                    parse_crossref_date(classified_rec, "published-print"),
                    parse_crossref_date(raw_rec, "published-print"),
                )

                paper.date_published_online = parse_date_any(online_date)
                paper.date_published_print = parse_date_any(print_date)

                license_url = first_non_empty(
                    article_rec.get("license_url"),
                    as_list(classified_rec.get("license"))[0].get("URL")
                    if as_list(classified_rec.get("license"))
                    and isinstance(as_list(classified_rec.get("license"))[0], dict)
                    else None,
                    as_list(raw_rec.get("license"))[0].get("URL")
                    if as_list(raw_rec.get("license"))
                    and isinstance(as_list(raw_rec.get("license"))[0], dict)
                    else None,
                )
                download_url = first_non_empty(
                    article_rec.get("download_url"),
                    classified_rec.get("URL"),
                    raw_rec.get("URL"),
                )
                primary_url = first_non_empty(
                    article_rec.get("url"),
                    as_dict(as_dict(classified_rec.get("resource")).get("primary")).get(
                        "URL"
                    ),
                    as_dict(as_dict(raw_rec.get("resource")).get("primary")).get(
                        "URL"
                    ),
                    classified_rec.get("URL"),
                    raw_rec.get("URL"),
                )

                paper.license_url = license_url
                paper.download_url = download_url
                paper.url = primary_url

                top_cats = dedupe_str_list(
                    as_list(article_rec.get("top_level_categories"))
                    or as_list(as_dict(classified_rec.get("categories")).get("top"))
                    or as_list(as_dict(raw_rec.get("categories")).get("top"))
                )
                mid_cats = dedupe_str_list(
                    as_list(article_rec.get("mid_level_categories"))
                    or as_list(as_dict(classified_rec.get("categories")).get("mid"))
                    or as_list(as_dict(raw_rec.get("categories")).get("mid"))
                )
                low_cats = dedupe_str_list(
                    as_list(article_rec.get("low_level_categories"))
                    or as_list(as_dict(classified_rec.get("categories")).get("low"))
                    or as_list(as_dict(raw_rec.get("categories")).get("low"))
                )

                paper.top_level_categories = top_cats
                paper.mid_level_categories = mid_cats
                paper.low_level_categories = low_cats
                paper.category_urls = dedupe_str_list(as_list(article_rec.get("category_urls")))
                paper.top_category_urls = dedupe_str_list(
                    as_list(article_rec.get("top_category_urls"))
                )
                paper.mid_category_urls = dedupe_str_list(
                    as_list(article_rec.get("mid_category_urls"))
                )
                paper.low_category_urls = dedupe_str_list(
                    as_list(article_rec.get("low_category_urls"))
                )

                paper.themes = dedupe_str_list(
                    as_list(article_rec.get("themes"))
                    or as_list(classified_rec.get("themes"))
                    or as_list(raw_rec.get("themes"))
                )
                paper.keywords = merge_keywords_from_record(article_rec, classified_rec)

                paper.faculty_members = dedupe_str_list(as_list(article_rec.get("faculty_members")))
                paper.faculty_affiliations = as_dict(article_rec.get("faculty_affiliations"))

                paper.source_metadata = extract_source_metadata(
                    article_rec, classified_rec, raw_rec
                )
                paper.engagement_metrics = extract_engagement_metrics(
                    article_rec, classified_rec, raw_rec
                )
                paper.source_record = classified_rec or raw_rec or {}

                paper.save()

            # 3) LINKING

            # (a) By DOI crosswalk from Faculty.dois
            doi_lower_to_paper = {p.doi.lower(): p for p in Paper.objects.all()}
            for dlower, fac_ids in doi_to_faculty_ids.items():
                paper = doi_lower_to_paper.get(dlower)
                if not paper:
                    continue
                for faculty_id in fac_ids:
                    fac = Faculty.objects.filter(id=faculty_id).first()
                    if not fac:
                        continue
                    paper.authors.add(fac)
                    PaperAuthorship.objects.get_or_create(
                        paper=paper,
                        faculty=fac,
                        defaults={"status": "pending"},
                    )
                    linked += 1

            # (b) By article faculty_members names (if present)
            for article_rec in papers_json:
                doi = normalize_doi(
                    article_rec.get("doi")
                    or article_rec.get("id")
                    or article_rec.get("_id")
                )
                if not doi:
                    continue

                paper = Paper.objects.filter(doi=doi).first()
                if not paper:
                    continue

                for nm in as_list(article_rec.get("faculty_members")):
                    key = str(nm or "").strip().lower()
                    if not key:
                        continue
                    for fac in fac_by_name.get(key, []):
                        paper.authors.add(fac)
                        PaperAuthorship.objects.get_or_create(
                            paper=paper,
                            faculty=fac,
                            defaults={"status": "pending"},
                        )
                        linked += 1

            if dry:
                raise CommandError("Dry run complete - rolled back.")

        self.stdout.write(
            self.style.SUCCESS(
                f"DONE. faculty: created={created_fac}, updated={updated_fac} | "
                f"papers: created={created_pap}, updated={updated_pap} | links={linked}"
            )
        )


class _NullCtx:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False
