"""Process Annotation - Work Standardization & Kaizen: video -> work instructions and time study, plus improvement copilot and line balancing.

Run:  uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from . import align, auth, chart, copilot, export, feedback, media, observation, store, template
from .engine import CycleReadings, EngineError, EngineEvent, TimingCycle, TimingResult, get_engine, normalize

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("workstudy")

ALLOWED_EXT = {".mp4", ".mov", ".avi", ".webm", ".mpeg", ".mpg", ".wmv"}
MAX_UPLOAD_BYTES = int(os.environ.get("WORKSTUDY_MAX_UPLOAD_MB", "2048")) * 1024 * 1024
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Process Annotation - Work Standardization & Kaizen")


def _load_or_404(sid: str) -> dict:
    try:
        return store.load(sid)
    except store.StudyNotFound:
        raise HTTPException(404, "Study not found.")


# ---------------------------------------------------------------- accounts

OPEN_API = {"/api/config", "/api/auth/signup", "/api/auth/login", "/api/auth/logout", "/api/me"}


@app.middleware("http")
async def team_workspace(request: Request, call_next):
    """With accounts on, every /api call needs a signed-in user and works in that user's team folder."""
    if not auth.enabled() or not request.url.path.startswith("/api/") or request.url.path in OPEN_API:
        return await call_next(request)
    user = auth.user_from_cookie(request.cookies.get(auth.COOKIE))
    if not user:
        return JSONResponse({"detail": "Please sign in."}, status_code=401)
    request.state.user = user
    with store.workspace(auth.workspace_dir(user["team"])):
        return await call_next(request)


def _set_session(resp: Response, request: Request, uid: str) -> None:
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    resp.set_cookie(auth.COOKIE, auth.make_cookie(uid), max_age=auth.SESSION_DAYS * 86400, httponly=True,
                    samesite="lax", secure=secure, path="/")


class SignupIn(BaseModel):
    email: str = Field(max_length=200)
    name: str = Field(default="", max_length=80)
    password: str = Field(min_length=1, max_length=200)
    team_name: str = Field(default="", max_length=80)
    team_code: str = Field(default="", max_length=20)
    access_code: str = Field(default="", max_length=200)


class LoginIn(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=200)


@app.post("/api/auth/signup")
def signup(body: SignupIn, request: Request, response: Response) -> dict:
    if not auth.enabled():
        raise HTTPException(404, "Accounts are off on this server.")
    try:
        user = auth.signup(body.email, body.name, body.password, body.team_name, body.team_code, body.access_code)
    except auth.AuthError as e:
        raise HTTPException(e.status, str(e))
    _set_session(response, request, user["id"])
    return auth.me(user)


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request, response: Response) -> dict:
    if not auth.enabled():
        raise HTTPException(404, "Accounts are off on this server.")
    try:
        user = auth.login(body.email, body.password)
    except auth.AuthError as e:
        raise HTTPException(e.status, str(e))
    _set_session(response, request, user["id"])
    return auth.me(user)


