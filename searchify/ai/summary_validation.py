"""Prevent obvious source/credential echoes before persisting derived summaries."""

import os
import re


def validate_summary(summary, content):
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("Ollama did not return a complete summary.")
    if content.strip() in summary or any(
        len(line.strip()) >= 40 and line.strip() in summary
        for line in content.splitlines()
    ):
        raise ValueError("Summary reproduced original file contents.")
    if re.search(
        r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[:=]\s*\S+",
        summary,
    ) or re.search(
        r"(?:eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|sk-[A-Za-z0-9_-]{8,}|sb_secret_)",
        summary,
    ):
        raise ValueError("Summary may contain sensitive source text.")
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
