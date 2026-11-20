"""
Summary Service - Shared File Processing Logic

This module provides the core file processing logic shared between the
sync pipeline and standalone summarization commands. It handles the complete
flow: content extraction -> summarization -> embedding -> persistence.

Key responsibilities:
- Read and verify file content from local filesystem
- Acquire processing claims for concurrent worker coordination
- Reuse existing summaries/embeddings when content unchanged
- Generate new summaries via LLM and embeddings via embedding model
- Validate summaries for privacy (no source echoes, no credentials)
- Persist results to Supabase and update local journal
- Handle failures with proper state transitions and error recording

The service is designed for:
- Idempotency: Re-running on same content produces same results
- Concurrency: Multiple workers can process different files safely
- Recovery: Interrupted processing can resume without data loss
- Privacy: Source content never persisted; summaries validated

Used by: sync_service.py (batch processing), CLI summarize command
"""

import logging
from pathlib import Path

from searchify.ai.embeddings import Embeddings, validate_embedding
from searchify.ai.ollama_client import check_processing_runtime, format_processing_error
from searchify.ai.summarizer import Summarizer
from searchify.ai.summary_validation import validate_summary
from searchify.config.settings import settings
from searchify.core.extractor import extract_content
from searchify.core.file_registry import FileRegistry
from searchify.core.scanner import root_is_unredirected, signature
from searchify.indexer import LocalContentError, calculate_hash, resolve_indexed_file
from searchify.reporting.sync_report import describe_error, make_outcome
from searchify.services.processing_claim import ProcessingClaim, recover_claim

logger = logging.getLogger(__name__)


class StaleContentError(RuntimeError):
    """
    Raised when file content has changed or become unavailable during processing.

    This indicates the file state is no longer valid for the current operation,
    distinct from permanent failures. Processing should be retried after
    re-scanning.
    """


