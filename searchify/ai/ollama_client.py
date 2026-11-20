"""
Ollama HTTP Client for Local AI Runtime

This module provides a secure, retry-capable HTTP client for communicating
with a local Ollama instance. It enforces strict security constraints:

- Only allows connections to local loopback addresses (localhost/127.0.0.1)
- Disallows HTTPS, authentication, paths, query parameters, fragments
- Disables HTTP redirects to prevent SSRF
- Implements exponential backoff retry for transient errors
- Sanitizes error messages to prevent leakage of file contents or credentials

All AI processing in Searchify runs through this client to ensure
privacy and reliability.

Used by: summarizer.py, embeddings.py, summary_service.py, sync_service.py
"""

import ipaddress
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from searchify.config.settings import settings


class OllamaError(RuntimeError):
    """
    Base exception for Ollama client errors.

    Attributes:
        http_status: HTTP status code if error originated from HTTP response
        request_id: Request ID from Ollama response headers (for debugging)
    """
    def __init__(self, message, http_status=None, request_id=None):
        super().__init__(message)
        self.http_status = http_status
        self.request_id = request_id


class _NoRedirects(HTTPRedirectHandler):
    """
    HTTP redirect handler that blocks all redirects.

    This prevents SSRF (Server-Side Request Forgery) by ensuring the client
    never follows redirects to external or unintended endpoints.
    """
    # Refuse redirects so requests carrying local source text stay at the validated loopback
    # endpoint.
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class OllamaClient:
    """
    HTTP client for local Ollama API with bounded retries.

    Enforces that the Ollama host is a local loopback HTTP address with no
    authentication, path, query, or fragment components. This ensures all
    AI processing stays on the local machine.

    Attributes:
        host: Base URL for Ollama API (e.g., "http://localhost:11434")
        timeout: Request timeout in seconds (default: 180)
        retries: Number of retry attempts for transient errors (default: 2)
    """

    def __init__(self, host=None, timeout=180, retries=2):
        """
        Initialize Ollama client with validation.

        Args:
            host: Ollama host URL (defaults to settings.OLLAMA_HOST)
            timeout: Request timeout in seconds
            retries: Number of retry attempts for transient errors

        Raises:
            ValueError: If host is not a valid local loopback HTTP address,
                       or if timeout/retries are invalid
        """
        self.host = (host or settings.OLLAMA_HOST).rstrip("/")
        parsed = urlsplit(self.host)

        # Validate host is a local loopback address
        try:
            local = ipaddress.ip_address(parsed.hostname).is_loopback
        except (ValueError, TypeError):
            local = parsed.hostname == "localhost"

        # Strict validation: only local HTTP, no auth, no path, no query, no fragment
        if (
            parsed.scheme != "http"
            or not local
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Ollama must use a local loopback HTTP address.")

        if retries < 0 or timeout <= 0:
            raise ValueError("Invalid Ollama retry or timeout configuration.")

        self.timeout, self.retries = timeout, retries
        # Build opener with no proxy, no redirects
        self.opener = build_opener(ProxyHandler({}), _NoRedirects())

    def check_available(self):
        """
        Probe the local Ollama runtime without sending prompts or file contents.

        Calls GET /api/tags to verify Ollama is running and responding.
        Uses a short timeout (min 5s) and retries with exponential backoff.

        Raises:
            OllamaUnavailableError: If Ollama cannot be reached after retries
        """
        request = Request(self.host + "/api/tags", method="GET")
        for attempt in range(self.retries + 1):
            try:
                with self.opener.open(
                    request, timeout=min(self.timeout, 5)
                ) as response:
                    data = json.load(response)
                if (
                    not isinstance(data, dict)
                    or not isinstance(data.get("models"), list)
                    or data.get("error")
                ):
                    raise ValueError("Invalid runtime response")
                return
            except (OSError, ValueError, UnicodeError):
                if attempt < self.retries:
                    time.sleep(2**attempt)
        raise OllamaUnavailableError(
            "Ollama is unavailable. Start the Ollama app or run `ollama serve`, "
            "check OLLAMA_HOST points to the local server, then retry the command."
        ) from None

    def post(self, endpoint, payload):
        """
        Send POST request to Ollama API with retry logic.

        Implements exponential backoff retry for transient HTTP errors:
        - 408 Request Timeout
        - 429 Too Many Requests
        - 500 Internal Server Error
        - 502 Bad Gateway
        - 503 Service Unavailable
        - 504 Gateway Timeout

        Non-transient errors (4xx except 408/429) are raised immediately.
        Connection errors and timeouts are retried.

        Args:
            endpoint: API endpoint (e.g., "/api/generate", "/api/embed")
            payload: JSON-serializable request payload

        Returns:
            Parsed JSON response dict

        Raises:
            OllamaError: On HTTP errors, connection failures, or invalid responses
        """
        request = Request(
            self.host + endpoint,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        last_http_status, last_request_id = None, None
        for attempt in range(self.retries + 1):
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    data = json.load(response)
                if not isinstance(data, dict) or data.get("error"):
                    raise OllamaError("Ollama returned an invalid response.")
                return data
            except HTTPError as error:
                last_http_status = error.code
                last_request_id = (
                    error.headers.get("x-request-id") if error.headers else None
                )
                # Non-transient errors: fail immediately
                if error.code not in {408, 429, 500, 502, 503, 504}:
                    raise OllamaError(
                        f"Ollama HTTP error {error.code}.",
                        http_status=error.code,
                        request_id=last_request_id,
                    ) from None
            except (URLError, TimeoutError, ConnectionError):
                last_http_status = None
            except (ValueError, UnicodeError):
                raise OllamaError("Ollama returned invalid JSON.") from None
            if attempt < self.retries:
                time.sleep(2**attempt)
        if last_http_status is not None:
            raise OllamaError(
                f"Ollama HTTP error {last_http_status} after retries.",
                http_status=last_http_status,
                request_id=last_request_id,
            ) from None
        raise OllamaError("Ollama unavailable after retries.") from None


class OllamaUnavailableError(OllamaError):
    """
    Raised when the local Ollama runtime cannot be reached before processing starts.

    This is distinct from transient errors during processing - it indicates
    Ollama is not running or not accessible at the configured host.
    """
    pass


# Exception bodies can contain source excerpts, request payloads or credentials.
# Preserve only messages generated by Searchify itself, never arbitrary provider text.
_SAFE_ERROR_MESSAGES = frozenset(
    {
        "Ollama returned an invalid response.",
        "Ollama returned invalid JSON.",
        "Ollama unavailable after retries.",
        "Ollama did not return a complete summary.",
        "Ollama returned an invalid embedding response.",
        "Expected 768 finite numeric embedding dimensions.",
        "Expected a nonzero embedding vector.",
        "Summary upsert returned no record.",
        "Embedding upsert returned no record.",
        "File not found in Supabase after upsert.",
        "Summary reproduced original file contents.",
        "Summary may contain sensitive source text.",
        "Only derived summary text and provenance may be persisted.",
        "No extracted content to summarize.",
    }
)

# PostgreSQL error codes mapped to human-readable messages
_DATABASE_ERRORS = {
    "23503": "Foreign key constraint failed.",
    "23505": "Unique constraint failed.",
    "23502": "Required database field is missing.",
    "23514": "Database check constraint failed.",
    "42501": "Database permission denied; check authentication and RLS.",
    "42P01": "Database table is missing; check migrations.",
    "42703": "Database column is missing; check migrations.",
    "PGRST301": "Authentication token could not be verified.",
    "PGRST302": "Authentication is required.",
    "PGRST204": "Database column is missing from the schema cache.",
    "PGRST205": "Database table is missing from the schema cache.",
}


def format_processing_error(stage, error):
    """
    Format a processing error for logging/reporting without leaking sensitive data.

    This function sanitizes error messages to ensure no file contents, credentials,
    or provider error details are exposed. Only known-safe error messages from
    Searchify's own code are preserved; everything else is replaced with a generic
    redacted message.

    Args:
        stage: Processing stage ("extraction", "summarization", "embedding", "persistence")
        error: Exception that occurred

    Returns:
        Formatted error string: "stage: ExceptionType: sanitized_message"
    """
    import errno
    import re

    stage = (
        stage
        if stage in {"extraction", "summarization", "embedding", "persistence"}
        else "persistence"
    )
    exception_type = type(error).__name__
    # Validate exception type name is a safe identifier
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,79}", exception_type):
        exception_type = "Exception"
    message = (
        "Provider error details redacted to protect file contents and credentials."
    )
    raw = error.args[0] if error.args else None
    # Preserve known-safe Searchify-generated messages
    if isinstance(raw, str) and raw in _SAFE_ERROR_MESSAGES:
        message = raw
    elif isinstance(error, HTTPError):
        if isinstance(error.code, int) and 100 <= error.code <= 599:
            message = (
                f"HTTP {error.code}; check provider availability and configuration."
            )
    elif isinstance(error, (TimeoutError, ConnectionError, URLError)):
        message = "Provider connection failed or timed out; check the local runtime and network."
    elif isinstance(error, OSError):
        message = {
            errno.ENOENT: "Local file is missing.",
            errno.EACCES: "Permission denied reading the local file.",
            errno.EPERM: "Permission denied reading the local file.",
        }.get(error.errno, "Local file or network I/O failed.")
    elif isinstance(raw, str) and re.fullmatch(
        r"Ollama HTTP error [1-5][0-9]{2}( after retries)?\.", raw
    ):
        message = raw
    # Check for PostgreSQL error codes
    code = getattr(error, "code", None)
    if not isinstance(code, str) and isinstance(raw, dict):
        code = raw.get("code")
    if isinstance(code, str) and code in _DATABASE_ERRORS:
        message = f"{code}: {_DATABASE_ERRORS[code]}"
    return f"{stage}: {exception_type}: {message}"


def check_processing_runtime(summarizer, embeddings):
    """
    Verify both summarization and embedding runtimes are available.

    Checks each distinct Ollama client (by host) once. Injected non-Ollama
    providers may not have a check_available method.

    Args:
        summarizer: Summarizer instance with .client attribute
        embeddings: Embeddings instance with .client attribute

    Raises:
        OllamaUnavailableError: If either runtime is unavailable (with processing_stage attribute set)
    """
    checked = set()
    for stage, provider in (("summarization", summarizer), ("embedding", embeddings)):
        client = getattr(provider, "client", None)
        key = getattr(client, "host", None)
        key = key if isinstance(key, str) else id(client)
        if client is not None and key not in checked:
            try:
                client.check_available()
            except OllamaUnavailableError as error:
                error.processing_stage = stage
                raise
            checked.add(key)
