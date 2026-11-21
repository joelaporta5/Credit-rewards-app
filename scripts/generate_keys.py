"""Print fresh random secrets for your .env file (or Render environment variables).

    python scripts/generate_keys.py

Keep TOKEN_ENCRYPTION_KEY forever: if it changes, stored Plaid connections can't be
decrypted and you'd have to reconnect (which uses Trial slots in Production).
"""

import secrets

from cryptography.fernet import Fernet

print(f"ADMIN_API_KEY={secrets.token_urlsafe(32)}")
print(f"WALLET_API_KEY={secrets.token_urlsafe(32)}")
print(f"SESSION_SECRET={secrets.token_urlsafe(32)}")
print(f"TOKEN_ENCRYPTION_KEY={Fernet.generate_key().decode()}")
