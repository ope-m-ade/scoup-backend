import re
import uuid

from ..models import Faculty


def _normalize_keyword_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _year_from_dates(*dates):
    for dt in dates:
        if dt:
            return dt.year
    return None


def _normalize_paper_link(download_url, url, license_url, doi):
    for value in (download_url, url, license_url):
        # Strip outer whitespace, then remove any embedded whitespace (spaces, tabs, newlines in URLs are invalid)
        clean_value = re.sub(r'\s+', '', str(value or "").strip())
        if not clean_value:
            continue
        if clean_value.startswith(("http://", "https://")):
            return clean_value

    # Only fall back to DOI if it looks complete (has a slash after the prefix e.g. 10.xxxx/yyyy)
    clean_doi = re.sub(r'\s+', '', str(doi or "").strip())
    if clean_doi and clean_doi.count("/") >= 1:
        parts = clean_doi.split("/", 1)
        if len(parts[1]) > 3:  # suffix must be more than 3 characters
            return f"https://doi.org/{clean_doi}"

    return ""


def _generate_signup_faculty_id():
    generated_faculty_id = f"SIGNUP-{uuid.uuid4().hex[:12]}"
    while Faculty.objects.filter(faculty_id=generated_faculty_id).exists():
        generated_faculty_id = f"SIGNUP-{uuid.uuid4().hex[:12]}"
    return generated_faculty_id


def _full_name(first_name, last_name, fallback=""):
    return f"{(first_name or '').strip()} {(last_name or '').strip()}".strip() or fallback


def _keywords_for_matching(faculty):
    merged = (
        _normalize_keyword_list(getattr(faculty, "keywords", None))
        + _normalize_keyword_list(getattr(faculty, "faculty_keywords", None))
        + _normalize_keyword_list(getattr(faculty, "ai_keywords", None))
    )
    return {item.lower() for item in merged if item}


def _merge_unique_list(*values):
    ordered = []
    seen = set()
    for value in values:
        items = _normalize_keyword_list(value)
        for item in items:
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(item)
    return ordered


def _has_salisbury_department(faculty):
    return bool(_faculty_department_names(faculty) or _faculty_school_names(faculty))


def _faculty_school_names(faculty):
    names = []
    if getattr(faculty, "primary_school_id", None):
        names.append(faculty.primary_school.name)
    names.extend([school.name for school in faculty.schools.all()])
    return _merge_unique_list(names)


def _faculty_department_names(faculty):
    names = []
    if getattr(faculty, "primary_department_id", None):
        names.append(faculty.primary_department.name)
    names.extend([department.name for department in faculty.departments.all()])
    return _merge_unique_list(names)


def _is_confirmed_su_faculty(faculty):
    if getattr(faculty, "confirmed_su_faculty", False):
        return True
    if getattr(faculty, "review_status", "") == "confirmed_su":
        return True
    if faculty.user_id and faculty.is_approved and faculty.profile_visibility:
        return True
    email = (faculty.email or "").strip().lower()
    if email.endswith("@salisbury.edu") or email.endswith("@gulls.salisbury.edu"):
        return True
    return bool(_faculty_department_names(faculty) or _faculty_school_names(faculty))


def _first_non_empty(*values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            trimmed = value.strip()
            if trimmed:
                return trimmed
            continue
        return value
    return None


def _clean_abstract(raw: str) -> str:
    """Strip JATS/HTML tags and normalise whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()


def _title_similarity(a: str, b: str) -> float:
    """Simple word-overlap ratio to verify a search result matches our paper."""
    a_words = set(re.sub(r"[^a-z0-9 ]", "", a.lower()).split())
    b_words = set(re.sub(r"[^a-z0-9 ]", "", b.lower()).split())
    if not a_words or not b_words:
        return 0.0
    return len(a_words & b_words) / max(len(a_words), len(b_words))


def _email_available_for_faculty(email, internal_id, external_id):
    if not email:
        return False
    return not Faculty.objects.filter(email__iexact=email).exclude(
        id__in=[internal_id, external_id]
    ).exists()


def _get_request_faculty(user, create_if_missing=False):
    faculty = Faculty.objects.filter(user=user).first()
    if faculty:
        return faculty

    email = (user.email or "").strip()
    if not create_if_missing:
        return None

    email_in_use_elsewhere = (
        bool(email)
        and Faculty.objects.filter(email__iexact=email).exclude(user=user).exists()
    )
    safe_email = None if email_in_use_elsewhere else (email.lower() if email else None)

    return Faculty.objects.create(
        user=user,
        faculty_id=_generate_signup_faculty_id(),
        email=safe_email,
        first_name=user.first_name or "",
        last_name=user.last_name or "",
        name=_full_name(user.first_name, user.last_name, user.username),
        is_approved=True,
        profile_visibility=True,
    )