@app.post("/api/auth/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def whoami(request: Request) -> dict:
    if not auth.enabled():
        return {"auth": False}
    user = auth.user_from_cookie(request.cookies.get(auth.COOKIE))
    if not user:
        raise HTTPException(401, "Please sign in.")
    return auth.me(user) | {"signup_code_required": bool(os.environ.get("WORKSTUDY_SIGNUP_CODE"))}


def _count_paid_run(request: Request) -> None:
    """Video analyses cost Gemini money: count them per team against the monthly limit."""
    user = getattr(request.state, "user", None)
    if not user or os.environ.get("WORKSTUDY_ENGINE", "gemini").lower() == "mock":
        return
    try:
        auth.count_analysis(user["team"])
    except auth.AuthError as e:
        raise HTTPException(e.status, str(e))


# ---------------------------------------------------------------- pages

@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


BRAND = STATIC / "brand"


@app.get("/brand/{name}", include_in_schema=False)
def brand_file(name: str) -> FileResponse:
    """Logo and theme images (public: the sign-in page uses them)."""
    path = BRAND / name
    if not re.fullmatch(r"[a-z0-9-]+\.(png|svg)", name) or not path.is_file():
        raise HTTPException(404, "Not found.")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/config")
def config() -> dict:
    return {"sheet_decimals": export.SHEET_DECIMALS,
            "engine": os.environ.get("WORKSTUDY_ENGINE", "gemini").lower(),
            # focused = work chart + step breakdown only; full = every module
            "ui": os.environ.get("WORKSTUDY_UI", "focused").lower(),
            "align": align.available() and os.environ.get("WORKSTUDY_ALIGN", "1") != "0",
            "timing_model": os.environ.get("WORKSTUDY_TIMING_MODEL") or os.environ.get("WORKSTUDY_GEMINI_MODEL", "gemini-3.8-flash"),
            "auth": auth.enabled(), "signup_code_required": bool(os.environ.get("WORKSTUDY_SIGNUP_CODE")),
            "signup_open": os.environ.get("WORKSTUDY_SIGNUP", "open") != "closed"}


# ---------------------------------------------------------------- studies

@app.get("/api/studies")
def list_studies() -> list[dict]:
    return store.list_studies()


# Videos arrive in chunks (POST /api/uploads, then PUT each piece): proxies and tunnels in front of a
# hosted app often cap one request at ~100 MB, and a dropped connection only repeats one chunk.
UPLOAD_RE = re.compile(r"^[a-f0-9]{32}$")
CHUNK_MAX = 48 * 1024 * 1024


class UploadStart(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=1)


def _upload_path(uid: str) -> Path:
    if not UPLOAD_RE.match(uid):
        raise HTTPException(404, "Upload not found.")
    return store.data_dir() / "uploads" / f"{uid}.part"


@app.post("/api/uploads", status_code=201)
def start_upload(body: UploadStart) -> dict:
    ext = Path(body.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"Unsupported file type '{ext}'. Use one of: {', '.join(sorted(ALLOWED_EXT))}.")
    if body.size > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Video is larger than {MAX_UPLOAD_BYTES >> 20} MB.")
    d = store.data_dir() / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob("*.part"):  # abandoned uploads older than a day
        if time.time() - old.stat().st_mtime > 86400:
            old.unlink(missing_ok=True)
    uid = store.new_id()
    _upload_path(uid).write_bytes(b"")
    return {"upload_id": uid, "chunk": 32 * 1024 * 1024}


@app.put("/api/uploads/{uid}")
async def upload_chunk(uid: str, request: Request, offset: int = 0) -> dict:
    path = _upload_path(uid)
    if not path.exists():
        raise HTTPException(404, "Upload not found or expired. Start again.")
    have = path.stat().st_size
    if offset != have:  # tells the browser where to continue
        return JSONResponse({"received": have, "detail": "offset mismatch"}, status_code=409)
    n = 0
    with open(path, "ab") as fh:
        async for part in request.stream():
            n += len(part)
            if n > CHUNK_MAX or have + n > MAX_UPLOAD_BYTES:
                fh.truncate(have)
                raise HTTPException(413, "Chunk too large.")
            fh.write(part)
    return {"received": have + n}


@app.post("/api/studies", status_code=201)
async def create_study(
    background: BackgroundTasks,
    request: Request,
    video: UploadFile | None = File(None),
    upload_id: str = Form(""), filename: str = Form(""),
    project: str = Form(""), product: str = Form(""),
    job: str = Form(""), line: str = Form(""), takt: str = Form(""),
    operator: str = Form(""), prepared_by: str = Form(""), date: str = Form(""),
) -> dict:
    name = video.filename if video is not None else filename
    ext = Path(name or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"Unsupported file type '{ext}'. Use one of: {', '.join(sorted(ALLOWED_EXT))}.")
    part = _upload_path(upload_id) if video is None else None
    if part is not None and (not part.exists() or part.stat().st_size == 0):
        raise HTTPException(400, "The uploaded video is missing or empty. Upload it again.")
    _count_paid_run(request)
    sid = store.new_id()
    d = store.study_dir(sid)
    d.mkdir(parents=True)
    original = d / f"original{ext}"
    if part is not None:
        os.replace(part, original)
    else:
        written = 0
        try:
            with open(original, "wb") as fh:
                while chunk := await video.read(1 << 20):
                    written += len(chunk)
                    if written > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, f"Video is larger than {MAX_UPLOAD_BYTES >> 20} MB.")
                    fh.write(chunk)
        except BaseException:
            shutil.rmtree(d, ignore_errors=True)
            raise
        if written == 0:
            shutil.rmtree(d, ignore_errors=True)
            raise HTTPException(400, "The uploaded video is empty.")

    header = {"project": project.strip(), "product": product.strip(), "job": job.strip(), "line": line.strip(), "takt": takt.strip(),
              "operator": operator.strip(), "prepared_by": prepared_by.strip(), "date": date.strip()}
    store.save(sid, {
        "id": sid, "number": _next_number(), "created_at": time.time(), "status": "processing", "progress": "Queued",
        "error": None, "header": header, "source_name": name, "duration": None,
        "engine": None, "cycles": [], "steps": [], "representative_cycle": 1,
        "warnings": [], "notes": "",
    })
    background.add_task(store.bind(run_analysis), sid)
    return {"id": sid}


def _next_number() -> int:
    return max((s.get("number") or 0 for s in store.list_studies()), default=0) + 1


def run_analysis(sid: str) -> None:
    d = store.study_dir(sid)

    def progress(msg: str) -> None:
        store.update(sid, progress=msg)

    try:
        original = next(d.glob("original.*"))
        proxy = d / "proxy.mp4"
        progress("Converting video")
        media.make_proxy(original, proxy)
        duration = media.probe_duration(proxy)
        store.update(sid, duration=round(duration, 2))
        study = store.load(sid)
        header = study["header"]
        key = template.station_key(header)
        tpl = template.load(key)
        engine = get_engine()
        engine.rules = feedback.rules_text(tpl, ("timing", "general"))  # what the engineer taught this station
        engine.layout_rules = feedback.rules_text(tpl, ("chart", "general"))
        calibration = None
        try:
            if tpl and tpl.get("elements"):
                # the station's elements are fixed: Gemini only reports when each break point happens
                if os.environ.get("WORKSTUDY_TIMING", "per_cycle") == "per_cycle" and hasattr(engine, "time_elements_per_cycle"):
                    ref = template.reference_for_engine(key, tpl)
                    if ref and align.available() and os.environ.get("WORKSTUDY_ALIGN", "1") != "0":
                        align.prefetch(sid)  # frames for alignment are read while Gemini works
                    res = engine.time_elements_per_cycle(proxy, header, duration, progress, tpl["elements"], ref)
                    calibration = template.use_reference_marks(res, ref, sid, tpl["elements"])
                    calibration = _align_readings(sid, res, tpl, calibration, progress)
                else:
                    res = engine.time_elements(proxy, header, duration, progress, tpl["elements"], tpl.get("layout"))
                if template.apply_location_suggestions(tpl, res.locations):
                    template.save(key, tpl)
                norm = template.normalize_timing(res, tpl, duration)
                notes, mode = res.notes, "template"
            else:
                result = engine.analyze(proxy, header, duration, progress,
                                        layout=(tpl or {}).get("layout") or None)
                norm = normalize(result, duration)
                notes, mode = result.notes, "draft"
            rep = [s for s in norm["steps"] if s["cycle"] == norm["representative_cycle"]]
            transcripts = engine.transcribe(proxy, rep, progress) if rep else {}
            for s in norm["steps"]:
                s["transcript"] = transcripts.get(s["id"], [])
        finally:
            engine.close()
        if mode == "draft" and key:
            # first study of this station: its elements and layout become the station's draft template
            draft = template.from_study(norm, header)
            if tpl and tpl.get("layout"):
                draft["layout"] = tpl["layout"]
            template.save(key, draft)
        if calibration:  # cycle numbers after normalization
            for c in calibration["cycles"]:
                near = min(norm["cycles"], key=lambda x: abs(x["start"] - c["cycle_start"]), default=None)
                c["cycle"] = near["index"] if near and abs(near["start"] - c["cycle_start"]) <= 5 else c["cycle_src"]
            idx = {c["cycle_src"]: c["cycle"] for c in calibration["cycles"]}
            for r in calibration["rows"]:
                r["cycle"] = idx.get(r.pop("cycle_src"))
        store.update(sid, status="ready", progress="", engine=engine.name, notes=notes.strip(),
                     station_key=key, template_used=bool(key), analysis_mode=mode,
                     layout_source=mode, calibration=calibration, **norm)
        log.info("study %s ready: %d cycles, %d steps, %d transcribed", sid, len(norm["cycles"]),
                 len(norm["steps"]), len(transcripts))
    except Exception as e:  # surface every failure to the UI rather than leaving it "processing"
        log.exception("analysis failed for %s", sid)
        msg = str(e) if isinstance(e, (EngineError, media.MediaError)) else f"{type(e).__name__}: {e}"
        store.update(sid, status="failed", progress="", error=msg)


