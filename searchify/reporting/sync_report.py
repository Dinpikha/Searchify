"""Structured, conservative diagnostics and append-only local run reports."""

import errno
import hashlib
import json
import os
import re
import tempfile
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError

from searchify.ai.ollama_client import (
    OllamaError,
    OllamaUnavailableError,
    format_processing_error,
)
from searchify.config.settings import settings

OUTCOMES = ("completed", "failed", "stale", "skipped")
ACTIONS = {
    "claim_conflict": "Another worker changed the processing claim. Wait for it to finish; do not overwrite its work.",
    "service_unavailable": "Start Ollama or check the configured local runtime endpoint, then retry after its health probe succeeds.",
    "workspace_busy": "Wait for the active Searchify command to finish, then retry. Do not remove lock files to bypass it.",
    "hash_mismatch": "Refresh the local file hash and remote metadata, then retry. The file may have changed during processing.",
    "empty_or_binary": "No usable UTF-8 text was extracted; the file may be empty, whitespace-only, binary or use an unsupported encoding.",
    "dependency_error": "Use the intended Python environment and install Searchify with python -m pip install -e .",
    "unsupported_file_type": "Extraction is not implemented for this file type; it was intentionally skipped.",
    "size_restriction": "The file exceeds this directory’s configured size limit; review its rule before changing the limit.",
    "missing_file": "The local file is unavailable; refresh metadata or restore the file.",
    "excluded_pattern": "The file matches an explicit directory exclusion or protected-file rule.",
    "unknown": "Cause is unknown. Inspect the failing stage using diagnostics that omit request bodies and credentials.",
    "connection_error": "Check provider availability and network connectivity; for Ollama, start the app or run ollama serve.",
    "timeout": "Check provider responsiveness and system load before retrying.",
    "http_authentication": "Check the configured credentials and authentication session.",
    "http_permission": "Check provider permissions and Supabase row-level security.",
    "rate_limit": "Wait for the provider rate limit to reset before retrying.",
    "http_server_error": "Check provider health and retry after service recovery.",
    "http_client_error": "Check the endpoint, requested resource or model, and request configuration.",
    "http_error": "Inspect the HTTP status and provider configuration; no more specific cause is known.",
    "database_permission": "Check authentication and Supabase row-level security policies.",
    "database_schema": "Check that the required database migrations and schema cache are current.",
    "database_constraint": "Check required metadata and database constraints before retrying.",
    "invalid_embedding": "Verify the local embedding model returns 768 finite numeric dimensions.",
    "incomplete_summary": "Inspect generation completion and output limits; the response was incomplete or empty.",
    "invalid_provider_response": "Check the provider API response format without logging its body.",
    "potential_sensitive_summary": "The summary matched a credential-pattern guard. Review summarization locally before retrying; no credential value was logged.",
    "source_echo": "Review summarization instructions; the generated output reproduced source text.",
    "local_file_missing": "Restore the local file or refresh its indexed metadata.",
    "local_permission": "Grant local read permission before retrying.",
    "stale_content": "Refresh local hashes and file metadata before retrying; unsupported or empty files need eligibility review.",
}
_DATABASE_CATEGORIES = {
    "42501": "database_permission",
    "PGRST301": "http_authentication",
    "PGRST302": "http_authentication",
    "42P01": "database_schema",
    "42703": "database_schema",
    "PGRST204": "database_schema",
    "PGRST205": "database_schema",
    "23503": "database_constraint",
    "23505": "database_constraint",
    "23502": "database_constraint",
    "23514": "database_constraint",
}
_STALE_MESSAGES = frozenset(
    {
        "Remote file metadata is unavailable.",
        "Remote file is no longer eligible.",
        "Processing claim changed before completion.",
        "Directory metadata unavailable.",
        "File path is outside its registered directory.",
        "Unsupported file type.",
        "File exceeds maximum size.",
        "File has no extractable content.",
        "Local content hash mismatch.",
        "File has no content hash.",
        "Local file content is missing or changed.",
        "Remote content hash changed.",
        "File changed before completion.",
        "Invalid local file identity.",
        "Local file is missing, unsupported or stale.",
        "Local index or file is unavailable.",
    }
)


