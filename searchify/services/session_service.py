"""
Session Service - Secure Supabase Authentication Management

This module handles authentication session storage and validation for
Supabase connections. It uses the system keyring for secure token storage
and implements strict target validation to prevent token reuse across
different Supabase projects.

Key security features:
- Canonical URL normalization prevents spoofing
- Target-bound sessions: tokens only valid for specific Supabase URL
- SHA-256 based account identifiers in keyring
- Token validation on retrieval (non-empty access/refresh tokens)

Used by: supabase_client.py, CLI auth commands
"""

import hashlib
import json
from urllib.parse import urlsplit, urlunsplit

# Keyring service name for Searchify Supabase sessions
KEYRING_SERVICE = "searchify-supabase"


def canonical_target(url):
    """
    Normalize and validate a Supabase target URL.

    Enforces strict URL format:
    - Only http/https schemes
    - Must have hostname
    - No username/password in URL
    - No query parameters or fragments
    - No whitespace
    - Default ports (80/443) omitted from host

    Args:
        url: Supabase project URL string

    Returns:
        Canonicalized URL string

    Raises:
        ValueError: If URL format is invalid
    """
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
    """
    Generate keyring account identifier for a Supabase target.

    Creates a deterministic, target-specific account name using
    SHA-256 hash of the canonical URL. This ensures tokens for
    different Supabase projects are stored separately.

    Args:
        url: Supabase project URL

    Returns:
        Keyring account string (e.g., "target-v1:a1b2c3d4...")
    """
    return "target-v1:" + hashlib.sha256(canonical_target(url).encode()).hexdigest()


def decode_session(stored, url):
    """
    Validate and decode a stored session from keyring.

    Verifies:
    1. Stored value is valid JSON object
    2. Target matches requested URL (canonicalized)
    3. Contains non-empty access_token and refresh_token strings

    Args:
        stored: Raw string from keyring
        url: Supabase project URL for validation

    Returns:
        Decoded session dict with access_token, refresh_token

    Raises:
        ValueError: If session is invalid, expired, or for different target
    """
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
