"""
CLI Output Module - Safe Rich Rendering with Redaction

This module provides console output utilities with automatic secret redaction.
It uses Rich for pretty formatting when available, with a plain-text fallback.

Key safety features:
- Automatic redaction of environment variable secrets (TOKEN, PASSWORD, SECRET, KEY, etc.)
- Redaction of common token patterns (JWT, API keys, Supabase secrets)
- Control character sanitization
- Cached Rich console for performance

Used by: All CLI commands for safe output
"""

import os
import re
from functools import lru_cache


def literal(value):
    """
    Sanitize a value for safe display by redacting secrets.

    Redaction rules:
    1. Environment variables matching secret patterns (TOKEN, PASSWORD, SECRET, CREDENTIAL, KEY)
    2. JWT tokens (eyJ...)
    3. API keys (sk-...)
    4. Supabase secrets (sb_secret_)
    5. Control characters replaced with ?

    Args:
        value: Any value to sanitize

    Returns:
        Sanitized string safe for console output
    """
    text = str(value)
    # Redact environment variable values that look like secrets
    for key, secret in os.environ.items():
        if len(secret) >= 4 and re.search(
            r"(TOKEN|PASSWORD|SECRET|CREDENTIAL|(?:^|_)KEY(?:_|$))", key, re.IGNORECASE
        ):
            text = text.replace(secret, "[redacted]")
    # Redact common token patterns
    text = re.sub(
        r"(?:eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?|sk-[A-Za-z0-9_-]{8,}|sb_secret_[A-Za-z0-9_-]+)",
        "[redacted]",
        text,
    )
    # Replace control characters
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "?", text)


@lru_cache(maxsize=1)
def _console():
    """
    Get cached Rich console instance.

    Returns:
        Rich Console instance, or None if Rich not available
    """
    try:
        from rich.console import Console
    except ImportError:
        return None
    return Console()


def line(value=""):
    """
    Print a line with automatic secret redaction.

    Args:
        value: String to print (newlines supported)
    """
    text = "\n".join(literal(part) for part in str(value).split("\n"))
    console = _console()
    if console is None:
        print(text)
    else:
        console.print(text, markup=False, highlight=False, soft_wrap=True)


def table(title, columns, rows):
    """
    Print a table with automatic secret redaction.

    Falls back to plain text if Rich is unavailable.

    Args:
        title: Table title
        columns: List of column headers
        rows: List of row tuples/lists
    """
    console = _console()
    if console is None:
        line(title)
        line(" | ".join(columns))
        for row in rows:
            line(" | ".join(str(value) for value in row))
        return
    from rich.table import Table
    from rich.text import Text

    result = Table(title=literal(title), show_header=True)
    for column in columns:
        result.add_column(literal(column))
    for row in rows:
        result.add_row(*(Text(literal(value)) for value in row))
    console.print(result)
