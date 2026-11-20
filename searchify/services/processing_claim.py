"""
Processing Claim - Concurrent Worker Coordination

This module implements a lease-based claim system for coordinating
concurrent file processing across multiple workers. It uses Supabase
summary_updated_at as a CAS (Compare-And-Swap) token and a background
heartbeat thread to renew the claim periodically.

Key design:
- Each worker gets a unique owner ID (UUID)
- Claim acquisition uses conditional update on summary_status
- Heartbeat thread renews claim every 60 seconds (extends lease)
- Lease expires after 1 hour without renewal
- Recovery logic detects interrupted claims and allows takeover
- Local workspace lock required for recovery operations

This prevents duplicate AI processing of the same file while allowing
recovery from worker crashes/interruptions.

Used by: summary_service.py, sync_service.py
"""

import threading
import uuid
from datetime import datetime, timedelta, timezone

# Claim lease duration: 1 hour
LEASE_SECONDS = 3600
# Heartbeat renewal interval: 60 seconds
HEARTBEAT_SECONDS = 60


def expired(stamp):
    """
    Check if a claim timestamp has expired.

    A claim is expired if the timestamp is older than LEASE_SECONDS
    from the current time. Invalid/malformed timestamps are treated
    as non-expired (fail-safe: don't assume worker stopped).

    Args:
        stamp: ISO format timestamp string (may end with Z or +00:00)

    Returns:
        True if expired, False otherwise
    """
    try:
        value = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        return value.tzinfo is not None and value < datetime.now(
            timezone.utc
        ) - timedelta(seconds=LEASE_SECONDS)
    except (AttributeError, TypeError, ValueError):
        # An invalid timestamp does not establish that a worker has stopped.
        return False


def eligible_entry(files, registry, file):
    """
    Verify a file is eligible for claim recovery.

    Checks:
    1. Directory exists and is enabled
    2. Root boundary not redirected via symlink
    3. File path resolves and matches content hash
    4. File not in protected directory
    5. Local metadata exists with valid rules

    Args:
        files: FileRepository instance
        registry: FileRegistry instance
        file: File dict with directory_id, relative_path, content_hash

    Returns:
        Enriched file dict with path, root_id, signature, rules; or None if ineligible
    """
    from pathlib import Path

    from searchify.core.scanner import PROTECTED_DIRS, root_is_unredirected, signature
    from searchify.indexer import LocalContentError, resolve_indexed_file

    directory = files.get_directory(file["directory_id"])
    if not directory or not directory.get("enabled", True):
        return None
    root = Path(directory["path"]).expanduser().absolute()
    if not root_is_unredirected(root):
        return None
    try:
        path = resolve_indexed_file(
            registry.path,
            directory["path"],
            file["relative_path"],
            file.get("content_hash"),
        )
        if any(part in PROTECTED_DIRS for part in path.parts):
            return None
        metadata = registry.metadata().get(str(path))
        if not metadata:
            return None
        rules = registry.rules(metadata["root_id"])
        return {
            **file,
            "path": str(path),
            "root_id": metadata["root_id"],
            "signature": signature(path.stat()),
            "max_size": rules.max_size,
            "allowed_extensions": rules.extensions,
        }
    except (LocalContentError, OSError):
        return None


def recoverable(registry, file):
    """
    Determine if a processing claim can be recovered.

    A claim is recoverable if:
    1. Has a summary_updated_at timestamp
    2. Either:
       a) Local state shows "processing" with matching claim_owner, remote_id, root_id, claim_timestamp
       b) Timestamp is expired (lease expired)

    Args:
        registry: FileRegistry instance
        file: File dict with summary_status, summary_updated_at, id, root_id

    Returns:
        True if claim can be recovered, False otherwise
    """
    stamp = file.get("summary_updated_at")
    state = registry.states().get(file.get("path"), {})
    interrupted = (
        state.get("status") == "processing"
        and state.get("claim_owner")
        and state.get("remote_id") == file["id"]
        and state.get("root_id") == file.get("root_id")
        and state.get("claim_timestamp") == stamp
    )
    return bool(stamp and (interrupted or expired(stamp)))


