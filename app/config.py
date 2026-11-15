import os
import secrets


def data_dir() -> str:
    return os.environ.get("SITEMAP_DATA_DIR", os.path.join(os.getcwd(), "data"))


def secure_cookies() -> bool:
    """HTTPS-only cookies are opt-in, so the app also works over plain http on a LAN."""
    return os.environ.get("SITEMAP_SECURE_COOKIES", "") == "1"


def secret_key(directory: str) -> str:
    """SITEMAP_SECRET_KEY if set, else a key generated once and kept in the data folder."""
    if os.environ.get("SITEMAP_SECRET_KEY"):
        return os.environ["SITEMAP_SECRET_KEY"]
    path = os.path.join(directory, "secret_key")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        key = secrets.token_urlsafe(48)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key)
        return key
