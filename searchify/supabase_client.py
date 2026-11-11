
import os
import json
import keyring

from pathlib import Path
from getpass import getpass
from dotenv import load_dotenv
from supabase import create_client

load_dotenv(Path(__file__).parent / ".env")

SERVICE = "searchify-supabase"
ACCOUNT = "default"


def save_session(session):
    keyring.set_password(
        SERVICE,
        ACCOUNT,
        json.dumps({
            "access_token": session.access_token,
            "refresh_token": session.refresh_token,
        }),
    )


def authenticate():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")

    if not url or not key:
        raise RuntimeError("Missing Supabase environment variables.")

    client = create_client(url, key)

    # Try restoring the saved session.
    stored = keyring.get_password(SERVICE, ACCOUNT)

    if stored:
        try:
            tokens = json.loads(stored)

            response = client.auth.set_session(
                tokens["access_token"],
                tokens["refresh_token"],
            )

            session = response.session

            if session:
                save_session(session)
                print(f"Session restored: {session.user.id}")
                return client, session.user.id

        except Exception:
            print("Saved session expired or invalid. Sign in again.")

        try:
            keyring.delete_password(SERVICE, ACCOUNT)
        except keyring.errors.PasswordDeleteError:
            pass

    # Fresh login.
    email = input("Supabase email: ").strip()
    password = getpass("Supabase password: ")

    response = client.auth.sign_in_with_password({
        "email": email,
        "password": password,
    })

    if not response.session:
        raise RuntimeError("Authentication failed.")

    save_session(response.session)

    print(f"Authenticated: {response.user.id}")
    print("Session saved to macOS Keychain.")

    return client, response.user.id


if __name__ == "__main__":
    authenticate()
