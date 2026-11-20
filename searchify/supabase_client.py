# Interactive Supabase authentication shared by owner-scoped repositories.
# SessionService validates target identities; keyring stores target-bound tokens.
# Authenticated clients are cached per target and API-key fingerprint, while
# read-only health diagnostics use a separate path that never refreshes sessions.

import hashlib
import json
from getpass import getpass

import keyring

from searchify.config.settings import settings
from searchify.services.session_service import (
    KEYRING_SERVICE,
    canonical_target,
    decode_session,
    session_account,
)
from supabase import Client, create_client

SERVICE = KEYRING_SERVICE
# Unscoped legacy sessions cannot be safely attributed and remain untouched.
_clients = {}


# Capture and validate the target and key together before any authentication work.
def _configuration(supabase_url=None, supabase_key=None):
    url = settings.SUPABASE_URL if supabase_url is None else supabase_url
    key = settings.SUPABASE_KEY if supabase_key is None else supabase_key
    if not url or not isinstance(key, str) or not key.strip():
        raise RuntimeError("Missing Supabase configuration.")
    return canonical_target(url), key


# Validate target-bound access/refresh tokens before replacing the matching keyring entry.
def save_session(session, supabase_url=None):
    target = canonical_target(
        settings.SUPABASE_URL if supabase_url is None else supabase_url
    )
    stored = json.dumps(
        {
            "target": target,
            "access_token": session.access_token,
            "refresh_token": session.refresh_token,
        }
    )
    decode_session(stored, target)
    keyring.set_password(SERVICE, session_account(target), stored)


# Delete only this target’s keyring entry; a missing entry is already logged out.
def clear_session(supabase_url=None):
    target = canonical_target(
        settings.SUPABASE_URL if supabase_url is None else supabase_url
    )
    try:
        keyring.delete_password(SERVICE, session_account(target))
    except keyring.errors.PasswordDeleteError:
        pass


# Construct an unauthenticated SDK client from validated configuration.
def create_supabase_client(supabase_url=None, supabase_key=None) -> Client:
    target, key = _configuration(supabase_url, supabase_key)
    return create_client(target, key)


# Restore and persist refreshed tokens, or prompt for password sign-in if restoration fails.
def authenticate(supabase_url=None, supabase_key=None):
    target, key = _configuration(supabase_url, supabase_key)
    client = create_supabase_client(target, key)

    stored = keyring.get_password(SERVICE, session_account(target))

    if stored:
        try:
            tokens = decode_session(stored, target)

            response = client.auth.set_session(
                tokens["access_token"], tokens["refresh_token"]
            )

            session = response.session

            if session and response.user:
                save_session(session, target)
                return client, response.user.id

        except Exception:  # noqa: BLE001 - session restoration failure is non-fatal
            print(
                "Session restoration failed. Cached credentials were not displayed; sign in again."
            )

        clear_session(target)

    email = input("Supabase email: ").strip()
    password = getpass("Supabase password: ")

    response = client.auth.sign_in_with_password({"email": email, "password": password})

    if not response.session or not response.user:
        raise RuntimeError("Authentication failed.")

    save_session(response.session, target)

    print("Authentication successful.")
    return client, response.user.id


# Reuse a client only while both target URL and API-key fingerprint match.
def _authenticated_entry():
    target, key = _configuration()
    fingerprint = hashlib.sha256(key.encode()).digest()
    cached = _clients.get(target)
    if cached is None or cached[2] != fingerprint:
        client, owner = authenticate(target, key)
        cached = (client, owner, fingerprint)
        _clients[target] = cached
    return cached


# Resolve the authenticated SDK client; first use may prompt for credentials.
def get_client():
    return _authenticated_entry()[0]


# Return the same cached authentication identity used by repository owner filters.
def get_owner_id():
    return _authenticated_entry()[1]


# Evict this target’s client and tokens even if remote sign-out fails.
def logout():
    target = canonical_target(settings.SUPABASE_URL)
    cached = _clients.pop(target, None)
    if cached is not None:
        try:
            cached[0].auth.sign_out()
        except Exception:  # noqa: BLE001,S110 - sign-out failure is non-fatal
            pass
    clear_session(target)
    print("Logged out successfully.")


if __name__ == "__main__":
    client = get_client()
    print(f"Connected. User: {get_owner_id()}")