def recover_claim(files, registry, file):
    """
    Attempt to recover an interrupted processing claim.

    Requires workspace lock. Performs:
    1. Verify file summary_status is "processing"
    2. Verify local eligibility
    3. Check if claim is recoverable (interrupted or expired)
    4. Atomically transition status from "processing" to "pending" via CAS
    5. Return updated file dict with pending status

    Args:
        files: FileRepository instance
        registry: FileRegistry instance
        file: File dict from Supabase

    Returns:
        Updated file dict with summary_status="pending", or None if not recoverable
    """
    """Requires the workspace lock and a currently eligible local candidate."""
    if file.get("summary_status") != "processing":
        return file
    file = eligible_entry(files, registry, file)
    if file is None:
        return None
    stamp = file.get("summary_updated_at")
    if not recoverable(registry, file):
        return None
    rows = files.update_summary_status(
        file["id"],
        "pending",
        content_hash=file["content_hash"],
        expected_status="processing",
        expected_updated_at=stamp,
    )
    return {**file, **rows[0], "summary_status": "pending"} if rows else None


class ProcessingClaim:
    """
    Manages a processing claim for a single file.

    Provides:
    - Claim acquisition via conditional Supabase update
    - Background heartbeat thread for lease renewal
    - Thread-safe status transitions (completed, failed, pending)
    - Claim loss detection (CAS failure = another worker took over)
    - Cleanup on close (stops heartbeat thread)

    Usage:
        claim = ProcessingClaim(files, file, journal_callback)
        if claim.acquire():
            try:
                # ... process file ...
                claim.transition("completed")
            except Exception:
                claim.transition("failed", error)
            finally:
                claim.close()
    """

    def __init__(self, files, file, journal):
        """
        Initialize claim manager.

        Args:
            files: FileRepository instance
            file: File dict being processed
            journal: Callback function(status, claim) for local journal updates
        """
        self.files, self.file, self.journal = files, file, journal
        self.owner = uuid.uuid4().hex  # Unique worker identifier
        self.mutex = threading.RLock()  # Protects transition/heartbeat
        self.stop = threading.Event()  # Signals heartbeat to stop
        self.thread = None  # Heartbeat thread
        self.lost = False  # Set if CAS fails (claim stolen)

    def acquire(self):
        """
        Attempt to acquire processing claim.

        Performs conditional update on Supabase:
        - summary_status must be "pending" or "failed"
        - content_hash must match
        - expected_status must match current
        - expected_updated_at must match (if present)

        On success, starts heartbeat thread and records claim in journal.

        Returns:
            True if claim acquired, False otherwise
        """
        if self.file.get("summary_status") not in {"pending", "failed"}:
            return False
        options = {
            "content_hash": self.file["content_hash"],
            "expected_status": self.file["summary_status"],
        }
        if self.file.get("summary_updated_at"):
            options["expected_updated_at"] = self.file["summary_updated_at"]
        rows = self.files.update_summary_status(
            self.file["id"], "processing", **options
        )
        if not rows:
            return False
        self.file["claim_timestamp"] = rows[0].get("summary_updated_at")
        self.journal("processing", self)
        self.thread = threading.Thread(
            target=self._heartbeat, name="searchify-claim-heartbeat", daemon=True
        )
        self.thread.start()
        return True

    def transition(self, status, error=None):
        """
        Transition claim to new status with CAS.

        Uses compare-and-swap on Supabase:
        - expected_status must be "processing"
        - expected_updated_at must match claim_timestamp
        - content_hash must match

        On CAS failure, marks claim as lost (another worker took over).
        On success to "processing", updates claim_timestamp and journals.

        Args:
            status: New status ("completed", "failed", "pending", "processing")
            error: Error dict for failed status

        Returns:
            Updated rows from Supabase, or empty list if CAS failed
        """
        with self.mutex:
            if self.lost:
                return []
            rows = self.files.update_summary_status(
                self.file["id"],
                status,
                error=error,
                content_hash=self.file["content_hash"],
                expected_status="processing",
                expected_updated_at=self.file["claim_timestamp"],
            )
            if not rows:
                self.lost = True
            elif status == "processing":
                self.file["claim_timestamp"] = rows[0].get("summary_updated_at")
                self.journal("processing", self)
            return rows

    def _heartbeat(self):
        """
        Background thread: renew claim lease every HEARTBEAT_SECONDS.

        Calls transition("processing") to extend the summary_updated_at
        timestamp. On any failure (CAS failure, exception), marks claim
        as lost and exits (fail-closed: no persistence after uncertainty).
        """
        while not self.stop.wait(HEARTBEAT_SECONDS):
            try:
                if not self.transition("processing"):
                    return
            except Exception:  # noqa: BLE001 - fail closed on renewal uncertainty
                # Fail closed; no persistence after renewal becomes uncertain.
                self.lost = True
                return

    def close(self):
        """
        Clean up claim resources.

        Stops heartbeat thread and waits for it to finish.
        Should be called in finally block after processing.
        """
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
