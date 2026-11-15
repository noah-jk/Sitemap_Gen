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


def _check_password(password: str) -> None:
    if len(password) < 12:
        raise ValueError("password must be at least 12 characters")


def create_user(store: Store, username: str, password: str) -> None:
    _check_password(password)
    with closing(store.registry()) as reg:
        if reg.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise ValueError(f"user {username} already exists; use set-password to change the password")
        reg.execute("INSERT INTO users(username, password_hash, created_at) VALUES (?,?,?)",
                    (username, hash_password(password), now_ms()))


def set_password(store: Store, username: str, password: str) -> None:
    """Change a password. Browsers logged in before the change are logged out."""
    _check_password(password)
    with closing(store.registry()) as reg:
        cur = reg.execute("UPDATE users SET password_hash = ?, password_changed_at = ? WHERE username = ?",
                          (hash_password(password), now_ms(), username))
    if cur.rowcount == 0:
        raise ValueError(f"no user called {username}")


def list_usernames(store: Store) -> list:
    with closing(store.registry()) as reg:
        return [r["username"] for r in reg.execute("SELECT username FROM users ORDER BY username")]


def authenticate(store: Store, username: str, password: str) -> Optional[int]:
    with closing(store.registry()) as reg:
        row = reg.execute("SELECT id, password_hash FROM users WHERE username = ?", (username,)).fetchone()
    if row is None:
        verify_password(password, _DUMMY_HASH)
        return None
    return row["id"] if verify_password(password, row["password_hash"]) else None


def session_valid(store: Store, user_id: int, logged_in_at: int) -> bool:
    """The user still exists and hasn't changed their password since this login."""
    with closing(store.registry()) as reg:
        row = reg.execute("SELECT password_changed_at FROM users WHERE id = ?", (user_id,)).fetchone()
    return row is not None and logged_in_at >= row["password_changed_at"]


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
