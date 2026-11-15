"""Pure target identities and validation for secure session storage."""

import hashlib
import json
from urllib.parse import urlsplit, urlunsplit

KEYRING_SERVICE = "searchify-supabase"


def canonical_target(url):
    try:
        if not isinstance(url, str) or not url.strip():
            raise ValueError
        url = url.strip()
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(c.isspace() for c in url)
        ):
            raise ValueError
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        if port is not None and (parsed.scheme, port) not in {
            ("http", 80),
            ("https", 443),
        }:
            host += f":{port}"
        return urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))
    except ValueError:
        raise ValueError("Invalid Supabase target URL.") from None


def session_account(url):
    return "target-v1:" + hashlib.sha256(canonical_target(url).encode()).hexdigest()


def decode_session(stored, url):
    try:
        value = json.loads(stored)
        if not isinstance(value, dict) or value.get("target") != canonical_target(url):
            raise ValueError
        for name in ("access_token", "refresh_token"):
            if not isinstance(value.get(name), str) or not value[name].strip():
                raise ValueError
        return value
    except (TypeError, ValueError):
        raise ValueError(
            "Cached session is invalid for this Supabase target."
        ) from None
