"""
Run this ONCE locally to authenticate with Google Calendar.
It will open your browser for consent and write token.json.

After running:
  python auth_calendar.py

Then encode the token for Railway:
  python -c "import base64; print(base64.b64encode(open('token.json','rb').read()).decode())"

Paste the output as the GOOGLE_TOKEN_B64 environment variable in Railway.
"""

import os
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
        return

    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow  = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    print(f"✅  token.json written successfully!")
    print()
    print("📋  Copy this value into Railway as GOOGLE_TOKEN_B64:")
    print()
    import base64
    with open(TOKEN_PATH, "rb") as f:
        encoded = base64.b64encode(f.read()).decode()
    print(encoded)


if __name__ == "__main__":
    main()
