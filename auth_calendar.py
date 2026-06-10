"""
Run this ONCE locally to authenticate with Google Calendar.
It will open your browser for consent and write token.json.
"""

import os
import sys

from google.auth import exceptions as google_auth_exceptions
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES           = ["https://www.googleapis.com/auth/calendar"]
CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")
TOKEN_PATH       = os.getenv("GOOGLE_TOKEN_PATH",       "token.json")


def main():
    if not os.path.exists(CREDENTIALS_PATH):
        print(f"❌  '{CREDENTIALS_PATH}' not found.")
        print("    Download it from Google Cloud Console:")
        print("    APIs & Services → Credentials → OAuth 2.0 Client IDs → Download JSON")
        sys.exit(1)

    creds = None
    if os.path.exists(TOKEN_PATH):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        except Exception as exc:
            print(f"⚠️  Failed to load token.json: {exc}")
            print("    The file may be corrupt. It will be overwritten with a new token.")
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            # ── attempt silent refresh ──────────────────────────────────
            try:
                creds.refresh(Request())
            except google_auth_exceptions.RefreshError as exc:
                print("❌  Google OAuth refresh token is invalid or revoked.")
                print(f"    Underlying error: {exc}")
                print()
                print("    This happens when:")
                print("     • The token hasn't been used for 6+ months.")
                print("     • You revoked access via your Google Account.")
                print("     • The OAuth consent screen settings changed.")
                print("     • The app is in \"testing\" mode (tokens expire after 7 days).")
                print()
                print("    To fix this, re-authenticate:")
                print("    Delete token.json and run this script again.")
                sys.exit(1)
            except google_auth_exceptions.TransportError as exc:
                print(f"❌  Network error during token refresh: {exc}")
                print("    Check your internet connection and try again.")
                sys.exit(1)
            except google_auth_exceptions.GoogleAuthError as exc:
                print(f"❌  Google OAuth error during token refresh: {exc}")
                sys.exit(1)
            except Exception as exc:
                print(f"❌  Unexpected error during token refresh: {exc}")
                sys.exit(1)
        else:
            flow  = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    print(f"✅  token.json written successfully!")


if __name__ == "__main__":
    main()
