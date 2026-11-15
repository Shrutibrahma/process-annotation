"""File-based study storage. One folder per study: original video, playback proxy, study.json."""
from __future__ import annotations

import contextvars
import functools
import json
import os
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# Shared root: models, accounts and (with accounts on) one folder per team under workspaces/.
DATA_DIR = Path(os.environ.get("WORKSTUDY_DATA", Path(__file__).resolve().parent.parent / "data"))
_workspace: contextvars.ContextVar[Path | None] = contextvars.ContextVar("workspace", default=None)


def data_dir() -> Path:
    """The signed-in team's folder (studies, stations); the data folder itself when accounts are off."""
    return _workspace.get() or DATA_DIR


def studies_dir() -> Path:
    return data_dir() / "studies"


def stations_dir() -> Path:
    return data_dir() / "stations"


@contextmanager
def workspace(path: Path | None):
    token = _workspace.set(path)
    try:
        yield
    finally:
        _workspace.reset(token)


def bind(fn):
    """fn bound to the current team's folder, for background tasks and threads."""
    ws = _workspace.get()

    @functools.wraps(fn)
    def run(*a, **kw):
        with workspace(ws):
            return fn(*a, **kw)
    return run

_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_lock = threading.RLock()


class StudyNotFound(KeyError):
    pass


def new_id() -> str:
    return uuid.uuid4().hex


def study_dir(sid: str) -> Path:
    if not _ID_RE.match(sid):
        raise StudyNotFound(sid)
    return studies_dir() / sid


def _json_path(sid: str) -> Path:
    return study_dir(sid) / "study.json"


def save(sid: str, study: dict[str, Any]) -> None:
    path = _json_path(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    study["updated_at"] = time.time()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".study-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(study, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # atomic on Windows and POSIX
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load(sid: str) -> dict[str, Any]:
    path = _json_path(sid)
    if not path.exists():
        raise StudyNotFound(sid)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def update(sid: str, **fields: Any) -> dict[str, Any]:
    with _lock:
        study = load(sid)
        study.update(fields)
        save(sid, study)
        return study


def list_studies() -> list[dict[str, Any]]:
    out = []
    if not studies_dir().exists():
        return out
    for d in studies_dir().iterdir():
        if not (d.is_dir() and _ID_RE.match(d.name)):
            continue
        try:
            s = load(d.name)
        except (StudyNotFound, json.JSONDecodeError, OSError):
            continue
        out.append({
            "id": s["id"],
            "number": s.get("number"),
            "header": s.get("header", {}),
            "status": s.get("status"),
            "created_at": s.get("created_at"),
            "duration": s.get("duration"),
            "cycles": len(s.get("cycles", [])),
            "steps": len(s.get("steps", [])),
        })
    out.sort(key=lambda s: s.get("created_at") or 0, reverse=True)
    return out


def station_key(header: dict[str, str]) -> str:
    """A station is project + line/operation. Empty when neither is filled in."""
    parts = [header.get("project", ""), header.get("line", "")]
    key = re.sub(r"[^a-z0-9]+", "-", " ".join(p for p in parts if p).lower()).strip("-")
    return key[:80]


def load_station(key: str) -> list[dict[str, Any]] | None:
    if not key:
        return None
    path = stations_dir() / f"{key}.json"
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("layout") or None
    except (OSError, json.JSONDecodeError):
        return None


def save_station(key: str, layout: list[dict[str, Any]]) -> None:
    if not key:
        return
    stations_dir().mkdir(parents=True, exist_ok=True)
    path = stations_dir() / f"{key}.json"
    fd, tmp = tempfile.mkstemp(dir=stations_dir(), suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"key": key, "saved_at": time.time(), "layout": layout}, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def forget_station(key: str) -> None:
    if key:
        (stations_dir() / f"{key}.json").unlink(missing_ok=True)


def mutate(sid: str, fn) -> dict[str, Any]:
    """Load, change in place with fn(study), save, all under the lock."""
    with _lock:
        study = load(sid)
        fn(study)
        save(sid, study)
        return study
