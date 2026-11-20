"""
File Content Extraction Module

This module provides functionality to read text content from local files
for processing by the Searchify pipeline. It enforces strict limits and
safety checks to prevent resource exhaustion and ensure only valid
text files are processed.

Key design principles:
- Never caches extracted text (content exists only in memory during processing)
- Enforces maximum file size (2 MB default) to prevent memory issues
- Validates UTF-8 encoding; rejects binary files
- Rejects files containing null bytes (indicator of binary content)
- Supports configurable extension allowlist

Used by: scanner.py (during directory scan), indexer.py (during indexing),
summary_service.py (during summarization)
"""

from pathlib import Path

# Supported text file extensions for content extraction.
# This frozenset is used for O(1) membership testing during scanning.
# Extensions cover common source code, markup, config, and documentation formats.
TEXT_EXTENSIONS = frozenset(
    {
        ".py",  # Python
        ".js",  # JavaScript
        ".jsx",  # React JavaScript
        ".ts",  # TypeScript
        ".tsx",  # React TypeScript
        ".html",  # HTML
        ".css",  # CSS
        ".scss",  # SCSS
        ".json",  # JSON
        ".md",  # Markdown
        ".txt",  # Plain text
        ".yaml",  # YAML
        ".yml",  # YAML (alternative)
        ".toml",  # TOML
        ".ini",  # INI config
        ".cfg",  # Config files
        ".sql",  # SQL
        ".sh",  # Shell scripts
        ".zsh",  # Zsh scripts
        ".bash",  # Bash scripts
        ".java",  # Java
        ".c",  # C
        ".cpp",  # C++
        ".h",  # C/C++ headers
        ".go",  # Go
        ".rs",  # Rust
        ".swift",  # Swift
        ".kt",  # Kotlin
        ".kts",  # Kotlin script
        ".gradle",  # Gradle build files
        ".properties",  # Java properties
        ".pro",  # Qt project / Prolog
        ".rst",  # reStructuredText
        ".xml",  # XML
        ".log",  # Log files
        ".csv",  # CSV
    }
)

# Maximum file size for content extraction (2 MB).
# Files larger than this are skipped during scanning/summarization.
# This limit applies to the actual read operation, not just stat() size.
MAX_CONTENT_SIZE = 2 * 1024 * 1024


def extract_content(
    path, max_size=MAX_CONTENT_SIZE, extensions=TEXT_EXTENSIONS, strict=False
):
    """
    Extract text content from a file with safety bounds.

    This function reads a file's content into memory for processing.
    It performs multiple validation checks:
    1. Extension allowlist check
    2. File size bound (reads at most max_size + 1 bytes to detect overflow)
    3. Null byte detection (rejects binary files)
    4. UTF-8 decoding validation

    Args:
        path: Path to the file (string or Path object)
        max_size: Maximum bytes to read (default: MAX_CONTENT_SIZE = 2MB)
        extensions: Allowable file extensions (default: TEXT_EXTENSIONS)
        strict: If True, raise OSError on read failure; otherwise return empty string

    Returns:
        File content as UTF-8 string if valid, empty string otherwise.
        Empty string is returned for: unsupported extension, size overflow,
        null bytes, decoding errors, or OS errors (unless strict=True).

    Note:
        The +1 byte read allows detection of files that exceed max_size
        without actually loading the entire oversized file into memory.
    """
    path = Path(path)

    # Quick extension check before any I/O
    if path.suffix.lower() not in extensions:
        return ""

    try:
        # Bound the read even if the file grows after stat().
        # Read max_size + 1 bytes to detect overflow without loading entire file.
        with path.open("rb") as source:
            data = source.read(min(max_size, MAX_CONTENT_SIZE) + 1)

        # Reject if file exceeds size limit (read returned max_size + 1 bytes)
        # or if null bytes are present (binary file indicator)
        if len(data) > min(max_size, MAX_CONTENT_SIZE) or b"\0" in data:
            return ""

        # Attempt UTF-8 decode; reject if not valid text
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return ""

    except OSError:
        # File not found, permission denied, etc.
        if strict:
            raise
        return ""
