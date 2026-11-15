"""Read supported local files into memory; never cache extracted text."""

from pathlib import Path

TEXT_EXTENSIONS = frozenset(
    {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".html",
        ".css",
        ".scss",
        ".json",
        ".md",
        ".txt",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".sql",
        ".sh",
        ".zsh",
        ".bash",
        ".java",
        ".c",
        ".cpp",
        ".h",
        ".go",
        ".rs",
        ".swift",
        ".kt",
        ".kts",
        ".gradle",
        ".properties",
        ".pro",
        ".rst",
        ".xml",
        ".log",
        ".csv",
    }
)
MAX_CONTENT_SIZE = 2 * 1024 * 1024


def extract_content(
    path, max_size=MAX_CONTENT_SIZE, extensions=TEXT_EXTENSIONS, strict=False
):
    path = Path(path)
    if path.suffix.lower() not in extensions:
        return ""
    try:
        # Bound the read even if the file grows after stat().
        with path.open("rb") as source:
            data = source.read(min(max_size, MAX_CONTENT_SIZE) + 1)
        if len(data) > min(max_size, MAX_CONTENT_SIZE) or b"\0" in data:
            return ""
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    except OSError:
        if strict:
            raise
        return ""