def _align_readings(sid: str, res: TimingResult, tpl: dict, calibration: dict | None, progress) -> dict | None:
    """Second, free timing: every cycle aligned to the marked cycles. Per element, whichever of Gemini
    and alignment was closer to your marks (both timed blind) is used. Marked cycles keep the marks."""
    refs = template.references(tpl)
    if not refs or os.environ.get("WORKSTUDY_ALIGN", "1") == "0" or not align.available():
        return calibration
    ids = [e["id"] for e in tpl["elements"]]
    cycles = [{"index": c.index, "start": c.start, "end": c.end} for c in res.cycles]
    try:
        al = align.run(sid, cycles, refs, ids, progress)
    except Exception:  # alignment is an extra: Gemini's readings stand without it
        log.exception("alignment failed for %s", sid)
        return calibration
    marked = {}  # cycle index -> marks of that cycle
    for r in refs:
        if r.get("study_id") == sid:
            near = min(cycles, key=lambda c: abs(c["start"] - r["cycle_start"]), default=None)
            if near and abs(near["start"] - r["cycle_start"]) <= 5:
                marked[near["index"]] = r["marks"]
    gem_rows = {(r["cycle_src"], r["element_id"]): r.get("gemini") for r in (calibration or {}).get("rows", [])}
    rows = []
    for ci, marks in marked.items():
        a = al.get(ci)
        for k, eid in enumerate(ids):
            rows.append({"cycle_src": ci, "element_id": eid, "marked": marks.get(eid),
                         "gemini": gem_rows.get((ci, eid)), "aligned": a["times"][k] if a else None})
    choice = align.choose(rows, ids)
    by = {r.cycle: r for r in res.readings}
    readings = {c["index"]: list(by[c["index"]].times) if c["index"] in by else [None] * len(ids)
                for c in cycles if c["index"] not in marked}
    merged, unsure = align.merge(readings, al, ids, choice, align.trusted(rows, ids))
    for ci, times in merged.items():
        if ci in by:
            by[ci].times = times
            by[ci].uncertain = sorted(set(by[ci].uncertain) | set(unsure.get(ci, [])))
        else:
            res.readings.append(CycleReadings(cycle=ci, times=times, uncertain=unsure.get(ci, [])))
    if not rows:
        return calibration
    cyc_list = [{"cycle_src": ci, "cycle_start": next(c["start"] for c in cycles if c["index"] == ci)} for ci in marked]
    return {"cycles": cyc_list, "rows": rows, "leave_one_out": True, **align.report(rows, choice)}


