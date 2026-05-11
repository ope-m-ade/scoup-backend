# views/__init__.py — re-exports only.
# All logic lives in the sub-modules alongside this file.
# urls.py does `from . import views` and `from .views import ...` —
# both work identically whether views is a file or a package.

from .utils import (
    _normalize_keyword_list,
    _year_from_dates,
    _normalize_paper_link,
    _generate_signup_faculty_id,
    _full_name,
    _keywords_for_matching,
    _merge_unique_list,
    _has_salisbury_department,
    _faculty_school_names,
    _faculty_department_names,
    _is_confirmed_su_faculty,
    _first_non_empty,
    _clean_abstract,
    _title_similarity,
    _email_available_for_faculty,
    _get_request_faculty,
)
from .search import (
    public_search_data,
    network_discovery,
    semantic_paper_search,
    unified_search,
    query_expansions,
    categories_list,
    category_detail,
)
from .faculty import (
    home,
    FacultyListCreateView,
    FacultyDetailView,
    faculty_signup,
    faculty_me,
    admin_me,
    faculty_me_suggestions,
    approve_faculty_suggestion,
    faculty_suggestion_preview,
    reject_faculty_suggestion,
    FacultyPhotoUploadView,
)
from .paper import (
    PaperListCreateView,
    PaperDetailView,
    ProjectListCreateView,
    ProjectDetailView,
    PatentListCreateView,
    PatentDetailView,
    FacultyUploadCVPapers,
    bulk_publish_papers,
    extract_abstract_from_pdf,
    generate_faculty_bio,
    generate_research_interests,
    generate_faculty_keywords,
    generate_paper_keywords,
    paper_search_external,
    confirm_cv_items,
)
from .contact import (
    contact_team_list,
    admin_contact_team_list,
    admin_contact_team_detail,
    contact_settings,
    admin_contact_settings,
    admin_contact_team_photo_upload,
)
from .admin import (
    IsAdminUser,
    AdminSchoolListCreateView,
    AdminSchoolDetailView,
    AdminDepartmentListCreateView,
    AdminDepartmentDetailView,
    admin_faculty_list,
    admin_faculty_detail,
    admin_approve_faculty,
    admin_reject_faculty,
    admin_paper_list,
    admin_paper_detail,
    admin_project_detail,
    admin_patent_detail,
)
from .auth import (
    forgot_password,
    reset_password,
    change_password,
    send_institutional_otp,
    verify_institutional_otp,
)
