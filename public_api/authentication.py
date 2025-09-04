import hashlib
import hmac
import secrets


def generate_long_lived_token() -> str:
    """
    Generates a random string of 32 characters to be used as a
    long-lived token for the system user.
    This token is not JWT encoded and is intended for long-term use.
    The token is valid until it is explicitly revoked or the user is deleted.
    The token is stored as a hash in the database for security.
    """
    return secrets.token_urlsafe(32)


def hash_long_lived_token(token: str) -> str:
    """
    Hashes the long-lived token using the configured hashing algorithm.
    This is used to securely store the token in the database.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def verify_long_lived_token(token: str, stored_long_lived_token_hash: str) -> bool:
    """
    Verifies the long-lived token against the stored hash.
    Returns True if the token matches, False otherwise.
    """
    return hmac.compare_digest(hash_long_lived_token(token), stored_long_lived_token_hash)