def run_alignment(sid: str) -> None:
    """Re-time a finished study by alignment only: no Gemini call, no cost. Edited steps, marked
    cycles, X ticks and notes are kept."""
    def progress(msg: str) -> None:
        store.update(sid, progress=msg)

    study = store.load(sid)
    try:
        key = study.get("station_key") or template.station_key(study.get("header", {}))
        tpl = template.load(key)
        if not tpl or not tpl.get("elements"):
            raise EngineError("This station has no template yet.")
        if not template.references(tpl):
            raise EngineError("Mark at least one reference cycle first (Station template tab).")
        ids = [e["id"] for e in tpl["elements"]]
        cycles = sorted(study["cycles"], key=lambda c: c["start"])
        cur = {c["index"]: [None] * len(ids) for c in cycles}
        edited = set()
        for st in study["steps"]:
            if st["element_id"] in ids and st["cycle"] in cur:
                cur[st["cycle"]][ids.index(st["element_id"])] = st["start"]
                if st.get("edited"):
                    edited.add((st["cycle"], st["element_id"]))
        res = TimingResult(
            cycles=[TimingCycle(index=c["index"], start=c["start"], end=c["end"], x=bool(c.get("x_suggested")),
                                x_reason=c.get("x_reason", ""), x_time=c.get("x_time")) for c in cycles],
            readings=[CycleReadings(cycle=ci, times=list(t)) for ci, t in cur.items()],
            events=[EngineEvent(cycle=c["index"], time=e.get("time"), element_id=e.get("element_id", ""), text=e["text"])
                    for c in cycles for e in c.get("events") or [] if e.get("text")],
            representative_cycle=study.get("representative_cycle") or 1)
        # Gemini's blind readings of the marked cycles from the last Gemini run, to compare against
        gem = [{"cycle_src": r["cycle"], "element_id": r["element_id"], "gemini": r["gemini"]}
               for r in (study.get("calibration") or {}).get("rows", [])
               if r.get("gemini") is not None and r.get("cycle") in cur]
        ref = template.reference_for_engine(key, tpl)
        template.use_reference_marks(res, ref, sid, tpl["elements"])  # marked cycles take the marks
        calibration = _align_readings(sid, res, tpl, {"rows": gem}, progress)
        by = {r.cycle: r for r in res.readings}
        for ci, eid in edited:  # your edits win
            by[ci].times[ids.index(eid)] = cur[ci][ids.index(eid)]
        norm = template.normalize_timing(res, tpl, study["duration"] or 0)
        old = {c["index"]: c for c in cycles}
        old_steps = {(s["cycle"], s["element_id"]): s for s in study["steps"]}
        # normalize renumbers cycles in time order; map back by nearest start
        idx_map = {n["index"]: min(cycles, key=lambda c: abs(c["start"] - n["start"]))["index"] for n in norm["cycles"]}
        for c in norm["cycles"]:
            o = old[idx_map[c["index"]]]
            c.update(abnormal=o.get("abnormal", False), note=o.get("note", ""))
        for st in norm["steps"]:
            key2 = (idx_map.get(st["cycle"]), st["element_id"])
            o = old_steps.get(key2)
            if o:
                st["transcript"] = o.get("transcript", [])
            st["edited"] = key2 in edited
        if calibration:
            back = {v: k for k, v in idx_map.items()}
            for c in calibration["cycles"]:
                c["cycle"] = back.get(c["cycle_src"])
            for r in calibration["rows"]:
                r["cycle"] = back.get(r.pop("cycle_src"))
        store.update(sid, status="ready", progress="", calibration=calibration, timing="alignment", **norm)
        log.info("study %s re-timed by alignment", sid)
    except Exception as e:
        log.exception("alignment failed for %s", sid)
        msg = str(e) if isinstance(e, (EngineError, align.AlignError)) else f"{type(e).__name__}: {e}"
        store.update(sid, status="ready", progress="", align_error=msg)


@app.post("/api/studies/{sid}/align", status_code=202)
def align_study(sid: str, background: BackgroundTasks) -> dict:
    """Free re-timing from the marked reference cycles (no Gemini call)."""
    study = _ready(sid)
    if not align.available():
        raise HTTPException(501, "Alignment needs onnxruntime: pip install onnxruntime")
    store.update(sid, status="processing", progress="Queued: re-timing from your marked cycles", align_error=None)
    background.add_task(store.bind(run_alignment), sid)
    return {"id": study["id"]}


# ---------------------------------------------------------------- feedback (teach the app)

class FeedbackIn(BaseModel):
    text: str = Field(min_length=2, max_length=2000)
    tab: str = Field(default="", max_length=40)


class FeedbackApply(BaseModel):
    items: list[str] = []
    rules: list[str] = []
    descriptions: bool = False


def _study_tpl(study: dict) -> tuple[str, dict]:
    key = study.get("station_key") or template.station_key(study.get("header", {}))
    tpl = template.load(key) if key else None
    if not tpl or not tpl.get("elements"):
        raise HTTPException(409, "This study has no station template yet. Analyze it with a Line/Operation first.")
    return key, tpl


@app.post("/api/studies/{sid}/feedback")
def give_feedback(sid: str, body: FeedbackIn) -> dict:
    """Plain-language feedback -> proposed changes and rules. Nothing is applied yet."""
    study = _ready(sid)
    key, tpl = _study_tpl(study)
    try:
        prop = feedback.propose(study, tpl, body.text, body.tab)
    except EngineError as e:
        raise HTTPException(502, str(e))
    pending = {k: v for k, v in (study.get("feedback_pending") or {}).items()
               if time.time() - v.get("at", 0) < 24 * 3600}
    pending[prop["pid"]] = prop
    store.update(sid, feedback_pending=dict(list(pending.items())[-5:]))
    return {k: v for k, v in prop.items() if k != "items"} | {
        "items": [{k: v for k, v in it.items() if k != "op"} for it in prop["items"]]}


@app.post("/api/studies/{sid}/feedback/{pid}/apply")
def apply_feedback(sid: str, pid: str, body: FeedbackApply) -> dict:
    study = _ready(sid)
    prop = (study.get("feedback_pending") or {}).get(pid)
    if not prop:
        raise HTTPException(404, "That suggestion expired. Send the feedback again.")
    key, tpl = _study_tpl(study)
    done = feedback.apply(tpl, prop, body.items, body.rules)
    if body.descriptions and prop.get("descriptions"):
        try:
            desc = feedback.write_descriptions(study, tpl, prop["descriptions"]["style"])
        except EngineError as e:
            raise HTTPException(502, str(e))
        for e in tpl["elements"]:
            if e["id"] in desc:
                e["description"] = desc[e["id"]]
        done.append(f"Wrote step descriptions ({len(desc)} elements, style: {prop['descriptions']['style']})")
    if not done:
        raise HTTPException(422, "Tick at least one change to apply.")
    if any(it["op"]["type"] == "add" for it in prop["items"] if it["id"] in body.items):
        done.append("New elements have no times yet: re-time the study (Edit step times, Re-time from marks, or Re-analyze).")
    tpl.setdefault("feedback", []).append({"at": time.time(), "study_id": sid, "text": prop["text"],
                                           "reply": prop.get("reply", ""), "done": done})
    tpl["feedback"] = tpl["feedback"][-50:]
    tpl["status"] = "edited"
    tpl = template.save(key, tpl)
    # this study's steps follow the template's descriptions right away
    by = {e["id"]: e for e in tpl["elements"]}
    steps = study["steps"]
    for s in steps:
        e = by.get(s["element_id"])
        if e is not None and study.get("analysis_mode") == "template":
            s["description"] = e.get("description", "")
    pending = study.get("feedback_pending") or {}
    pending.pop(pid, None)
    study = store.update(sid, steps=steps, feedback_pending=pending)
    return {"done": done, "study": study, **_template_view(key, tpl)}