def now():
    return datetime.now(timezone.utc).isoformat()


def _sensitive(value):
    # Names and provider request IDs are also untrusted metadata.
    secrets = [settings.SUPABASE_KEY]
    secrets += [
        v
        for k, v in os.environ.items()
        if re.search(
            r"(TOKEN|PASSWORD|SECRET|CREDENTIAL|(?:^|_)KEY(?:_|$))", k, re.IGNORECASE
        )
    ]
    return (
        any(
            isinstance(secret, str) and len(secret) >= 4 and secret in value
            for secret in secrets
        )
        or re.search(
            r"(?i)(authorization|bearer\s|password|credential|api[_-]?key|access[_-]?token|refresh[_-]?token|token\s*[=:])",
            value,
        )
        or re.search(
            r"(?:eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|sk-[A-Za-z0-9_-]{8,}|sb_secret_)",
            value,
        )
    )


def safe_filename(value):
    if not isinstance(value, str) or _sensitive(value):
        return "[redacted filename]"
    value = value.replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"[^\w.() -]", "_", value)[:160] or "[unnamed]"


def safe_identifier(value):
    if not isinstance(value, str) or _sensitive(value):
        return None
    return value if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value) else None


def _request_id(value):
    if not isinstance(value, str) or _sensitive(value):
        return None
    if re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        value,
    ) or re.fullmatch(r"req_[A-Za-z0-9_-]{8,80}", value):
        return value
    return None


def describe_error(stage, error):
    """Classify only typed errors, explicit codes and known Searchify messages."""
    diagnostic = format_processing_error(stage, error)
    stage, exception_type, message = diagnostic.split(": ", 2)
    category, retryable = "unknown", None
    raw = error.args[0] if error.args else None
    status = getattr(error, "http_status", None)
    response = getattr(error, "response", None)
    if status is None and isinstance(error, HTTPError):
        status = error.code
    if status is None:
        status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(error, "status", None)
    if isinstance(status, str) and re.fullmatch(r"[1-5][0-9]{2}", status):
        status = int(status)
    if (
        not isinstance(status, int)
        or isinstance(status, bool)
        or not 100 <= status <= 599
    ):
        status = None
    if status is None and isinstance(error, OllamaError) and isinstance(raw, str):
        match = re.fullmatch(
            r"Ollama HTTP error ([1-5][0-9]{2})( after retries)?\.", raw
        )
        if match:
            status = int(match[1])
    if status is not None:
        category = (
            "http_authentication"
            if status == 401
            else "http_permission"
            if status == 403
            else "rate_limit"
            if status == 429
            else "http_server_error"
            if status >= 500
            else "http_client_error"
            if status >= 400
            else "http_error"
        )
        retryable = status in {408, 429, 500, 502, 503, 504}
        # HTTP status metadata is safe even when the raw message was redacted.
        if message.startswith("Provider error details redacted"):
            message = f"Provider returned HTTP {status}; response details redacted."
    elif isinstance(error, ImportError):
        category, retryable = "dependency_error", False
    elif type(error).__name__ == "RegistryBusy":
        category, retryable, message = (
            "workspace_busy",
            True,
            "Another Searchify processing command holds the workspace lock.",
        )
    elif isinstance(error, OllamaUnavailableError):
        category, retryable = "service_unavailable", True
        message = "Ollama is unavailable. Start the Ollama app or run ollama serve, then retry."
    elif isinstance(error, TimeoutError):
        category, retryable = "timeout", True
    elif isinstance(error, (ConnectionError, URLError)):
        category, retryable = "connection_error", True
    elif isinstance(error, OllamaError) and raw == "Ollama unavailable after retries.":
        # This transport message can also follow an HTTP failure in older clients.
        category = "unknown"
    elif isinstance(error, OSError) and error.errno in {
        errno.ENOENT,
        errno.EACCES,
        errno.EPERM,
    }:
        category = (
            "local_file_missing" if error.errno == errno.ENOENT else "local_permission"
        )
        retryable = False
    elif isinstance(raw, str):
        category = {
            "Expected 768 finite numeric embedding dimensions.": "invalid_embedding",
            "Expected a nonzero embedding vector.": "invalid_embedding",
            "Ollama did not return a complete summary.": "incomplete_summary",
            "Ollama returned invalid JSON.": "invalid_provider_response",
            "Ollama returned an invalid response.": "invalid_provider_response",
            "Ollama returned an invalid embedding response.": "invalid_provider_response",
            "Summary reproduced original file contents.": "source_echo",
            "Summary may contain sensitive source text.": "potential_sensitive_summary",
        }.get(raw, "unknown")
        if type(error).__name__ == "StaleContentError" and raw in _STALE_MESSAGES:
            category, message = (
                (
                    {
                        "Local content hash mismatch.": "hash_mismatch",
                        "Remote content hash changed.": "hash_mismatch",
                        "Unsupported file type.": "unsupported_file_type",
                        "File exceeds maximum size.": "size_restriction",
                        "Directory metadata unavailable.": "missing_file",
                        "Remote file metadata is unavailable.": "missing_file",
                        "Processing claim changed before completion.": "claim_conflict",
                    }.get(raw, "stale_content")
                ),
                raw,
            )
    code = getattr(error, "code", None)
    if not isinstance(code, str) and isinstance(raw, dict):
        code = raw.get("code")
    if isinstance(code, str) and code in _DATABASE_CATEGORIES:
        category, retryable = _DATABASE_CATEGORIES[code], False
    request_id = _request_id(getattr(error, "request_id", None))
    headers = getattr(error, "headers", None)
    if headers is None:
        headers = getattr(response, "headers", None)
    if headers is not None and hasattr(headers, "get"):
        for key in ("x-request-id", "sb-request-id", "request-id", "X-Request-ID"):
            request_id = request_id or _request_id(headers.get(key))
    return {
        "stage": stage,
        "category": category,
        "exception_type": exception_type,
        "message": message,
        "provider": "local Ollama"
        if isinstance(error, OllamaError)
        else ("PostgREST" if code.startswith("PGRST") else "PostgreSQL")
        if isinstance(code, str) and code in _DATABASE_CATEGORIES
        else None,
        "http_status": status,
        "provider_request_id": request_id,
        "retryable": retryable,
        "corrective_action": ACTIONS[category],
    }


