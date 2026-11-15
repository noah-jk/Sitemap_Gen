"""Password hashing (stdlib scrypt), user lookup, and a small failed-login limiter."""
import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from contextlib import closing
from typing import Optional

from app.db import Store, now_ms

N, R, P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"scrypt${N}${R}${P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        expected = base64.b64decode(digest)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                             n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


# Verifying against this when the username doesn't exist keeps response times equal.
_DUMMY_HASH = hash_password(secrets.token_hex(16))


def create_user(store: Store, username: str, password: str) -> None:
    if len(password) < 12:
        raise ValueError("password must be at least 12 characters")
    with closing(store.registry()) as reg:
        reg.execute("INSERT INTO users(username, password_hash, created_at) VALUES (?,?,?)",
                    (username, hash_password(password), now_ms()))


def authenticate(store: Store, username: str, password: str) -> Optional[int]:
    with closing(store.registry()) as reg:
        row = reg.execute("SELECT id, password_hash FROM users WHERE username = ?", (username,)).fetchone()
    if row is None:
        verify_password(password, _DUMMY_HASH)
        return None
    return row["id"] if verify_password(password, row["password_hash"]) else None


def user_exists(store: Store, user_id: int) -> bool:
    with closing(store.registry()) as reg:
        return reg.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is not None


class LoginLimiter:
    """At most `limit` failed logins per client per `window` seconds."""

    def __init__(self, limit: int = 5, window: float = 300.0):
        self.limit, self.window = limit, window
        self._fails = defaultdict(deque)
        self._lock = threading.Lock()

    def blocked(self, client: str) -> bool:
        with self._lock:
            q = self._fails[client]
            cutoff = time.monotonic() - self.window
            while q and q[0] < cutoff:
                q.popleft()
            return len(q) >= self.limit

    def failed(self, client: str) -> None:
        with self._lock:
            self._fails[client].append(time.monotonic())

    def reset(self, client: str) -> None:
        with self._lock:
            self._fails.pop(client, None)
