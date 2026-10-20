import base64
import hashlib
import hmac
import re
import secrets
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError

from service.settings import secret

hasher = PasswordHasher()
_dummy = hasher.hash(secrets.token_urlsafe(32))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def token():
    return secrets.token_urlsafe(32)


def runtime_token(user_id):
    return hmac.new(secret('manager_key').encode(), ('runtime:' + user_id).encode(), hashlib.sha256).hexdigest()


def csrf_for(session_id):
    return hmac.new(secret('app_key').encode(), ('csrf:' + session_id).encode(), hashlib.sha256).hexdigest()


def password_ok(password, encoded=None):
    try:
        return hasher.verify(encoded or _dummy, password)
    except (VerificationError, InvalidHashError):
        return False


def validate_password(value):
    if len(value) < 12 or len(value) > 128:
        raise ValueError('비밀번호는 12~128자여야 합니다.')


def validate_username(value):
    value = value.strip().lower()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{2,63}', value):
        raise ValueError('ID는 영문 소문자·숫자·._- 3~64자여야 합니다.')
    return value


def challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')


def valid_loopback(uri):
    try:
        u = urlsplit(uri)
        return u.scheme == 'http' and u.hostname == '127.0.0.1' and bool(u.port) and not u.username and not u.fragment and not u.query
    except ValueError:
        return False
