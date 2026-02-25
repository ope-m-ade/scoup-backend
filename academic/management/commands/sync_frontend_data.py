from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Import frontend JSON datasets (faculty_data.json + article_data.json) into DB."

    def add_arguments(self, parser):
        parser.add_argument(
            "--frontend-data-dir",
            default="",
            help="Path to frontend src/data directory. Defaults to sibling scoup-frontend-2.0/src/data.",
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete existing Faculty/Paper/PaperAuthorship rows before import.",
        )
        parser.add_argument(
            "--max",
            type=int,
            default=0,
            help="Import at most N papers (for testing).",
        )

    def handle(self, *args, **options):
        frontend_data_dir = options["frontend_data_dir"].strip()
        if frontend_data_dir:
            data_dir = Path(frontend_data_dir).expanduser().resolve()
        else:
            repo_root = Path(__file__).resolve().parents[4]
            data_dir = (repo_root / "scoup-frontend-2.0" / "src" / "data").resolve()

        faculty_path = data_dir / "faculty_data.json"
        papers_path = data_dir / "article_data.json"

        if not data_dir.exists():
            raise CommandError(f"Data directory not found: {data_dir}")
        if not faculty_path.exists():
            raise CommandError(f"Missing file: {faculty_path}")
        if not papers_path.exists():
            raise CommandError(f"Missing file: {papers_path}")

        self.stdout.write(f"Using data dir: {data_dir}")
        call_command(
            "import_full_dataset",
            faculty=str(faculty_path),
            papers=str(papers_path),
            reset=options["reset"],
            max=options["max"],
        )

