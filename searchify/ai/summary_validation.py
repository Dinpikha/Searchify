"""
Summary Privacy Validation Module

This module provides critical privacy protection by validating LLM-generated
summaries before they are persisted. It prevents accidental leakage of
source code, credentials, or sensitive data into the search index.

Validation layers:
1. Basic completeness: Summary must be non-empty string
2. Source echo detection: Rejects summaries containing verbatim source lines (>=40 chars)
3. Credential pattern detection: Regex patterns for API keys, tokens, passwords, secrets
4. JWT/Token format detection: Catches common token formats (eyJ..., sk-..., sb_secret_)
5. Environment variable scanning: Checks if any env vars with sensitive names appear in summary

This is a defense-in-depth measure - the LLM system prompt also instructs
against reproducing source content, but this validation provides a hard
guarantee before persistence.

Used by: summary_service.py (process_file method)
"""

import os
import re


def validate_summary(summary, content):
    """
    Validate a generated summary for privacy and completeness.

    Performs multiple checks to ensure the summary is safe to persist:
    1. Non-empty string check
    2. Source content echo detection (verbatim lines >= 40 chars)
    3. Credential pattern detection (API keys, tokens, passwords, secrets)
    4. Common token format detection (JWT, API keys, Supabase secrets)
    5. Environment variable leakage detection

    Args:
        summary: Generated summary text from LLM
        content: Original file content (for echo detection)

    Returns:
        The validated summary string (unchanged)

    Raises:
        ValueError: If summary fails any validation check with descriptive message
    """
    # Basic completeness check
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("Ollama did not return a complete summary.")

    # Detect verbatim source reproduction
    # Check if entire content or any significant line (>=40 chars) appears in summary
    if content.strip() in summary or any(
        len(line.strip()) >= 40 and line.strip() in summary
        for line in content.splitlines()
    ):
        raise ValueError("Summary reproduced original file contents.")

    # Detect credential patterns: key=value or key: value formats
    if re.search(
        r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[:=]\s*\S+",
        summary,
    ) or re.search(
        # Detect common token formats: JWT (eyJ...), API keys (sk-...), Supabase secrets
        r"(?:eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|sk-[A-Za-z0-9_-]{8,}|sb_secret_)",
        summary,
    ):
        raise ValueError("Summary may contain sensitive source text.")

    # Scan environment variables for sensitive values that might have leaked
    # Only checks vars with sensitive names (TOKEN, PASSWORD, SECRET, etc.) and length >= 8
    for key, value in os.environ.items():
        if (
            len(value) >= 8
            and re.search(
                r"(TOKEN|PASSWORD|SECRET|CREDENTIAL|API_KEY|SUPABASE_KEY)",
                key,
                re.IGNORECASE,
            )
            and value in summary
        ):
            raise ValueError("Summary may contain sensitive source text.")

    return summary
