import base64
import hashlib
import hmac
import re
import secrets


PASSWORD_ITERATIONS = 600_000
PASSWORD_BYTES = 32
PASSWORD_SALT_BYTES = 16
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


def validate_password(password):
    if not isinstance(password, str) or not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError(f"Пароль має містити від {MIN_PASSWORD_LENGTH} до {MAX_PASSWORD_LENGTH} символів.")


def new_password_salt():
    return secrets.token_bytes(PASSWORD_SALT_BYTES)


def derive_password(password, salt, iterations=PASSWORD_ITERATIONS):
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
        dklen=PASSWORD_BYTES,
    )


def encode_password_hash(salt, digest, iterations=PASSWORD_ITERATIONS):
    encoded_salt = base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")
    encoded_digest = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"pbkdf2_sha256${iterations}${encoded_salt}${encoded_digest}"


def hash_password(password):
    validate_password(password)
    salt = new_password_salt()
    return encode_password_hash(salt, derive_password(password, salt))


def decode_password_hash(encoded):
    try:
        algorithm, iterations, salt, digest = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256" or int(iterations) != PASSWORD_ITERATIONS:
            return None
        decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        salt_bytes = decode(salt)
        digest_bytes = decode(digest)
        if len(salt_bytes) != PASSWORD_SALT_BYTES or len(digest_bytes) != PASSWORD_BYTES:
            return None
        return salt_bytes, digest_bytes
    except (AttributeError, ValueError):
        return None


def verify_password(password, encoded):
    if not isinstance(password, str):
        return False
    parsed = decode_password_hash(encoded)
    if parsed is None:
        return False
    salt, expected = parsed
    return hmac.compare_digest(derive_password(password, salt), expected)


def hash_session_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session_token():
    return secrets.token_urlsafe(32)


def username_from_email(email):
    local_part = email.split("@", 1)[0].lower()
    normalized = re.sub(r"[^a-z0-9_.-]+", "-", local_part).strip("-._")
    return (normalized or "onegram")[:30]