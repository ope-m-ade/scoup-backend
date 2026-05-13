import re
from django.core.exceptions import ValidationError


class SpecialCharacterValidator:
    """Require at least one special character (non-alphanumeric)."""

    def validate(self, password, user=None):
        if not re.search(r"[^a-zA-Z0-9]", password):
            raise ValidationError(
                "Password must contain at least one special character (e.g. !, @, #, $).",
                code="password_no_special",
            )

    def get_help_text(self):
        return "Your password must contain at least one special character."


class NumberValidator:
    """Require at least one digit."""

    def validate(self, password, user=None):
        if not re.search(r"[0-9]", password):
            raise ValidationError(
                "Password must contain at least one number.",
                code="password_no_number",
            )

    def get_help_text(self):
        return "Your password must contain at least one number."
