from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from academic.models import Paper
from academic.semantic import build_paper_semantic_text, create_embeddings


class Command(BaseCommand):
    help = "Generate and store semantic embeddings for papers."

    def add_arguments(self, parser):
        parser.add_argument(
            "--model",
            default="text-embedding-3-small",
            help="Embedding model name",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=50,
            help="How many papers to embed per API request",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Embed at most N papers",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-embed papers even when an embedding already exists",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Do not write embeddings, only report counts",
        )

    def handle(self, *args, **opts):
        model = str(opts["model"] or "text-embedding-3-small").strip()
        batch_size = max(1, int(opts["batch_size"] or 50))
        limit = max(0, int(opts["limit"] or 0))
        force = bool(opts["force"])
        dry_run = bool(opts["dry_run"])

        queryset = Paper.objects.all().prefetch_related("authors").order_by("id")
        if not force:
            queryset = queryset.filter(paper_embedding=[])

        papers = list(queryset)
        if limit:
            papers = papers[:limit]

        if not papers:
            self.stdout.write(self.style.WARNING("No papers require embedding."))
            return

        self.stdout.write(
            f"Embedding {len(papers)} papers with model={model} batch_size={batch_size}"
        )

        updated = 0
        for start in range(0, len(papers), batch_size):
            batch = papers[start : start + batch_size]
            texts = [build_paper_semantic_text(paper) for paper in batch]

            # If a paper has no semantic text, keep a small placeholder so indexing still works.
            texts = [text if text else "Untitled research paper." for text in texts]

            try:
                vectors = create_embeddings(texts, model=model)
            except RuntimeError as exc:
                raise CommandError(str(exc)) from exc
            except Exception as exc:
                raise CommandError(f"Embedding request failed: {exc}") from exc

            if len(vectors) != len(batch):
                raise CommandError(
                    f"Embedding count mismatch: vectors={len(vectors)} papers={len(batch)}"
                )

            for paper, vector in zip(batch, vectors):
                if dry_run:
                    continue
                paper.paper_embedding = vector
                paper.embedding_model = model
                paper.embedding_updated_at = timezone.now()
                paper.save(
                    update_fields=[
                        "paper_embedding",
                        "embedding_model",
                        "embedding_updated_at",
                    ]
                )
                updated += 1

            self.stdout.write(
                f"Processed batch {start + 1}-{start + len(batch)} / {len(papers)}"
            )

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"DRY RUN complete. Would embed {len(papers)} papers using {model}."
                )
            )
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"DONE. Embedded papers updated={updated} (requested={len(papers)})."
            )
        )