@app.delete("/api/stations/{key}/rules/{rid}")
def delete_rule(key: str, rid: str) -> dict:
    tpl = template.load(key)
    if not tpl:
        raise HTTPException(404, "No template for this station.")
    tpl["rules"] = [r for r in tpl.get("rules") or [] if r.get("id") != rid]
    return _template_view(key, template.save(key, tpl))


@app.post("/api/studies/{sid}/reanalyze", status_code=202)
def reanalyze(sid: str, background: BackgroundTasks, request: Request) -> dict:
    study = _load_or_404(sid)
    if study["status"] == "processing":
        raise HTTPException(409, "This study is already being analyzed.")
    _count_paid_run(request)
    store.update(sid, status="processing", progress="Queued", error=None)
    background.add_task(store.bind(run_analysis), sid)
    return {"id": sid}


@app.get("/api/studies/{sid}")
def get_study(sid: str) -> dict:
    study = _load_or_404(sid)
    study.setdefault("station_key", template.station_key(study.get("header", {})))
    return study


@app.delete("/api/studies/{sid}", status_code=204)
def delete_study(sid: str) -> Response:
    study = _load_or_404(sid)
    if study["status"] == "processing":
        raise HTTPException(409, "Wait for analysis to finish before deleting.")
    shutil.rmtree(store.study_dir(sid), ignore_errors=True)
    return Response(status_code=204)


# ---------------------------------------------------------------- edits

Category = Literal["value_add", "necessary_nva", "waste"]


class TranscriptLineIn(BaseModel):
    t: float
    text: str = Field(min_length=1)


class StepIn(BaseModel):
    id: str
    element_id: str = Field(min_length=1)
    cycle: int
    start: float
    end: float
    title: str
    breakpoint: str = ""
    description: str = ""
    category: Category
    hand: Literal["L", "R", "both"]
    tools: list[str] = []
    materials: list[str] = []
    key_points: list[str] = []
    transcript: list[TranscriptLineIn] = []
    confidence: Literal["high", "medium", "low"] = "medium"
    location_id: str = ""
    walk: bool = False
    covers_gap: bool = False
    edited: bool = False


class CycleIn(BaseModel):
    index: int
    start: float
    end: float
    abnormal: bool = False
    x_suggested: bool = False
    note: str = ""
    x_reason: str = ""
    x_time: float | None = None
    events: list[dict] = []


class StudyEdit(BaseModel):
    header: dict[str, str]
    cycles: list[CycleIn]
    steps: list[StepIn]
    representative_cycle: int


@app.put("/api/studies/{sid}")
def save_study(sid: str, edit: StudyEdit) -> dict:
    study = _load_or_404(sid)
    if study["status"] != "ready":
        raise HTTPException(409, "Only finished studies can be edited.")
    duration = study["duration"] or 0
    cyc = {c.index: c for c in edit.cycles}
    if edit.representative_cycle not in cyc:
        raise HTTPException(422, "The instruction cycle does not exist.")
    prev_end = 0.0
    for c in sorted(edit.cycles, key=lambda c: c.start):
        if not (0 <= c.start < c.end <= duration + 0.05) or c.start < prev_end - 0.01:
            raise HTTPException(422, f"Cycle {c.index} has invalid or overlapping times.")
        prev_end = c.end
    by_cycle: dict[int, list[StepIn]] = {}
    for s in edit.steps:
        if s.cycle not in cyc:
            raise HTTPException(422, f"Step '{s.title}' belongs to a cycle that does not exist.")
        if not (s.end - s.start >= 0.05):
            raise HTTPException(422, f"Step '{s.title}' must end after it starts.")
        c = cyc[s.cycle]
        if s.start < c.start - 0.01 or s.end > c.end + 0.01:
            raise HTTPException(422, f"Step '{s.title}' extends outside cycle {s.cycle}.")
        by_cycle.setdefault(s.cycle, []).append(s)
    for ci, ss in by_cycle.items():
        ss.sort(key=lambda s: s.start)
        for a, b in zip(ss, ss[1:]):
            if b.start < a.end - 0.01:
                raise HTTPException(422, f"Steps '{a.title}' and '{b.title}' overlap in cycle {ci}.")
    study = store.update(
        sid,
        header={k: v.strip() for k, v in edit.header.items()},
        cycles=[c.model_dump() for c in sorted(edit.cycles, key=lambda c: c.index)],
        steps=[s.model_dump() for s in edit.steps],
        representative_cycle=edit.representative_cycle,
    )
    return study


@app.post("/api/studies/{sid}/transcribe")
def transcribe_cycle(sid: str, cycle: int) -> dict:
    """Transcribe every step of one cycle (replaces existing transcripts for those steps)."""
    study = _load_or_404(sid)
    if study["status"] != "ready":
        raise HTTPException(409, "The study is not ready yet.")
    steps = sorted((s for s in study["steps"] if s["cycle"] == cycle), key=lambda s: s["start"])
    if not steps:
        raise HTTPException(404, f"Cycle {cycle} has no steps.")
    try:
        engine = get_engine()
        try:
            result = engine.transcribe(store.study_dir(sid) / "proxy.mp4", steps, lambda m: None)
        finally:
            engine.close()
    except EngineError as e:
        raise HTTPException(502, str(e))
    if not result:
        raise HTTPException(502, "No step could be transcribed. Try again.")

    def apply(st: dict) -> None:
        for s in st["steps"]:
            if s["id"] in result:
                s["transcript"] = result[s["id"]]
    return store.mutate(sid, apply)


# ---------------------------------------------------------------- work chart

