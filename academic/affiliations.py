import html
import re


SALISBURY_UNIVERSITY_RE = re.compile(r"\bsalisbury university\b", re.I)
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"\+?\d[\d(). -]{6,}\d")
JOB_TITLE_RE = re.compile(
    r"^(?:assistant|associate|adjunct|clinical|visiting|emeritus|distinguished|board certified|director|dean|chair|professor|lecturer|instructor|ceo)\b",
    re.I,
)
NOISY_ENTITY_RE = re.compile(
    r"\b(?:university|medical center|hospital|institute|center|centre|laboratory|lab|consulting|inc\.?|llc|corporation|company|service)\b",
    re.I,
)

STANDARD_SCHOOLS = [
    ("College of Health and Human Services", "CHHS"),
    ("Fulton School of Liberal Arts", "FSLA"),
    ("Henson School of Science & Technology", "HSST"),
    ("Perdue School of Business", "PSB"),
    ("Seidel School of Education", "SSE"),
    ("Clarke Honors College", "CHC"),
]

DEPARTMENT_ALIAS_RULES = [
    (re.compile(r"^psychology department$", re.I), "Department of Psychology"),
    (re.compile(r"^social work department$", re.I), "Department of Social Work"),
    (re.compile(r"^department of social work$", re.I), "Department of Social Work"),
    (re.compile(r"^biology$", re.I), "Department of Biological Sciences"),
    (re.compile(r"^department of biology$", re.I), "Department of Biological Sciences"),
    (re.compile(r"^biological sciences$", re.I), "Department of Biological Sciences"),
    (re.compile(r"^chemistry$", re.I), "Department of Chemistry"),
    (re.compile(r"^communication$", re.I), "Department of Communication"),
    (re.compile(r"^communication arts$", re.I), "Department of Communication Arts"),
    (re.compile(r"^department of communication arts$", re.I), "Department of Communication Arts"),
    (re.compile(r"^computer science$", re.I), "Department of Computer Science"),
    (re.compile(r"^conflict analysis and dispute resolution$", re.I), "Department of Conflict Analysis and Dispute Resolution"),
    (re.compile(r"^department of geography\s*&\s*geosciences$", re.I), "Department of Geography and Geosciences"),
    (re.compile(r"^geography\s*(?:&|and)\s*geosciences$", re.I), "Department of Geography and Geosciences"),
    (re.compile(r"^management$", re.I), "Department of Management"),
    (re.compile(r"^management and marketing$", re.I), "Department of Management and Marketing"),
    (re.compile(r"^economics and finance$", re.I), "Department of Economics and Finance"),
    (re.compile(r"^accounting$", re.I), "Department of Accounting"),
    (re.compile(r"^information and decision sciences$", re.I), "Department of Information and Decision Sciences"),
    (re.compile(r"^nursing$", re.I), "Department of Nursing"),
    (re.compile(r"^exercise science$", re.I), "Department of Exercise Science"),
    (re.compile(r"^exercise physiology research lab$", re.I), "Department of Exercise Science"),
    (re.compile(r"^health and sport sciences$", re.I), "Department of Health and Sport Sciences"),
    (re.compile(r"^department of health and sport sciences$", re.I), "Department of Health and Sport Sciences"),
    (re.compile(r"^department of math and computer science$", re.I), "Department of Mathematics and Computer Science"),
    (re.compile(r"^mathematical sciences$", re.I), "Department of Mathematical Sciences"),
    (re.compile(r"^department of mathematical sciences$", re.I), "Department of Mathematical Sciences"),
    (re.compile(r"^early and elementary education$", re.I), "Department of Early and Elementary Education"),
    (re.compile(r"^department of early and elementary education$", re.I), "Department of Early and Elementary Education"),
    (re.compile(r"^secondary and physical education$", re.I), "Department of Secondary and Physical Education"),
    (re.compile(r"^athletic training education program$", re.I), "Athletic Training Education Program"),
]

