from django.test import SimpleTestCase

from academic.affiliations import (
    extract_salisbury_departments,
    extract_salisbury_schools,
    sanitize_department_label,
)


class AffiliationExtractionTests(SimpleTestCase):
    def test_extracts_clean_salisbury_department(self):
        values = [
            'Department of Biological Sciences, Salisbury University, Salisbury, MD 21801',
            '1 Salisbury University',
            'Department of Pharmacology, Case Western Reserve University, Cleveland, Ohio.',
        ]
        self.assertEqual(
            extract_salisbury_departments(values),
            ['Department of Biological Sciences'],
        )

    def test_extracts_department_from_longer_affiliation_text(self):
        value = (
            'Assistant Professor of Management. Department of Management and Marketing. '
            'Franklin P. Perdue School of Business, Salisbury University, MD, USA'
        )
        self.assertEqual(
            extract_salisbury_departments([value]),
            ['Department of Management and Marketing'],
        )

    def test_extracts_clean_school(self):
        value = '272 FH Department of Communication Arts, Fulton School of Liberal Arts, Salisbury University'
        self.assertEqual(
            extract_salisbury_schools([value]),
            ['Fulton School of Liberal Arts'],
        )

    def test_sanitize_rejects_non_department_noise(self):
        self.assertEqual(sanitize_department_label('Salisbury University'), '')