class LocationIn(BaseModel):
    id: str = Field(min_length=1, max_length=20)
    label: str = Field(min_length=1, max_length=120)
    type: Literal["bin", "bench", "fixture", "equipment", "tool", "output"]
    column: Literal["far_left", "left", "center", "right", "far_right"]
    row: Literal["back", "middle", "front"]
    shelf: Literal["top", "middle", "bottom"] | None = None
    order: int = Field(ge=1, le=40)
    x: float | None = None
    y: float | None = None


class ElementIn(BaseModel):
    id: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=120)
    breakpoint: str = ""
    location_id: str = ""
    category: Literal["value_add", "necessary_nva", "waste"] = "necessary_nva"
    hand: Literal["L", "R", "both"] = "both"
    walk: bool = False
    breakpoint_visible: bool = True
    issue: str = ""
    location_suggested: bool = False
    breakpoint_suggested: bool = False   # drafted by the app from the wording, not yet checked
    quality: bool = False   # quality check at this step (diamond on the work chart)
    safety: bool = False    # safety point at this step (cross in circle)
    description: str = Field(default="", max_length=600)   # how the step is done (breakdown, instructions)
    note: str = Field(default="", max_length=300)          # hint for timing this element (taught by feedback)


class TemplateIn(BaseModel):
    line: str = ""
    elements: list[ElementIn]
    layout: list[LocationIn]
    manual: dict = {}


def _template_view(key: str, tpl: dict) -> dict:
    return {"key": key, "template": tpl, "issues": template.issues(tpl),
            "grid": template.slot_positions(tpl.get("layout") or [])}


@app.get("/api/stations/{key}")
def get_station(key: str) -> dict:
    tpl = template.load(key)
    if not tpl:
        raise HTTPException(404, "This station has no template yet. Analyze a video of it first, or import a sheet.")
    return _template_view(key, tpl)


@app.put("/api/stations/{key}")
def save_station(key: str, body: TemplateIn) -> dict:
    """The engineer's template: elements in their wording and order, layout, element locations."""
    if not template.valid_key(key):
        raise HTTPException(422, "Invalid station key.")
    ids = [e.id for e in body.elements]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "Element ids must be unique.")
    loc_ids = [l.id for l in body.layout]
    if len(set(loc_ids)) != len(loc_ids):
        raise HTTPException(422, "Location ids must be unique.")
    bad = [e.name for e in body.elements if e.location_id and e.location_id not in loc_ids]
    if bad:
        raise HTTPException(422, f"These elements use a location that is not in the layout: {', '.join(bad)}.")
    old = template.load(key) or {}
    tpl = {"line": body.line or old.get("line", ""), "status": "edited", "source": old.get("source", "video"),
           "elements": [e.model_dump() for e in body.elements],
           "layout": template.clean_layout([l.model_dump() for l in body.layout]),
           "manual": body.manual or old.get("manual", {}),
           "reference": old.get("reference"), "references": old.get("references", []),
           "rules": old.get("rules", []), "feedback": old.get("feedback", [])}
    return _template_view(key, template.save(key, tpl))


@app.post("/api/stations/{key}/draft-breakpoints")
def draft_breakpoints(key: str) -> dict:
    tpl = template.load(key)
    if not tpl:
        raise HTTPException(404, "No template for this station.")
    if not template.draft_breakpoints(tpl):
        raise HTTPException(422, "Every element already has a break point.")
    return _template_view(key, template.save(key, tpl))


@app.post("/api/stations/{key}/confirm-places")
def confirm_places(key: str) -> dict:
    tpl = template.load(key)
    if not tpl:
        raise HTTPException(404, "No template for this station.")
    template.confirm_places(tpl)
    return _template_view(key, template.save(key, tpl))


@app.delete("/api/stations/{key}", status_code=204)
def delete_station(key: str) -> Response:
    template.delete(key)
    return Response(status_code=204)


@app.post("/api/stations/{key}/import-sheet")
async def import_sheet(key: str, image: UploadFile = File(...)) -> dict:
    """Read element names, break points and times from a photo of a filled paper sheet (not saved)."""
    data = await image.read()
    if not data:
        raise HTTPException(400, "The image is empty.")
    mime = image.content_type or "image/jpeg"
    if not mime.startswith("image/"):
        raise HTTPException(400, "Upload a photo (JPEG, PNG, WebP or HEIC).")
    fd, tmp = tempfile.mkstemp(suffix=Path(image.filename or "sheet.jpg").suffix or ".jpg")
    os.close(fd)
    path = Path(tmp)
    try:
        path.write_bytes(data)
        engine = get_engine()
        try:
            sheet = engine.read_sheet(path, mime)
        finally:
            engine.close()
    except EngineError as e:
        raise HTTPException(502, str(e))
    finally:
        path.unlink(missing_ok=True)
    tpl = template.load(key) or {}
    elements, manual = template.elements_from_sheet(sheet, tpl.get("layout") or [])
    return {"elements": elements, "manual": manual, "header": sheet.header, "notes": sheet.notes}


class ReferenceIn(BaseModel):
    study_id: str
    cycle_start: float
    marks: dict[str, float]