SCHOOL_ALIAS_RULES = [
    (re.compile(r"^college of health and human services$", re.I), "College of Health and Human Services"),
    (re.compile(r"^school of health sciences?$", re.I), "College of Health and Human Services"),
    (re.compile(r"^school of nursing\b.*$", re.I), "College of Health and Human Services"),
    (re.compile(r"^school of social work$", re.I), "College of Health and Human Services"),
    (re.compile(r"^fulton school of liberal arts$", re.I), "Fulton School of Liberal Arts"),
    (re.compile(r"^school of liberal arts\b.*$", re.I), "Fulton School of Liberal Arts"),
    (re.compile(r"^henson school of science\s*(?:&|and)\s*technology$", re.I), "Henson School of Science & Technology"),
    (re.compile(r"^school of science\s*(?:&|and)\s*technology$", re.I), "Henson School of Science & Technology"),
    (re.compile(r"^school of technology$", re.I), "Henson School of Science & Technology"),
    (re.compile(r"^perdue school of business$", re.I), "Perdue School of Business"),
    (re.compile(r"^franklin p\.?\s*perdue school of business$", re.I), "Perdue School of Business"),
    (re.compile(r"^school of business\b.*$", re.I), "Perdue School of Business"),
    (re.compile(r"^seidel school of education$", re.I), "Seidel School of Education"),
    (re.compile(r"^school of education\b.*$", re.I), "Seidel School of Education"),
    (re.compile(r"^school of education and professional studies$", re.I), "Seidel School of Education"),
    (re.compile(r"^clarke honors college\b.*$", re.I), "Clarke Honors College"),
]

DEPARTMENT_TO_SCHOOL = {
    "Department of Biological Sciences": "Henson School of Science & Technology",
    "Department of Chemistry": "Henson School of Science & Technology",
    "Department of Computer Science": "Henson School of Science & Technology",
    "Department of Geography and Geosciences": "Henson School of Science & Technology",
    "Department of Mathematical Sciences": "Henson School of Science & Technology",
    "Department of Mathematics and Computer Science": "Henson School of Science & Technology",
    "Department of Physics": "Henson School of Science & Technology",
    "Department of Environmental Studies": "Henson School of Science & Technology",
    "Department of Communication": "Fulton School of Liberal Arts",
    "Department of Communication Arts": "Fulton School of Liberal Arts",
    "Department of Conflict Analysis and Dispute Resolution": "Fulton School of Liberal Arts",
    "Department of English": "Fulton School of Liberal Arts",
    "Department of History": "Fulton School of Liberal Arts",
    "Department of Modern Languages": "Fulton School of Liberal Arts",
    "Department of Philosophy": "Fulton School of Liberal Arts",
    "Department of Political Science": "Fulton School of Liberal Arts",
    "Department of Psychology": "Fulton School of Liberal Arts",
    "Department of Sociology": "Fulton School of Liberal Arts",
    "Department of Art": "Fulton School of Liberal Arts",
    "Department of Music": "Fulton School of Liberal Arts",
    "Department of Exercise Science": "College of Health and Human Services",
    "Department of Health and Sport Sciences": "College of Health and Human Services",
    "Department of Nursing": "College of Health and Human Services",
    "Department of Social Work": "College of Health and Human Services",
    "Athletic Training Education Program": "College of Health and Human Services",
    "Department of Management": "Perdue School of Business",
    "Department of Management and Marketing": "Perdue School of Business",
    "Department of Economics and Finance": "Perdue School of Business",
    "Department of Accounting": "Perdue School of Business",
    "Department of Information and Decision Sciences": "Perdue School of Business",
    "Department of Early and Elementary Education": "Seidel School of Education",
    "Department of Secondary and Physical Education": "Seidel School of Education",
    "Teacher Education Program": "Seidel School of Education",
}


def as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def clean_string(value):
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()


def unique_strings(values):
    seen = set()
    result = []
    for value in values:
        text = clean_string(value)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def is_school_label(value):
    return bool(re.search(r"\b(school|college)\b", str(value or ""), flags=re.I))


