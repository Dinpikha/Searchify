"""
File Repository - Supabase File Metadata Operations

This module provides the high-level repository for file metadata in
Supabase. It extends the base database repository with operations
for synchronization pipeline support.

Key features:
- Owner-scoped queries (RLS via auth.uid())
- Conditional updates with CAS (Compare-And-Swap) for claim management
- Processing claim recovery for interrupted workers
- Directory caching for performance
- Pagination for large result sets
- Rename detection via content hash matching

All operations enforce owner isolation through Supabase RLS policies.

Used by: sync_service.py, summary_service.py, processing_claim.py
"""

from datetime import datetime, timezone

from searchify.config.settings import settings
from searchify.database.repositories.files import FileRepository as BaseFileRepository


class FileRepository(BaseFileRepository):
    """
    Extended file repository with sync pipeline support.

    Adds methods for claim recovery, conditional status updates,
    directory caching, and batch metadata operations.
    """

    def __init__(self, db_path=None):
        """
        Initialize with local database path for claim recovery.

        Args:
            db_path: Local SQLite path (default: settings.LOCAL_DB_PATH)
        """
        super().__init__()
        self.db_path = db_path or settings.LOCAL_DB_PATH
        self._directory_cache = {}

    def get_file(self, file_id):
        """
        Get minimal file metadata by ID.

        Selects only fields needed for claim processing to reduce payload.

        Args:
            file_id: Supabase file UUID

        Returns:
            File dict or None
        """
        return (
            self.client.table("files")
            .select("id, content_hash, status, summary_status, summary_updated_at")
            .eq("id", file_id)
            .eq("owner_id", self.owner_id)
            .maybe_single()
            .execute()
            .data
        )

    def recover_processing(self):
        """
        Recover eligible expired or interrupted processing claims.

        Scans all processing files, validates local eligibility,
        and attempts claim recovery under workspace lock.

        Returns:
            List of recovered file dicts with pending status
        """
        from searchify.core.file_registry import FileRegistry
        from searchify.services.processing_claim import recover_claim

        registry = FileRegistry(self.db_path)
        recovered = []
        with registry.lock():
            for file in self.get_pending_summaries(2**31 - 1, include_processing=True):
                if file.get("summary_status") != "processing":
                    continue
                entry = self.local_entry(file)
                row = recover_claim(self, registry, entry) if entry else None
                if row:
                    recovered.append(row)
        return recovered

    def local_entry(self, file):
        """
        Resolve local registration and rules without reading source contents.

        Args:
            file: Supabase file dict

        Returns:
            Eligible entry dict or None
        """
        from searchify.core.file_registry import FileRegistry
        from searchify.services.processing_claim import eligible_entry

        return eligible_entry(self, FileRegistry(self.db_path), file)

    def get_pending_summaries(
        self,
        limit=50,
        retry_failed=False,
        include_processing=False,
        retry_permanent=False,
    ):
        """
        Fetch files eligible for summary processing with local validation.

        Paginates through Supabase files, then validates each against
        local registry to ensure:
        - File exists and matches content hash
        - Directory rules allow processing
        - Processing claims are recoverable (if include_processing)
        - Failed files are retryable (if retry_failed)

        Args:
            limit: Maximum files to return
            retry_failed: Include failed files
            include_processing: Include processing files for recovery
            retry_permanent: Retry known non-retryable failures

        Returns:
            List of eligible file dicts
        """
        if limit <= 0:
            raise ValueError("Limit must be positive.")
        eligible = []
        offset = 0
        # Page metadata so cloud-only rows cannot consume the local batch limit.
        while len(eligible) < limit:
            query = (
                self.client.table("files")
                .select(
                    "id, directory_id, relative_path, filename, content_hash, status, summary_status, summary_updated_at"
                )
                .eq("owner_id", self.owner_id)
                .in_("status", ["indexed", "metadata_only"])
            )
            statuses = (
                ["pending"]
                + (["failed"] if retry_failed else [])
                + (["processing"] if include_processing else [])
            )
            query = query.in_("summary_status", statuses)
            rows = (
                query.order("created_at")
                .order("id")
                .range(offset, offset + 99)
                .execute()
                .data
                or []
            )
            for file in rows:
                if file.get("status") not in {"indexed", "metadata_only"}:
                    continue
                entry = self.local_entry(file)
                if entry is None:
                    continue
                from searchify.core.file_registry import FileRegistry
                from searchify.services.processing_claim import recoverable

                registry = FileRegistry(self.db_path)
                if file.get("summary_status") == "processing" and not recoverable(
                    registry, entry
                ):
                    continue
                state = registry.states().get(entry["path"], {})
                if (
                    file.get("summary_status") == "failed"
                    and state.get("content_hash") == file["content_hash"]
                    and (state.get("error") or {}).get("retryable") is False
                    and not retry_permanent
                ):
                    continue
                eligible.append(file)
                if len(eligible) == limit:
                    break
            if len(rows) < 100:
                break
            offset += len(rows)
        return eligible

    def get_directory(self, directory_id):
        """
        Get directory metadata with caching.

        Args:
            directory_id: Supabase directory UUID

        Returns:
            Directory dict with path and enabled status
        """
        cache = getattr(self, "_directory_cache", {})
        if directory_id not in cache:
            cache[directory_id] = (
                self.client.table("directories")
                .select("path, enabled")
                .eq("id", directory_id)
                .eq("owner_id", self.owner_id)
                .maybe_single()
                .execute()
                .data
            )
            self._directory_cache = cache
        return cache[directory_id]

    def update_summary_status(
        self,
        file_id,
        status,
        error=None,
        content_hash=None,
        expected_status=None,
        expected_updated_at=None,
    ):
        """
        Conditionally update summary_status with CAS semantics.

        Builds query with optional filters for:
        - content_hash match
        - expected summary_status (CAS)
        - expected summary_updated_at (CAS for claim renewal)
        - status in [indexed, metadata_only] for processing transitions

        Args:
            file_id: Supabase file UUID
            status: New summary_status
            error: Error message for failed status
            content_hash: Expected content hash
            expected_status: Expected current summary_status
            expected_updated_at: Expected current summary_updated_at

        Returns:
            Updated rows

        Raises:
            ValueError: If status invalid
        """
        if status not in {"pending", "processing", "completed", "failed"}:
            raise ValueError("Invalid summary status.")
        query = (
            self.client.table("files")
            .update(
                {
                    "summary_status": status,
                    "summary_error": error,
                    "summary_updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            .eq("id", file_id)
            .eq("owner_id", self.owner_id)
        )
        if status == "processing" or expected_status == "processing":
            query = query.in_("status", ["indexed", "metadata_only"])
        if content_hash is not None:
            query = query.eq("content_hash", content_hash)
        if expected_status is not None:
            query = query.eq("summary_status", expected_status)
        if expected_updated_at is not None:
            query = query.eq("summary_updated_at", expected_updated_at)
        return query.execute().data or []

    def get_sync_counts(self):
        """
        Get file counts by summary_status for current owner.

        Returns:
            Dict with counts for each status plus indexed total
        """
        counts = {}
        for status in ("pending", "failed", "completed", "processing"):
            result = (
                self.client.table("files")
                .select("id", count="exact")
                .eq("owner_id", self.owner_id)
                .eq("summary_status", status)
                .execute()
            )
            counts[status] = result.count or 0
        indexed_result = (
            self.client.table("files")
            .select("id", count="exact")
            .eq("owner_id", self.owner_id)
            .eq("status", "indexed")
            .execute()
        )
        counts["indexed"] = indexed_result.count or 0
        return counts

    def _pages(self, table, columns, **filters):
        """
        Paginate through Supabase table with owner_id filter.

        Args:
            table: Table name
            columns: Columns to select
            **filters: Additional equality filters

        Returns:
            List of all rows
        """
        rows, offset = [], 0
        while True:
            query = (
                self.client.table(table).select(columns).eq("owner_id", self.owner_id)
            )
            for key, value in filters.items():
                query = query.eq(key, value)
            page = query.order("id").range(offset, offset + 499).execute().data or []
            rows.extend(page)
            if len(page) < 500:
                return rows
            offset += len(page)

    def list_directories(self):
        """
        List all directories for current owner with pagination.

        Returns:
            List of directory dicts (id, path, enabled)
        """
        return self._pages("directories", "id, path, enabled")

    def list_files(self, directory_id):
        """
        List all files in a directory with pagination.

        Returns:
            List of file dicts with full metadata
        """
        return self._pages(
            "files",
            "id, directory_id, relative_path, filename, extension, size_bytes, modified_ns, content_hash, status, summary_status, summary_error, summary_updated_at",
            directory_id=directory_id,
        )

    def ensure_directory(self, path):
        """
        Ensure directory exists in Supabase, creating if needed.

        Converts absolute path to display format (~/project) for storage.

        Args:
            path: Absolute directory path

        Returns:
            Directory record dict

        Raises:
            RuntimeError: If persistence returns no record
        """
        from pathlib import Path

        root = Path(path).expanduser().resolve()
        try:
            display = "~/" + str(root.relative_to(Path.home()))
        except ValueError:
            display = str(root)
        rows = (
            self.client.table("directories")
            .upsert(
                {"owner_id": self.owner_id, "path": display, "name": root.name},
                on_conflict="owner_id,path",
            )
            .execute()
            .data
            or []
        )
        if not rows:
            raise RuntimeError("Directory metadata persistence returned no record.")
        return rows[0]

    def upsert_metadata(self, records):
        """
        Batch upsert file metadata with conditional updates.

        For records with _expected (existing), uses conditional update
        with CAS on id, content_hash, summary_status, summary_updated_at.
        For new records, uses upsert with ignore_duplicates to avoid
        racing with concurrent claim acquisition.

        Args:
            records: List of file metadata dicts

        Returns:
            List of upserted records
        """
        output = []
        inserts = []
        for record in records:
            payload = dict(record)
            expected = payload.pop("_expected", None)
            if expected is not None:
                query = (
                    self.client.table("files")
                    .update(payload)
                    .eq("owner_id", self.owner_id)
                    .eq("id", expected["id"])
                    .eq("content_hash", expected["content_hash"])
                    .eq("summary_status", expected["summary_status"])
                )
                if expected.get("summary_updated_at"):
                    query = query.eq(
                        "summary_updated_at", expected["summary_updated_at"]
                    )
                output.extend(query.execute().data or [])
            else:
                inserts.append(payload)
        for start in range(0, len(inserts), 50):
            # Ignore insertion races; never upsert over another worker's claim.
            output.extend(
                self.client.table("files")
                .upsert(
                    inserts[start : start + 50],
                    on_conflict="directory_id,relative_path",
                    ignore_duplicates=True,
                )
                .execute()
                .data
                or []
            )
        return output

    def mark_unavailable(self, file_id):
        """
        Mark file as unavailable (local file missing).

        Updates status to error with error messages.

        Args:
            file_id: Supabase file UUID

        Returns:
            Updated rows
        """
        return (
            self.client.table("files")
            .update(
                {
                    "status": "error",
                    "error_message": "Local file unavailable.",
                    "summary_error": "Local file unavailable.",
                }
            )
            .eq("id", file_id)
            .eq("owner_id", self.owner_id)
            .execute()
            .data
            or []
        )

    def move_metadata(
        self, file_id, relative_path, filename, size_bytes, modified_ns, content_hash
    ):
        """
        Move file metadata (rename detection) with content hash CAS.

        Only succeeds if content_hash matches and summary_status != processing.

        Args:
            file_id: Supabase file UUID
            relative_path: New relative path
            filename: New filename
            size_bytes: New size
            modified_ns: New mtime nanoseconds
            content_hash: Expected content hash

        Returns:
            Updated rows (empty if CAS failed)
        """
        from pathlib import Path

        return (
            self.client.table("files")
            .update(
                {
                    "relative_path": relative_path,
                    "filename": filename,
                    "extension": Path(filename).suffix.lower(),
                    "size_bytes": size_bytes,
                    "modified_ns": modified_ns,
                }
            )
            .eq("id", file_id)
            .eq("owner_id", self.owner_id)
            .eq("content_hash", content_hash)
            .neq("summary_status", "processing")
            .execute()
            .data
            or []
        )

    def mark_skipped(self, file_id, reason):
        """
        Mark file as intentionally skipped.

        Args:
            file_id: Supabase file UUID
            reason: Skip reason

        Returns:
            Updated rows
        """
        return (
            self.client.table("files")
            .update(
                {
                    "status": "skipped",
                    "summary_error": "Intentionally skipped: " + reason,
                }
            )
            .eq("id", file_id)
            .eq("owner_id", self.owner_id)
            .execute()
            .data
            or []
        )

    def restore_available(self, file_id, content_hash, previous_status):
        """
        Restore file availability after incorrect unavailability.

        Only succeeds if content_hash matches, previous_status matches,
        and summary_status is completed.

        Args:
            file_id: Supabase file UUID
            content_hash: Expected content hash
            previous_status: Expected previous status

        Returns:
            Updated rows
        """
        return (
            self.client.table("files")
            .update({"status": "indexed", "summary_error": None, "error_message": None})
            .eq("id", file_id)
            .eq("owner_id", self.owner_id)
            .eq("content_hash", content_hash)
            .eq("status", previous_status)
            .eq("summary_status", "completed")
            .execute()
            .data
            or []
        )