@app.post("/api/stations/{key}/reference")
def save_reference(key: str, body: ReferenceIn) -> dict:
    """One marked reference cycle (up to 3 per station). Re-marking the same cycle replaces it."""
    tpl = template.load(key)
    if not tpl:
        raise HTTPException(404, "No template for this station.")
    study = _ready(body.study_id)
    ids = [e["id"] for e in tpl["elements"]]
    marks = {eid: round(float(t), 2) for eid, t in body.marks.items() if eid in ids}
    if not marks:
        raise HTTPException(422, "Mark at least one break point.")
    ordered = [marks[e] for e in ids if e in marks]
    if any(b2 < a2 for a2, b2 in zip(ordered, ordered[1:])):
        raise HTTPException(422, "Marks must follow the element order (each one later than the one before).")
    refs = [r for r in template.references(tpl) if not r.get("legacy")]
    same = next((r for r in refs if r["study_id"] == body.study_id and abs(r["cycle_start"] - body.cycle_start) <= 3), None)
    if same is None and len(refs) >= template.MAX_REFS:
        raise HTTPException(422, f"Up to {template.MAX_REFS} reference cycles. Remove one first.")
    rid = same["rid"] if same else next(f"r{i}" for i in range(1, 20) if f"r{i}" not in {r["rid"] for r in refs})
    video = store.study_dir(body.study_id) / "proxy.mp4"
    d = template.ref_dir(key) / rid
    d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("*.jpg"):
        f.unlink()
    for eid, t in marks.items():
        try:
            media.extract_frame(video, t, d / f"{eid}.jpg", width=480)
        except media.MediaError as e:
            raise HTTPException(422, str(e))
    entry = {"rid": rid, "study_id": body.study_id, "cycle_start": round(body.cycle_start, 2), "marks": marks,
             "study_number": study.get("number")}
    tpl["references"] = [r for r in refs if r["rid"] != rid] + [entry]
    tpl.pop("reference", None)
    return _template_view(key, template.save(key, tpl))


@app.delete("/api/stations/{key}/reference/{rid}")
def delete_reference(key: str, rid: str) -> dict:
    tpl = template.load(key)
    if not tpl:
        raise HTTPException(404, "No template for this station.")
    tpl["references"] = [r for r in template.references(tpl) if r["rid"] != rid and not r.get("legacy")]
    tpl.pop("reference", None)
    shutil.rmtree(template.ref_dir(key) / rid, ignore_errors=True)
    return _template_view(key, template.save(key, tpl))


@app.get("/api/stations/{key}/reference/{rid}/{eid}.jpg")
def reference_frame(key: str, rid: str, eid: str) -> FileResponse:
    if not (re.fullmatch(r"[A-Za-z0-9_-]{1,20}", eid) and re.fullmatch(r"r\d{1,2}", rid)):
        raise HTTPException(404, "Not found.")
    tpl = template.load(key)
    ref = next((r for r in template.references(tpl or {}) if r["rid"] == rid), None)
    path = template.ref_image(key, ref, eid) if ref else None
    if not path or not path.exists():
        raise HTTPException(404, "No reference frame.")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


@app.get("/api/stations/{key}/chart.{fmt}")
def station_chart(key: str, fmt: Literal["svg", "png", "pdf"]) -> Response:
    tpl = template.load(key)
    if not tpl:
        raise HTTPException(404, "No template for this station.")
    data = chart.render_template(tpl, {"line": tpl.get("line", "")}, fmt)
    media_type = {"svg": "image/svg+xml", "png": "image/png", "pdf": "application/pdf"}[fmt]
    return Response(data, media_type=media_type, headers={"Cache-Control": "no-cache"})


def _chart_or_422(study: dict, fmt: str) -> bytes:
    if not (chart.chart_input(study).get("layout")):
        raise HTTPException(422, "This study has no station layout. Analyze it again to create the work chart.")
    return chart.render(study, fmt)


@app.get("/api/studies/{sid}/chart.{fmt}")
def work_chart(sid: str, fmt: Literal["svg", "png", "pdf"], download: bool = False) -> Response:
    study = _ready(sid)
    data = _chart_or_422(study, fmt)
    media_type = {"svg": "image/svg+xml", "png": "image/png", "pdf": "application/pdf"}[fmt]
    headers = {"Cache-Control": "no-cache"}
    if download or fmt == "pdf":
        headers["Content-Disposition"] = f'attachment; filename="{_export_name(study, "-work-chart." + fmt)}"'
    return Response(data, media_type=media_type, headers=headers)


@app.get("/api/studies/{sid}/chart-check")
def work_chart_check(sid: str) -> dict:
    """Chart numbers vs breakdown numbers, overlaps, and elements without a valid location."""
    study = _ready(sid)
    ci = chart.chart_input(study)
    if not ci.get("layout"):
        raise HTTPException(422, "This study has no station layout.")
    res = chart.check(study)
    rows = export.breakdown_rows(study)
    valid = {loc["id"] for loc in ci["layout"]}
    steps = chart.instruction_steps(ci)
    return {
        "chart_numbers": [n for n, _ in res["numbers"]],
        "breakdown_numbers": [r[0] for r in rows],
        "numbers_match": [n for n, _ in res["numbers"]] == [r[0] for r in rows]
                          and [s["title"] for s in steps] == [r[1] for r in rows],
        "missing_location": [n for n, s in enumerate(steps, 1) if s.get("location_id") not in valid],
        "overlaps": res["issues"],
        "layout": ci["layout"],
        "template_used": ci is not study,
    }


@app.get("/api/studies/{sid}/export/breakdown")
def export_breakdown(sid: str) -> FileResponse:
    study = _ready(sid)
    fd, tmp = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        export.build_breakdown_xlsx(study, tmp_path)
    except export.ExportError as e:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(422, str(e))
    return FileResponse(
        tmp_path, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=_export_name(study, "-step-breakdown.xlsx"),
        background=BackgroundTask(tmp_path.unlink, missing_ok=True))


