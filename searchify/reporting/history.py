"""Read-only local report history validation, independent of CLI presentation."""

import json
from pathlib import Path


# Return newest-name-first valid sync reports plus an invalid-file count without creating
# any files.
def load_reports(directory):
    valid, invalid = [], 0
    for path in sorted(Path(directory).expanduser().glob("sync-*.json"), reverse=True):
        try:
            # History is untrusted input: avoid redirected/oversized reads and
            # require nonnegative integer counts before the CLI consumes them.
            if path.is_symlink() or path.stat().st_size > 64 * 1024 * 1024:
                raise ValueError("Unsafe or oversized report")
            with path.open(encoding="utf-8") as source:
                report = json.load(source)
            counts = report["counts"]
            if any(
                type(counts.get(key)) is not int or counts[key] < 0
                for key in ("total", "completed", "failed", "stale", "skipped")
            ):
                raise ValueError("Invalid counts")
            valid.append((path, report))
        except (OSError, ValueError, KeyError, TypeError):
            invalid += 1
    return valid, invalid
