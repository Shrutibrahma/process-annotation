"""Accounts and team workspaces for the hosted app.

WORKSTUDY_AUTH=1 turns accounts on (the default in the Docker image). Each account belongs to one team;
a team's studies and station templates live in their own folder under DATA/workspaces/<team id>.
People join a team with its invite code, or create a new team when they sign up.

Sign-up can be limited with WORKSTUDY_SIGNUP_CODE (an access code you hand out with the link), and
Gemini spend is capped per team with WORKSTUDY_MONTHLY_ANALYSES video analyses a month.
Passwords are stored as scrypt hashes; the session is an HMAC-signed, HttpOnly cookie.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from . import store

COOKIE = "nl_session"
SESSION_DAYS = 30
_lock = threading.RLock()
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def enabled() -> bool:
    return os.environ.get("WORKSTUDY_AUTH", "0") == "1"


def _accounts_path() -> Path:
    return store.DATA_DIR / "accounts.json"


def _load() -> dict[str, Any]:
    try:
        with open(_accounts_path(), encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, json.JSONDecodeError):
        d = {}
    d.setdefault("users", {})
    d.setdefault("teams", {})
    return d


def _save(d: dict[str, Any]) -> None:
    path = _accounts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".accounts-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _secret() -> bytes:
    env = os.environ.get("WORKSTUDY_SECRET")
    if env:
        return env.encode()
    path = store.DATA_DIR / ".session-secret"
    with _lock:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(secrets.token_hex(32), encoding="utf-8")
        return path.read_text(encoding="utf-8").strip().encode()


def _hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"


def _check(password: str, stored: str) -> bool:
    try:
        _, salt, _h = stored.split("$")
        return hmac.compare_digest(_hash(password, base64.b64decode(salt)), stored)
    except (ValueError, TypeError):
        return False


def _team_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(2))


# ---------------------------------------------------------------- sessions

def make_cookie(uid: str) -> str:
    exp = int(time.time()) + SESSION_DAYS * 86400
    msg = f"{uid}.{exp}"
    sig = hmac.new(_secret(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{msg}.{sig}"


def user_from_cookie(value: str | None) -> dict[str, Any] | None:
    if not value or value.count(".") != 2:
        return None
    uid, exp, sig = value.split(".")
    good = hmac.new(_secret(), f"{uid}.{exp}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good) or not exp.isdigit() or int(exp) < time.time():
        return None
    u = _load()["users"].get(uid)
    return {**u, "id": uid} if u else None


def workspace_dir(team_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{16}", team_id):
        raise AuthError(400, "Invalid team.")
    return store.DATA_DIR / "workspaces" / team_id


# ---------------------------------------------------------------- accounts

def signup(email: str, name: str, password: str, team_name: str = "", team_code: str = "",
           access_code: str = "") -> dict[str, Any]:
    need = os.environ.get("WORKSTUDY_SIGNUP_CODE", "")
    if os.environ.get("WORKSTUDY_SIGNUP", "open") == "closed":
        raise AuthError(403, "Sign-up is closed. Ask your administrator for an account.")
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise AuthError(422, "Enter a valid email address.")
    if len(password) < 8:
        raise AuthError(422, "Use a password of at least 8 characters.")
    name = name.strip()[:80] or email.split("@")[0]
    with _lock:
        d = _load()
        if any(u["email"] == email for u in d["users"].values()):
            raise AuthError(409, "An account with this email already exists. Sign in instead.")
        code = team_code.strip().upper()
        if code:
            tid = next((t for t, v in d["teams"].items() if v["code"] == code), None)
            if not tid:
                raise AuthError(404, "No team has that invite code.")
        else:
            # a new team needs the access code (when one is set); joining with an invite code does not
            if need and not hmac.compare_digest(access_code.strip(), need):
                raise AuthError(403, "That access code is not right. Ask the person who shared the link.")
            tid = secrets.token_hex(8)
            first = not d["teams"]
            d["teams"][tid] = {"name": team_name.strip()[:80] or f"{name}'s team", "code": _team_code(),
                               "created_at": time.time(), "usage": {}}
            workspace_dir(tid).mkdir(parents=True, exist_ok=True)
            if first and os.environ.get("WORKSTUDY_SEED_FIRST_TEAM") == "1":
                _seed(workspace_dir(tid))
        uid = secrets.token_hex(8)
        d["users"][uid] = {"email": email, "name": name, "password": _hash(password), "team": tid,
                           "created_at": time.time(),
                           "role": "member" if code else "owner"}
        _save(d)
    return {**d["users"][uid], "id": uid}


def _seed(dst: Path) -> None:
    """The first team on a server gets a copy of the studies and templates already in the data
    folder (the local, no-accounts data). Videos are hard-linked where possible, so no extra space."""
    import shutil

    def link_or_copy(src, target):
        try:
            os.link(src, target)
        except OSError:
            shutil.copy2(src, target)
    for sub in ("studies", "stations"):
        src = store.DATA_DIR / sub
        if src.is_dir() and not (dst / sub).exists():
            shutil.copytree(src, dst / sub, copy_function=link_or_copy,
                            ignore=shutil.ignore_patterns("*.bak", ".study-*"))


def login(email: str, password: str) -> dict[str, Any]:
    email = email.strip().lower()
    d = _load()
    for uid, u in d["users"].items():
        if u["email"] == email and _check(password, u["password"]):
            return {**u, "id": uid}
    time.sleep(0.5)  # slows password guessing
    raise AuthError(401, "Wrong email or password.")


def me(user: dict[str, Any]) -> dict[str, Any]:
    d = _load()
    t = d["teams"].get(user["team"], {})
    month = time.strftime("%Y-%m")
    return {"auth": True, "id": user["id"], "email": user["email"], "name": user["name"], "role": user.get("role"),
            "team": {"name": t.get("name", ""), "code": t.get("code", "") if user.get("role") == "owner" or
                     os.environ.get("WORKSTUDY_SHOW_CODE", "1") == "1" else "",
                     "members": [u["name"] for u in d["users"].values() if u["team"] == user["team"]],
                     "analyses_this_month": t.get("usage", {}).get(month, 0), "monthly_limit": monthly_limit()}}


def monthly_limit() -> int:
    return int(os.environ.get("WORKSTUDY_MONTHLY_ANALYSES", "30"))


def count_analysis(team_id: str) -> None:
    """One video analysis (paid Gemini run) for the team; refuses past the monthly limit."""
    month = time.strftime("%Y-%m")
    with _lock:
        d = _load()
        t = d["teams"].get(team_id)
        if t is None:
            raise AuthError(403, "Unknown team.")
        used = t.setdefault("usage", {}).get(month, 0)
        if used >= monthly_limit():
            raise AuthError(429, f"Your team has used its {monthly_limit()} video analyses for this month. "
                                 "Ask the administrator to raise WORKSTUDY_MONTHLY_ANALYSES.")
        t["usage"][month] = used + 1
        _save(d)
