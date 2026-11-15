"""Literal Rich rendering, with cached console and a dependency-free fallback."""

import os
import re
from functools import lru_cache


def literal(value):
    text = str(value)
    for key, secret in os.environ.items():
        if len(secret) >= 4 and re.search(
            r"(TOKEN|PASSWORD|SECRET|CREDENTIAL|(?:^|_)KEY(?:_|$))", key, re.IGNORECASE
        ):
            text = text.replace(secret, "[redacted]")
    text = re.sub(
        r"(?:eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?|sk-[A-Za-z0-9_-]{8,}|sb_secret_[A-Za-z0-9_-]+)",
        "[redacted]",
        text,
    )
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "?", text)


@lru_cache(maxsize=1)
def _console():
    try:
        from rich.console import Console
    except ImportError:
        return None
    return Console()


def line(value=""):
    text = "\n".join(literal(part) for part in str(value).split("\n"))
    console = _console()
    if console is None:
        print(text)
    else:
        console.print(text, markup=False, highlight=False, soft_wrap=True)


def table(title, columns, rows):
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
