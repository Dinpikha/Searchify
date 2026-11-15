from datetime import datetime, timezone

from searchify.config.settings import settings
from searchify.database.repositories.files import FileRepository as BaseFileRepository


class FileRepository(BaseFileRepository):
    def __init__(self, db_path=None):
        super().__init__()
        self.db_path = db_path or settings.LOCAL_DB_PATH
        self._directory_cache = {}

    def get_file(self, file_id):
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
        """Recover eligible expired or proven-interrupted claims under the workspace lock."""
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
        """Resolve registration and current rules without reading source contents."""
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
        """Return counts of files by summary_status for the current owner."""
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
        return self._pages("directories", "id, path, enabled")

    def list_files(self, directory_id):
        return self._pages(
            "files",
            "id, directory_id, relative_path, filename, extension, size_bytes, modified_ns, content_hash, status, summary_status, summary_error, summary_updated_at",
            directory_id=directory_id,
        )

    def ensure_directory(self, path):
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
