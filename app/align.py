"""Free, local timing by alignment: every cycle is matched frame by frame to the engineer's marked
reference cycles, and their break points are carried over.

Each frame becomes a DINOv2 feature vector (small ONNX model, CPU). A cycle and a marked cycle are
aligned with dynamic time warping that may skip frames of the cycle being timed (rework, an
interruption, the camera looking away) but must pass through every frame of the marked cycle.
Where a marked break point lands in the cycle is that cycle's reading. With several marked cycles
each gives a reading; the median is used and their spread says how sure it is.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import urllib.request
from pathlib import Path
from statistics import median
from typing import Any, Callable

import numpy as np

from . import store
from .media import FFMPEG

log = logging.getLogger("workstudy.align")

# frames per second read for alignment; a CPU without a GPU manages about 2-3 frames a second
FPS = int(os.environ.get("WORKSTUDY_ALIGN_FPS", "2"))
W, H = 224, 224  # the ONNX export has fixed 16 x 16 position embeddings; the 16:9 frame is squashed
MODEL_PATH = store.DATA_DIR / "models" / "dinov2-small.onnx"
MODEL_URL = ("https://huggingface.co/Xenova/dinov2-small/resolve/"
             "c2bb04a51fab207c420665f1946016107bffc701/onnx/model.onnx")
FEATURES_FILE = f"features-dinov2s-cls-{FPS}fps.npy"
CONFIDENT_SPREAD = float(os.environ.get("WORKSTUDY_ALIGN_SPREAD", "0.6"))  # s between marked cycles' readings
PAD = 4.0  # s searched around each cycle

_model_lock = threading.Lock()
_session = None


class AlignError(RuntimeError):
    pass


def available() -> bool:
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def _model():
    global _session
    with _model_lock:
        if _session is None:
            import onnxruntime as ort
            if not MODEL_PATH.exists():
                MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
                tmp = MODEL_PATH.with_suffix(".part")
                log.info("downloading %s", MODEL_URL)
                urllib.request.urlretrieve(MODEL_URL, tmp)
                tmp.replace(MODEL_PATH)
            so = ort.SessionOptions()
            so.intra_op_num_threads = os.cpu_count() or 4
            _session = ort.InferenceSession(str(MODEL_PATH), so, providers=["CPUExecutionProvider"])
        return _session


_MEAN = np.array([0.485, 0.456, 0.406], np.float32)[:, None, None]
_STD = np.array([0.229, 0.224, 0.225], np.float32)[:, None, None]


def _embed(frames: list[np.ndarray]) -> np.ndarray:
    x = (np.stack(frames).astype(np.float32).transpose(0, 3, 1, 2) / 255 - _MEAN) / _STD
    # the image-level (CLS) token: what is happening, more than where it sits in the picture. On handheld
    # video, per-region features followed the camera angle and aligned much worse.
    return _model().run(None, {"pixel_values": x})[0][:, 0].astype(np.float16)


def features(video: Path, progress: Callable[[str], None] | None = None, duration: float | None = None) -> np.ndarray:
    """Frame features at FPS, cached next to the video: (frames, 384) float16."""
    cache = video.parent / FEATURES_FILE
    if cache.exists() and cache.stat().st_mtime >= video.stat().st_mtime:
        return np.load(cache)
    proc = subprocess.Popen([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(video), "-vf",
                             f"fps={FPS},scale={W}:{H}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                            stdout=subprocess.PIPE)
    out, batch, size = [], [], W * H * 3
    total = int((duration or 0) * FPS)
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            batch.append(np.frombuffer(buf, np.uint8).reshape(H, W, 3))
            if len(batch) == 8:
                out.append(_embed(batch))
                batch = []
                if progress and total:
                    done = sum(len(o) for o in out)
                    if done % 64 == 0:
                        progress(f"Reading frames for alignment ({min(99, 100 * done // total)}%)")
        if batch:
            out.append(_embed(batch))
    finally:
        proc.stdout.close()
        proc.wait()
    if not out:
        raise AlignError("No frames could be read from the video.")
    feat = np.concatenate(out)
    tmp = cache.with_name(cache.stem + ".part.npy")
    np.save(tmp, feat)
    tmp.replace(cache)
    return feat


def prepare(feat: np.ndarray) -> np.ndarray:
    """Per video: remove what every frame shares (bench, background, lighting), so the hands, parts and
    tools dominate."""
    x = feat.astype(np.float32)
    x = x - x.mean(0, keepdims=True)
    k = np.array([0.25, 0.5, 0.25], np.float32)  # light smoothing over +-1 frame
    x = np.apply_along_axis(lambda c: np.convolve(c, k, mode="same"), 0, x) if len(x) > 2 else x
    return x / (np.linalg.norm(x, axis=1, keepdims=True) + 1e-6)


def dtw(cost: np.ndarray, drop: float, pen: float, start_max: int | None = None, end_min: int = 0) -> np.ndarray:
    """cost: (target frames, reference frames). The match may start anywhere in the target's first
    `start_max` frames and end anywhere after `end_min`; target frames may be dropped at `drop`
    each; every reference frame is matched. Returns, per reference frame, the first target frame
    matched to it."""
    n, m = cost.shape
    c = cost.astype(np.float64)
    D = np.full((n, m), np.inf)
    for i in range(n):
        a = np.full(m, np.inf)
        a[0] = c[i, 0] if start_max is None or i <= start_max else np.inf
        if i:
            prev = D[i - 1]
            a[1:] = prev[:-1] + c[i, 1:]
            a = np.minimum(a, prev + np.minimum(c[i], drop))
        s = np.cumsum(c[i] + pen)
        D[i] = s + np.minimum.accumulate(a - s)
    # back-track from the cheapest end; walking backwards, the last frame written for j is the earliest
    end_min = min(max(0, end_min), n - 1)
    i, j = end_min + int(np.argmin(D[end_min:, m - 1])), m - 1
    first = np.full(m, -1)
    while True:
        d, ci = D[i, j], c[i, j]
        if i > 0 and np.isclose(d, D[i - 1, j] + drop) and not np.isclose(d, D[i - 1, j] + ci):
            i -= 1  # target frame dropped: not matched to j
            continue
        first[j] = i
        if i > 0 and j > 0 and np.isclose(d, D[i - 1, j - 1] + ci):
            i, j = i - 1, j - 1
        elif j > 0 and np.isclose(d, D[i, j - 1] + ci + pen):
            j -= 1
        elif i > 0 and np.isclose(d, D[i - 1, j] + ci):
            i -= 1
        else:
            break  # start (j == 0)
    return first


def _frames(t0: float, t1: float, n: int) -> tuple[int, int]:
    a = max(0, int(round(t0 * FPS)))
    b = min(n, int(round(t1 * FPS)) + 1)
    return a, b


def transfer_one(target: np.ndarray, t_start: float, t_end: float, ref_feat: np.ndarray, ref: dict[str, Any],
                 ref_end: float, ids: list[str], drop_q: float = 0.4, pen: float = 0.05) -> dict[str, float]:
    """Readings for one cycle from one marked cycle (absolute seconds in the target video)."""
    ra, rb = _frames(ref["cycle_start"], ref_end, len(ref_feat))
    ta, tb = _frames(t_start - PAD, t_end + PAD, len(target))
    R, T = ref_feat[ra:rb], target[ta:tb]
    if len(R) < 5 or len(T) < 5:
        return {}
    cost = 1.0 - T @ R.T
    drop = float(np.quantile(cost, drop_q))
    slack = int(2 * PAD * FPS)  # the cycle's own start and end are known to a few seconds
    first = dtw(cost, drop, pen, start_max=slack, end_min=len(T) - slack)
    out = {}
    for eid in ids:
        t = ref["marks"].get(eid)
        if t is None:
            continue
        j = min(len(R) - 1, max(0, int(round(t * FPS)) - ra))
        out[eid] = round((ta + int(first[j])) / FPS, 2)
    return out


def time_cycles(video_feat: np.ndarray, cycles: list[dict[str, Any]], refs: list[dict[str, Any]],
                ref_feats: dict[str, np.ndarray], ref_ends: dict[str, float], ids: list[str],
                exclude: Callable[[dict[str, Any], dict[str, Any]], bool] | None = None) -> dict[int, dict[str, Any]]:
    """Per cycle index: {"times": [...per element or None], "spread": [...], "n": [...]}."""
    out = {}
    for c in cycles:
        per = []
        for r in refs:
            if exclude and exclude(c, r):
                continue
            per.append(transfer_one(video_feat, c["start"], c["end"], ref_feats[r["rid"]], r, ref_ends[r["rid"]], ids))
        times, spread, count = [], [], []
        prev = -1.0
        for eid in ids:
            vals = [p[eid] for p in per if eid in p]
            if not vals:
                times.append(None); spread.append(None); count.append(0)
                continue
            t = max(median(vals), prev)
            prev = t
            times.append(round(t, 2)); spread.append(round(max(vals) - min(vals), 2)); count.append(len(vals))
        out[c["index"]] = {"times": times, "spread": spread, "n": count}
    return out


def ref_end(study: dict[str, Any], ref: dict[str, Any]) -> float:
    """End of the marked cycle in its own study (the next cycle's start), else last mark + 10 s."""
    cyc = min(study.get("cycles") or [], key=lambda c: abs(c["start"] - ref["cycle_start"]), default=None)
    last = max(ref["marks"].values())
    if cyc and abs(cyc["start"] - ref["cycle_start"]) <= 5 and cyc["end"] > last:
        return cyc["end"]
    return last + 10.0


# ---------------------------------------------------------------- station level

_prepared: dict[tuple[str, float], np.ndarray] = {}
_study_locks: dict[str, threading.Lock] = {}


def study_features(sid: str, progress: Callable[[str], None] | None = None) -> np.ndarray:
    video = store.study_dir(sid) / "proxy.mp4"
    if not video.exists():
        raise AlignError(f"Study {sid} has no video.")
    with _model_lock:
        lock = _study_locks.setdefault(sid, threading.Lock())
    with lock:  # a read already running (prefetch) is waited for, not repeated
        k = (sid, video.stat().st_mtime)
        if k not in _prepared:
            duration = None
            try:
                duration = store.load(sid).get("duration")
            except Exception:
                pass
            _prepared[k] = prepare(features(video, progress, duration))
        return _prepared[k]


def prefetch(sid: str) -> None:
    """Start reading a study's frames in the background (while Gemini works)."""
    def go():
        try:
            study_features(sid)
        except Exception as e:
            log.warning("alignment prefetch failed for %s: %s", sid, e)
    threading.Thread(target=store.bind(go), daemon=True).start()


def _same_cycle(sid: str, c: dict[str, Any], r: dict[str, Any]) -> bool:
    return r.get("study_id") == sid and abs(c["start"] - r["cycle_start"]) <= 5


def run(sid: str, cycles: list[dict[str, Any]], refs: list[dict[str, Any]], ids: list[str],
        progress: Callable[[str], None] | None = None) -> dict[int, dict[str, Any]]:
    """Time every cycle of a study from the marked cycles. A marked cycle of this study is timed only
    from the other marked cycles (blind), so its marks can measure the alignment."""
    refs = [r for r in refs if r.get("marks") and r.get("study_id")]
    if not refs:
        return {}
    feat = study_features(sid, progress)
    ref_feats, ref_ends = {}, {}
    for r in refs:
        try:
            ref_feats[r["rid"]] = feat if r["study_id"] == sid else study_features(r["study_id"], progress)
            ref_ends[r["rid"]] = ref_end(store.load(r["study_id"]), r)
        except Exception as e:  # a reference whose study was deleted is skipped
            log.warning("reference %s skipped: %s", r.get("rid"), e)
    refs = [r for r in refs if r["rid"] in ref_feats]
    if progress:
        progress("Aligning cycles to your marked cycles")
    return time_cycles(feat, cycles, refs, ref_feats, ref_ends, ids, exclude=lambda c, r: _same_cycle(sid, c, r))


def merge(readings: dict[int, list[float | None]], aligned: dict[int, dict[str, Any]], ids: list[str],
          choice: dict[str, str], trusted: set[str] = frozenset()) -> tuple[dict[int, list[float | None]], dict[int, list[str]]]:
    """Per cycle and element: the method `choice` names for that element ("align" or "gemini");
    a missing reading falls back to the other. Uncertain (only for `trusted` elements, where
    alignment proved accurate on your marks): the two disagree by more than 1 s, or the marked
    cycles disagree by more than CONFIDENT_SPREAD."""
    out, unsure = {}, {}
    for ci, gem in readings.items():
        al = aligned.get(ci)
        if not al:
            out[ci] = list(gem)
            continue
        times, flags, prev = [], [], -1.0
        for k, eid in enumerate(ids):
            g, a, sp = gem[k] if k < len(gem) else None, al["times"][k], al["spread"][k]
            t = (a if a is not None else g) if choice.get(eid) == "align" else (g if g is not None else a)
            if t is not None and t < prev:  # keep element order
                t = prev
            if t is not None:
                prev = t
            times.append(t)
            if eid in trusted and ((a is not None and g is not None and abs(a - g) > 1.0)
                                   or (sp is not None and sp > CONFIDENT_SPREAD)):
                flags.append(eid)
        out[ci], unsure[ci] = times, flags
    return out, unsure


ALIGN_MAX_ERR = float(os.environ.get("WORKSTUDY_ALIGN_MAX_ERR", "1.0"))


def choose(rows: list[dict[str, Any]], ids: list[str]) -> dict[str, str]:
    """Per element, which reading to use, from the blind errors on the marked cycles:
    both measured -> the smaller error; only alignment measured -> alignment if its mean error is at
    most ALIGN_MAX_ERR; nothing measured -> Gemini (alignment then only flags disagreements).
    WORKSTUDY_PREFER=gemini|align forces one."""
    force = os.environ.get("WORKSTUDY_PREFER", "auto")
    if force in ("gemini", "align"):
        return {e: force for e in ids}
    out = {}
    for eid in ids:
        mine = [r for r in rows if r["element_id"] == eid and r.get("marked") is not None]
        aa = [abs(r["aligned"] - r["marked"]) for r in mine if r.get("aligned") is not None]
        both = [r for r in mine if r.get("aligned") is not None and r.get("gemini") is not None]
        if both:
            ga = sum(abs(r["gemini"] - r["marked"]) for r in both)
            out[eid] = "align" if sum(abs(r["aligned"] - r["marked"]) for r in both) < ga else "gemini"
        elif aa:
            out[eid] = "align" if sum(aa) / len(aa) <= ALIGN_MAX_ERR else "gemini"
        else:
            out[eid] = "gemini"
    return out


def trusted(rows: list[dict[str, Any]], ids: list[str]) -> set[str]:
    """Elements whose alignment readings were within ALIGN_MAX_ERR of your marks on average."""
    out = set()
    for eid in ids:
        aa = [abs(r["aligned"] - r["marked"]) for r in rows
              if r["element_id"] == eid and r.get("aligned") is not None and r.get("marked") is not None]
        if aa and sum(aa) / len(aa) <= ALIGN_MAX_ERR:
            out.add(eid)
    return out


def _stats(errs: list[float]) -> dict[str, Any]:
    return {"compared": len(errs), "mae": round(sum(errs) / len(errs), 2) if errs else None,
            "within_0_5": sum(e <= 0.5 for e in errs), "within_1": sum(e <= 1.0 for e in errs)}


def report(rows: list[dict[str, Any]], choice: dict[str, str]) -> dict[str, Any]:
    """Accuracy of Gemini, alignment and the combination on the marked cycles (all blind)."""
    def errs(key):
        return [abs(r[key] - r["marked"]) for r in rows if r.get(key) is not None and r.get("marked") is not None]
    for r in rows:
        r["used"] = r.get("aligned") if choice.get(r["element_id"]) == "align" else r.get("gemini")
        r["error"] = round(r["used"] - r["marked"], 2) if r["used"] is not None and r.get("marked") is not None else None
        for key in ("gemini", "aligned"):
            r[f"error_{key}"] = round(r[key] - r["marked"], 2) if r.get(key) is not None and r.get("marked") is not None else None
    return {**_stats(errs("used")), "gemini": _stats(errs("gemini")), "aligned": _stats(errs("aligned")),
            "choice": choice}