def make_outcome(file, outcome, stage=None, error=None, reason=None):
    if outcome not in OUTCOMES:
        raise ValueError("Invalid synchronization outcome.")
    identifier = safe_identifier(file.get("file_id") or file.get("id"))
    if identifier is None:
        identity = (
            str(
                file.get("directory_id")
                or file.get("root_id")
                or file.get("root_path")
                or ""
            )
            + ":"
            + str(file.get("relative_path", file.get("filename", "")))
        )
        identifier = "local-" + hashlib.sha256(identity.encode()).hexdigest()[:20]
    return {
        "file_id": identifier,
        "filename": safe_filename(file.get("filename")),
        "relative_path": "/".join(
            safe_filename(part)
            for part in str(file.get("relative_path", file.get("filename", "")))
            .replace("\\", "/")
            .split("/")
        ),
        "outcome": outcome,
        "timestamp": now(),
        "error": describe_error(stage, error) if error is not None else None,
        "reason": reason,
    }


class SyncReport:
    def __init__(self):
        self.run_id = str(uuid.uuid4())
        self.started_at = now()
        self.finished_at = None
        self.outcomes = []
        self.run_errors = []
        self.metrics = {}
        self.dry_run = False

    def record(self, file, outcome, stage=None, error=None, reason=None):
        result = make_outcome(file, outcome, stage, error, reason)
        self.outcomes.append(result)
        return result

    def snapshot(self):
        counts = Counter(row["outcome"] for row in self.outcomes)
        groups = defaultdict(list)
        for row in self.outcomes:
            if row["outcome"] == "failed":
                error = row["error"]
                groups[(error["category"], error["stage"])].append(row)
        failures = [
            {
                "category": category,
                "stage": stage,
                "count": len(rows),
                "filenames": [row["filename"] for row in rows],
                "file_ids": [row["file_id"] for row in rows],
                "corrective_action": ACTIONS[category],
            }
            for (category, stage), rows in sorted(groups.items())
        ]
        problems = defaultdict(list)
        reason_categories = {
            "empty_or_binary": "empty_or_binary",
            "unsupported_file_type": "unsupported_file_type",
            "size_restriction": "size_restriction",
            "missing_file": "missing_file",
            "known_unavailable": "missing_file",
            "excluded_pattern": "excluded_pattern",
            "protected_file": "excluded_pattern",
            "protected_directory": "excluded_pattern",
            "changed_during_scan": "stale_content",
            "incomplete_scan": "stale_content",
            "missing_directory": "missing_file",
        }
        for row in self.outcomes:
            error = row.get("error")
            category = (
                error["category"] if error else reason_categories.get(row.get("reason"))
            )
            if category and row["outcome"] != "completed":
                problems[(category, error["stage"] if error else "extraction")].append(
                    row
                )
        problem_groups = [
            {
                "category": category,
                "stage": stage,
                "count": len(rows),
                "filenames": [row["filename"] for row in rows],
                "corrective_action": ACTIONS[category],
            }
            for (category, stage), rows in sorted(problems.items())
        ]
        remaining = sum(
            row["outcome"] in {"failed", "stale"}
            or row.get("reason")
            in {
                "dry_run_would_process",
                "retry_required",
                "nonretryable_failure",
                "processing_elsewhere",
                "run_aborted_before_processing",
            }
            for row in self.outcomes
        )
        return {
            "problem_groups": problem_groups,
            "metrics": self.metrics,
            "dry_run": self.dry_run,
            "remaining": remaining,
            "schema_version": 1,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": (
                datetime.fromisoformat(self.finished_at)
                - datetime.fromisoformat(self.started_at)
            ).total_seconds()
            if self.finished_at
            else None,
            "counts": {
                **{key: counts[key] for key in OUTCOMES},
                "total": len(self.outcomes),
            },
            "failure_groups": failures,
            "outcomes": self.outcomes,
            "run_errors": self.run_errors,
        }

    def save(self, directory):
        directory = Path(directory).expanduser()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = self.started_at.replace(":", "").replace("+", "_")
        path = directory / f"sync-{stamp}-{self.run_id}.json"
        # Write privately, then publish atomically without replacing any history.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=directory,
                prefix=".sync-",
                suffix=".tmp",
                encoding="utf-8",
                delete=False,
            ) as target:
                temporary = target.name
                json.dump(self.snapshot(), target, indent=2, ensure_ascii=True)
                target.write("\n")
                target.flush()
                os.fsync(target.fileno())
            os.link(temporary, path)
        finally:
            if temporary is not None:
                os.unlink(temporary)
        return path


def safe_error_message(value):
    from searchify.ai.ollama_client import _DATABASE_ERRORS, _SAFE_ERROR_MESSAGES

    known = (
        set(_SAFE_ERROR_MESSAGES)
        | set(_STALE_MESSAGES)
        | {
            "Provider error details redacted to protect file contents and credentials.",
            "Provider connection failed or timed out; check the local runtime and network.",
            "Local file is missing.",
            "Permission denied reading the local file.",
            "Local file or network I/O failed.",
            "Ollama is unavailable. Start the Ollama app or run ollama serve, then retry.",
        }
    )
    known.update(f"{code}: {text}" for code, text in _DATABASE_ERRORS.items())
    if isinstance(value, str) and not _sensitive(value):
        if value in known or re.fullmatch(
            r"Ollama HTTP error [1-5][0-9]{2}( after retries)?\.", value
        ):
            return value
        if re.fullmatch(
            r"(Provider returned HTTP [1-5][0-9]{2}; response details redacted\.|HTTP [1-5][0-9]{2}; check provider availability and configuration\.)",
            value,
        ):
            return value
    return "Error details redacted; inspect the recorded exception type and stage."