def normalize_department_label(raw):
    value = clean_string(raw)
    value = html.unescape(value)
    value = EMAIL_RE.sub(" ", value)
    value = PHONE_RE.sub(" ", value)
    value = re.sub(r"^\d+\s*", "", value)
    value = re.sub(r"\(.*?\)", " ", value)
    value = re.sub(r"\(.*$", " ", value)
    value = re.sub(r"[.;:,]+$", "", value)
    value = re.sub(r"\bsalisbury university\b", " ", value, flags=re.I)
    value = re.sub(r"\b(?:salisbury|maryland|md|usa|united states)\b", " ", value, flags=re.I)
    value = re.sub(r"\bHenson Science Hall\b.*$", " ", value, flags=re.I)
    value = re.sub(r"\bat\b.*$", " ", value, flags=re.I)
    value = re.sub(r"\b\d{4,}\b.*$", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" ,-")
    if not value or JOB_TITLE_RE.search(value):
        return ""

    for pattern, replacement in DEPARTMENT_ALIAS_RULES:
        if pattern.match(value):
            return replacement

    if re.match(r"^[A-Za-z&/ -]+ Department$", value, flags=re.I):
        value = f"Department of {re.sub(r'\s+Department$', '', value, flags=re.I).strip()}"

    return value[:150]


def is_valid_department_label(value):
    text = clean_string(value)
    if not text:
        return False
    if is_school_label(text):
        return False
    if not re.search(r"\b(department|program)\b", text, flags=re.I):
        return False
    if NOISY_ENTITY_RE.search(text) and not text.lower().startswith(("department of ", "program in ", "program of ")):
        return False
    if len(text.split()) > 10:
        return False
    return True


def sanitize_department_label(value):
    text = normalize_department_label(value)
    if not is_valid_department_label(text):
        return ""
    return text


def normalize_school_label(raw):
    value = normalize_department_label(raw)
    if not value:
        return ""
    for pattern, replacement in SCHOOL_ALIAS_RULES:
        if pattern.match(value):
            return replacement
    return value if is_school_label(value) else ""


def sanitize_school_label(value):
    return normalize_school_label(value)


def clean_affiliation(value):
    return clean_string(value).replace("&amp;", "and").replace("&#38;", "and")


def extract_salisbury_department(value):
    cleaned = clean_affiliation(value)
    if not cleaned or not SALISBURY_UNIVERSITY_RE.search(cleaned):
        return ""

    for pattern in (
        r"Department of [^,;.]+",
        r"Program in [^,;.]+",
        r"Program of [^,;.]+",
        r"[^,;.]+ Department",
        r"[^,;.]+ Program",
    ):
        match = re.search(pattern, cleaned, flags=re.I)
        if match:
            candidate = sanitize_department_label(match.group(0))
            if candidate:
                return candidate

    before_university = SALISBURY_UNIVERSITY_RE.split(cleaned)[0].strip(" ,-")
    if not before_university:
        return ""

    segments = [segment.strip() for segment in before_university.split(",") if segment.strip()]
    for segment in reversed(segments):
        candidate = sanitize_department_label(segment)
        if candidate:
            return candidate

    return sanitize_department_label(before_university)


def extract_salisbury_departments(values):
    return unique_strings(extract_salisbury_department(value) for value in as_list(values))


def extract_salisbury_school(value):
    cleaned = clean_affiliation(value)
    if not cleaned or not SALISBURY_UNIVERSITY_RE.search(cleaned):
        return ""

    for pattern in (r"School of [^,;.]+", r"College of [^,;.]+"):
        match = re.search(pattern, cleaned, flags=re.I)
        if match:
            candidate = sanitize_school_label(match.group(0))
            if candidate:
                return candidate

    before_university = SALISBURY_UNIVERSITY_RE.split(cleaned)[0].strip(" ,-")
    return sanitize_school_label(before_university)


def extract_salisbury_schools(values):
    return unique_strings(extract_salisbury_school(value) for value in as_list(values))


def extract_departments_from_faculty_affiliations(value):
    if not value or not isinstance(value, dict):
        return []
    return unique_strings(
        department
        for entry in value.values()
        for department in extract_salisbury_departments(entry)
    )


def extract_schools_from_faculty_affiliations(value):
    if not value or not isinstance(value, dict):
        return []
    return unique_strings(
        school
        for entry in value.values()
        for school in extract_salisbury_schools(entry)
    )


def department_school_name(department_name):
    return DEPARTMENT_TO_SCHOOL.get(clean_string(department_name), "")