@app.get("/api/studies/{sid}/export/observation.{fmt}")
def export_observation(sid: str, fmt: Literal["pdf", "xlsx", "png"]) -> Response:
    """Manual Time Observation Sheet: PDF like the paper form, XLSX editable (+ step breakdown)."""
    study = _ready(sid)
    if fmt == "png":
        return Response(observation.render_png(study), media_type="image/png", headers={"Cache-Control": "no-cache"})
    name = _export_name(study, f"-time-study.{fmt}")
    if fmt == "pdf":
        return Response(observation.render_study_pdf(study), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    fd, tmp = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        export.build_observation_xlsx(study, tmp_path)
    except export.ExportError as e:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(422, str(e))
    return FileResponse(tmp_path, filename=name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        background=BackgroundTask(tmp_path.unlink, missing_ok=True))


@app.get("/api/studies/{sid}/cycle-comments")
def cycle_comments(sid: str) -> dict:
    """Fluctuation reasons computed from element times (shown until the engineer types their own)."""
    study = _ready(sid)
    return {str(k): v for k, v in observation.cycle_comments(study).items()}


@app.get("/api/studies/{sid}/export/prep.png")
def export_prep_png(sid: str) -> Response:
    study = _ready(sid)
    return Response(observation.render_prep_png(study), media_type="image/png", headers={"Cache-Control": "no-cache"})


@app.get("/api/studies/{sid}/export/both")
def export_both(sid: str) -> FileResponse:
    import zipfile

    study = _ready(sid)
    pdf, png = _chart_or_422(study, "pdf"), _chart_or_422(study, "png")
    fd, tmp = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    tmp_path = Path(tmp)
    fd2, xl = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd2)
    try:
        export.build_observation_xlsx(study, Path(xl))
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(_export_name(study, "-work-chart.pdf"), pdf)
            zf.writestr(_export_name(study, "-work-chart.png"), png)
            zf.writestr(_export_name(study, "-time-study.pdf"), observation.render_study_pdf(study))
            zf.write(xl, _export_name(study, "-time-study.xlsx"))
    except export.ExportError as e:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(422, str(e))
    finally:
        Path(xl).unlink(missing_ok=True)
    return FileResponse(tmp_path, media_type="application/zip", filename=_export_name(study, "-work-chart.zip"),
                        background=BackgroundTask(tmp_path.unlink, missing_ok=True))


# ---------------------------------------------------------------- copilot

class AskIn(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


@app.post("/api/studies/{sid}/copilot")
def ask_copilot(sid: str, body: AskIn) -> dict:
    study = _ready(sid)
    history = study.get("copilot") or []
    try:
        answer = copilot.chat(study, history, body.message.strip())
    except EngineError as e:
        raise HTTPException(502, str(e))
    now = time.time()
    msgs = [{"role": "user", "text": body.message.strip(), "t": now},
            {"role": "model", "text": answer, "t": now}]
    study = store.mutate(sid, lambda st: st.setdefault("copilot", []).extend(msgs))
    return {"messages": study["copilot"]}


@app.delete("/api/studies/{sid}/copilot", status_code=204)
def clear_copilot(sid: str) -> Response:
    _load_or_404(sid)
    store.update(sid, copilot=[])
    return Response(status_code=204)


@app.post("/api/studies/{sid}/kaizen")
def find_kaizen(sid: str) -> dict:
    study = _ready(sid)
    try:
        ideas = copilot.kaizen_ideas(study)
    except EngineError as e:
        raise HTTPException(502, str(e))
    store.update(sid, kaizen_ideas=ideas)
    return {"ideas": ideas}


class ActionIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=300)
    detail: str = ""
    waste: str = ""
    steps: list[int] = []
    saving_s: float = 0.0
    effort: str = "medium"
    owner: str = ""
    due: str = ""
    status: Literal["open", "in_progress", "done"] = "open"


@app.put("/api/studies/{sid}/actions")
def save_actions(sid: str, actions: list[ActionIn]) -> dict:
    _load_or_404(sid)
    study = store.update(sid, actions=[a.model_dump() for a in actions])
    return {"actions": study["actions"]}


# ---------------------------------------------------------------- media

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


@app.get("/api/studies/{sid}/video")
def stream_video(sid: str, request: Request):
    _load_or_404(sid)
    path = store.study_dir(sid) / "proxy.mp4"
    if not path.exists():
        raise HTTPException(404, "Video is not ready yet.")
    size = path.stat().st_size
    rng = request.headers.get("range")
    if not rng:
        return FileResponse(path, media_type="video/mp4", headers={"Accept-Ranges": "bytes"})
    m = _RANGE_RE.fullmatch(rng.strip())
    if not m or m.groups() == ("", ""):
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    a, b = m.groups()
    if a == "":
        start, end = max(0, size - int(b)), size - 1
    else:
        start, end = int(a), min(int(b) if b else size - 1, size - 1)
    if start > end or start >= size:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    def body():
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = fh.read(min(1 << 20, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(body(), status_code=206, media_type="video/mp4", headers={
        "Content-Range": f"bytes {start}-{end}/{size}", "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1)})


# ---------------------------------------------------------------- exports

def _export_name(study: dict, suffix: str) -> str:
    base = "-".join(x for x in (study["header"].get("job"), study["header"].get("line")) if x) or "work-study"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base)[:60] + suffix


def _ready(sid: str) -> dict:
    study = _load_or_404(sid)
    if study["status"] != "ready":
        raise HTTPException(409, "The study is not ready yet.")
    return study


@app.get("/api/studies/{sid}/export/instructions")
def export_instructions(sid: str, cycle: int | None = None) -> FileResponse:
    study = _ready(sid)
    fd, tmp = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        export.build_instructions_zip(study, store.study_dir(sid) / "proxy.mp4", tmp_path, cycle)
    except (export.ExportError, media.MediaError) as e:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(422, str(e))
    return FileResponse(tmp_path, media_type="application/zip",
                        filename=_export_name(study, "-instructions.zip"),
                        background=BackgroundTask(tmp_path.unlink, missing_ok=True))


@app.get("/api/studies/{sid}/export/time-study")
def export_time_study(sid: str) -> FileResponse:
    study = _ready(sid)
    fd, tmp = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        export.build_time_study_xlsx(study, tmp_path)
    except export.ExportError as e:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(422, str(e))
    return FileResponse(
        tmp_path, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=_export_name(study, "-time-study.xlsx"),
        background=BackgroundTask(tmp_path.unlink, missing_ok=True))