class SummaryService:
    """
    Processes files through the complete summarization and embedding pipeline.

    This service encapsulates all steps required to transform a file into
    a searchable summary with vector embedding. It handles content verification,
    claim acquisition, AI generation, validation, and persistence.

    Attributes:
        files: FileRepository for Supabase file metadata
        summaries: SummaryRepository for Supabase summary/embedding storage
        summarizer: Summarizer instance for LLM summarization
        embeddings: Embeddings instance for vector generation
        db_path: Path to local SQLite database
    """

    def __init__(
        self, files=None, summaries=None, summarizer=None, embeddings=None, db_path=None
    ):
        """
        Initialize summary service with optional dependency injection.

        Args:
            files: FileRepository instance (created if None)
            summaries: SummaryRepository instance (created if None)
            summarizer: Summarizer instance (created if None)
            embeddings: Embeddings instance (created if None)
            db_path: Local database path (default: settings.LOCAL_DB_PATH)
        """
        if files is None or summaries is None:
            from searchify.repositories.files import FileRepository
            from searchify.repositories.summaries import SummaryRepository

            files = files if files is not None else FileRepository(db_path=db_path)
            summaries = summaries if summaries is not None else SummaryRepository()
        self.files, self.summaries = files, summaries
        self.summarizer = summarizer or Summarizer()
        self.embeddings = embeddings or Embeddings()
        self.db_path = Path(db_path or settings.LOCAL_DB_PATH)
        self.outcomes = []

    def _read_content(self, file):
        """
        Read and verify file content from local filesystem.

        Performs multiple validation steps:
        1. Verify directory exists and is enabled
        2. Verify directory root boundary not redirected
        3. Resolve file path using content hash (prevents TOCTOU)
        4. Extract content with size/extension validation
        5. Verify file signature unchanged during read
        6. Verify content hash matches expected value

        Args:
            file: File dict with directory_id, relative_path, content_hash

        Returns:
            File content as string

        Raises:
            StaleContentError: If any validation fails
        """
        directory = self.files.get_directory(file["directory_id"])
        if not directory or not directory.get("enabled", True):
            raise StaleContentError("Directory metadata unavailable.")
        if not root_is_unredirected(Path(directory["path"]).expanduser().absolute()):
            raise StaleContentError("Directory metadata unavailable.")
        try:
            # Resolve file using content hash - validates identity before reading
            path = resolve_indexed_file(
                self.db_path,
                directory["path"],
                file["relative_path"],
                file["content_hash"],
            )
            registry = FileRegistry(self.db_path)
            metadata = registry.metadata()[str(path)]
            rules = registry.rules(metadata["root_id"])
            file.update(
                path=str(path),
                root_id=metadata["root_id"],
                max_size=rules.max_size,
                allowed_extensions=rules.extensions,
            )
            before = signature(path.stat())
            content = extract_content(
                path,
                file.get("max_size", settings.MAX_FILE_SIZE),
                file.get("allowed_extensions", settings.SUPPORTED_EXTENSIONS),
                strict=True,
            )
            # Verify file didn't change during read
            if before != signature(path.stat()):
                raise LocalContentError("Local content hash mismatch.")
            if not content.strip():
                raise LocalContentError("File has no extractable content.")
            if calculate_hash(content) != file["content_hash"]:
                raise LocalContentError("Local content hash mismatch.")
            # Store verified state for later re-verification
            file["signature"] = before
            file["_verified_signature"] = before
            file["_verified_path"] = path
            return content
        except LocalContentError as error:
            raise StaleContentError(str(error)) from None

    def _check_current(self, file):
        """
        Verify file state hasn't changed since content was read.

        Checks:
        1. Local file signature still matches (not modified)
        2. Remote file metadata still exists and matches content hash
        3. Remote file status still eligible
        3. Processing claim hasn't been superseded

        Args:
            file: File dict with _verified_path, _verified_signature, id, content_hash

        Raises:
            StaleContentError: If any check fails
        """
        path = file.get("_verified_path")
        try:
            stable = (
                path is not None
                and not path.is_symlink()
                and signature(path.stat()) == file.get("_verified_signature")
            )
        except OSError:
            stable = False
        if not stable:
            self._read_content(file)
        try:
            current = self.files.get_file(file["id"])
        except Exception as error:
            error.processing_stage = "persistence"
            raise
        message = None
        if not current:
            message = "Remote file metadata is unavailable."
        elif current["content_hash"] != file["content_hash"]:
            message = "Remote content hash changed."
        elif current.get("status") not in {"indexed", "metadata_only"}:
            message = "Remote file is no longer eligible."
        elif (
            file.get("claim_timestamp")
            and current.get("summary_updated_at") != file["claim_timestamp"]
        ):
            message = "Processing claim changed before completion."
        if message:
            error = StaleContentError(message)
            error.processing_stage = "persistence"
            raise error

    def _journal(self, file, status, claim=None, outcome=None):
        """
        Record processing state in local journal (sync_state table).

        Updates the local SQLite journal with current processing status,
        error details, claim information, and model versions.

        Args:
            file: File dict
            status: Processing status (pending, processing, completed, failed, etc.)
            claim: ProcessingClaim instance (for claim_timestamp/owner)
            outcome: Outcome dict (for error details)
        """
        if file.get("root_id") is None or not file.get("path"):
            return
        registry = FileRegistry(self.db_path)
        registry.initialize_journal()
        registry.save_state(
            file,
            status,
            error=(outcome or {}).get("error"),
            remote_id=file["id"],
            claim_timestamp=file.get("claim_timestamp")
            if status == "processing"
            else None,
            claim_owner=claim.owner
            if claim is not None and status == "processing"
            else None,
            summary_model=self.summarizer.model_name if status == "completed" else None,
            embedding_model=self.embeddings.model_name
            if status == "completed"
            else None,
            summary_version=settings.SUMMARY_VERSION if status == "completed" else None,
        )

    def process_file(self, file, on_claim=None):
        """
        Process a single file through the complete pipeline.

        Pipeline stages:
        1. Validate file eligibility (status, content_hash)
        2. Read and verify local content
        3. Acquire processing claim (prevents concurrent processing)
        4. Check for reusable summary/embedding
        5. Generate summary (or reuse)
        6. Validate summary for privacy
        7. Generate embedding (or reuse)
        8. Persist summary and embedding to Supabase
        9. Verify state consistency
        10. Complete claim and update journal

        Args:
            file: File dict from Supabase with pending summary_status
            on_claim: Optional callback(claim_timestamp) when claim acquired

        Returns:
            Outcome dict with outcome, file_id, error (if any), etc.
        """
        file = dict(file)
        claimed, stage = False, "extraction"
        claim = None
        result = None
        content = None
        try:
            # Skip if already processed or processing elsewhere
            if file.get("summary_status") not in {"pending", "failed"}:
                return make_outcome(
                    file,
                    "skipped",
                    reason="processing_elsewhere"
                    if file.get("summary_status") == "processing"
                    else "already_synchronized",
                )
            if file.get("status") not in {"indexed", "metadata_only"}:
                return make_outcome(file, "skipped", reason="ineligible_status")
            if not file.get("content_hash"):
                raise StaleContentError("File has no content hash.")

            # Stage 1: Extract and verify content
            content = self._read_content(file)
            stage = "persistence"

            # Stage 2: Acquire processing claim
            claim = ProcessingClaim(
                self.files,
                file,
                lambda status, owner: self._journal(file, status, owner),
            )
            if not claim.acquire():
                return make_outcome(file, "skipped", reason="concurrent_claim_lost")
            claimed = True
            if on_claim:
                on_claim(file.get("claim_timestamp"))

            # Stage 3: Check for reusable summary
            stage = "persistence"
            record = self.summaries.get_summary(file["id"])
            reusable = (
                isinstance(record, dict)
                and record.get("source_content_hash") == file["content_hash"]
                and record.get("model_name") == self.summarizer.model_name
                and record.get("summary_version", 1) == settings.SUMMARY_VERSION
                and isinstance(record.get("summary"), str)
            )

            # Stage 4: Summarization
            stage = "summarization"
            summary = (
                record["summary"]
                if reusable
                else self.summarizer.summarize(content, file["filename"])
            )
            validate_summary(summary, content)
            del content  # Explicitly clear content from memory

            # Stage 5: Embedding
            stage = "embedding"
            vector = (
                self.summaries.get_summary_embedding(record["id"]) if reusable else None
            )
            embedding = None
            if (
                isinstance(vector, dict)
                and vector.get("model_name") == self.embeddings.model_name
            ):
                try:
                    import json

                    stored = vector.get("embedding")
                    embedding = validate_embedding(
                        json.loads(stored) if isinstance(stored, str) else stored
                    )
                except (TypeError, ValueError):
                    pass
            if embedding is None:
                embedding = validate_embedding(self.embeddings.embed(summary))

            # Stage 6: Persist to Supabase (under claim mutex)
            with claim.mutex:
                if claim.lost:
                    raise StaleContentError(
                        "Processing claim changed before completion."
                    )
                # Re-verify state before persistence
                stage = "extraction"
                self._check_current(file)
                stage = "persistence"
                if not reusable:
                    record = self.summaries.upsert_summary(
                        file["id"],
                        file["content_hash"],
                        summary,
                        {},
                        self.summarizer.model_name,
                        settings.SUMMARY_VERSION,
                    )
                saved = self.summaries.save_embedding(
                    record["id"], embedding, self.embeddings.model_name
                )
                if not saved:
                    raise RuntimeError("Embedding upsert returned no record.")
                # Final verification
                stage = "extraction"
                self._check_current(file)
                stage = "persistence"
                if claim.lost or not claim.transition("completed"):
                    raise StaleContentError(
                        "Processing claim changed before completion."
                    )
            result = make_outcome(file, "completed")

        except StaleContentError as error:
            stage = getattr(error, "processing_stage", stage)
            result = make_outcome(file, "stale", stage, error)
            if claimed:
                self._save_failure(file, "pending", stage, error, result, claim)
        except Exception as error:  # noqa: BLE001 - sanitize unexpected errors
            stage = getattr(error, "processing_stage", stage)
            result = make_outcome(file, "failed", stage, error)
            logger.error(
                "File %s: %s", result["file_id"], format_processing_error(stage, error)
            )
            self._save_failure(
                file, "failed", stage, error, result, claim if claimed else None
            )
        except (KeyboardInterrupt, SystemExit):
            # On interruption, try to return claim to pending for recovery
            if claimed:
                try:
                    if claim.transition("pending"):
                        self._journal(file, "pending")
                except Exception:  # noqa: BLE001,S110 - journal recovery is best-effort
                    pass  # A matching interrupted journal remains recoverable.
            raise
        finally:
            if claim is not None:
                claim.close()
            try:
                del content
            except NameError:
                pass

        # Record final outcome in journal
        if result is not None:
            try:
                if claimed and (result.get("status_error") or claim.lost):
                    self._journal(file, "processing", claim, result)
                else:
                    self._journal(file, result["outcome"], outcome=result)
            except Exception as error:  # noqa: BLE001 - journal error is non-fatal
                result["journal_error"] = describe_error("persistence", error)
        return result

    def _save_failure(self, file, status, stage, error, result, claim):
        """
        Persist failure state to Supabase and/or local journal.

        Args:
            file: File dict
            status: Target status (pending, failed)
            stage: Processing stage where failure occurred
            error: Exception that caused failure
            result: Outcome dict to update
            claim: ProcessingClaim instance (if acquired)
        """
        try:
            if claim is not None:
                claim.transition(status, format_processing_error(stage, error))
            elif file.get("summary_status") in {"pending", "failed"}:
                self.files.update_summary_status(
                    file["id"],
                    status,
                    error=format_processing_error(stage, error),
                    content_hash=file["content_hash"],
                    expected_status=file["summary_status"],
                    expected_updated_at=file.get("summary_updated_at"),
                )
        except Exception as status_error:  # noqa: BLE001 - status persistence is non-fatal
            result["status_error"] = describe_error("persistence", status_error)
            logger.error(
                "Could not save failure status: %s",
                format_processing_error("persistence", status_error),
            )

    def process_pending(
        self,
        limit=50,
        retry_failed=False,
        recover_processing=False,
        retry_permanent=False,
    ):
        """
        Process pending summaries from Supabase queue.

        Used by standalone summarization command. Fetches pending files
        from Supabase, validates locally, and processes through pipeline.

        Args:
            limit: Maximum files to process
            retry_failed: Include failed files eligible for retry
            recover_processing: Include files stuck in processing state
            retry_permanent: With retry_failed, retry known non-retryable

        Returns:
            Dict with counts for each outcome type
        """
        if retry_permanent and not retry_failed:
            raise ValueError("retry_permanent requires retry_failed.")
        if limit <= 0:
            raise ValueError("Limit must be positive.")
        registry = FileRegistry(self.db_path)
        self.outcomes = []
        with registry.lock():
            from searchify.indexer import purge_legacy_content

            if self.db_path.exists():
                purge_legacy_content(self.db_path)
            if self.db_path.exists():
                registry.initialize_journal()
            # Safe recovery is automatic in both commands. Keep the explicit
            # recover_processing argument for existing CLI/API callers.
            options = {"include_processing": True}
            if retry_permanent:
                options["retry_permanent"] = True
            pending = self.files.get_pending_summaries(
                limit, retry_failed=retry_failed, **options
            )
            if pending:
                check_processing_runtime(self.summarizer, self.embeddings)
            for file in pending:
                file = dict(file)
                # Repository selection is metadata-only; read/validate again before AI.
                directory = self.files.get_directory(file["directory_id"])
                if directory:
                    try:
                        path = resolve_indexed_file(
                            self.db_path,
                            directory["path"],
                            file["relative_path"],
                            file["content_hash"],
                        )
                        metadata = registry.metadata().get(str(path))
                        if metadata:
                            file.update(path=str(path), root_id=metadata["root_id"])
                    except LocalContentError:
                        pass
                state = registry.states().get(file.get("path"), {})
                if file.get("summary_status") == "failed" and (
                    not retry_failed
                    or (
                        state.get("content_hash") == file["content_hash"]
                        and (state.get("error") or {}).get("retryable") is False
                        and not retry_permanent
                    )
                ):
                    continue
                if file.get("summary_status") == "processing":
                    file = recover_claim(self.files, registry, file)
                    if file is None:
                        continue
                self.outcomes.append(self.process_file(file))
        counts = {
            status: sum(row["outcome"] == status for row in self.outcomes)
            for status in ("completed", "failed", "stale", "skipped")
        }
        counts["journal_errors"] = sum("journal_error" in row for row in self.outcomes)
        return counts
