"""Station templates: the engineer's fixed work elements and station layout, per Line/Operation.

A template holds
  elements: [{id, name, breakpoint, location_id, category, hand, walk, breakpoint_visible, issue}]
            in the engineer's order and wording; studies only time them
  layout:   [{id, label, type, column, row, shelf, order, x?, y?}]; x/y are grid positions set by
            dragging in the editor (slot units: one slot per place, rows back=0, middle=1, front=2)
  manual:   element times read from an imported paper sheet, for comparison
The work chart is drawn from the template only.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from typing import Any

from . import store
from .engine import (COLUMNS, LOCATION_TYPES, ROWS, SHELVES, EngineLocation, TimingResult,
                     normalize_layout, resolve_location)

ROW_Y = {"back": 0, "middle": 1, "front": 2}


def station_key(header: dict[str, str]) -> str:
    """A station is its Line/Operation."""
    return re.sub(r"[^a-z0-9]+", "-", (header.get("line") or "").lower()).strip("-")[:80]


KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def valid_key(key: str) -> bool:
    return bool(key) and len(key) <= 80 and bool(KEY_RE.match(key))


def _path(key: str):
    if not valid_key(key):
        raise ValueError(f"Invalid station key: {key!r}")
    return store.stations_dir() / f"{key}.json"


def load(key: str) -> dict[str, Any] | None:
    if not valid_key(key):
        return None
    try:
        with open(_path(key), encoding="utf-8") as fh:
            t = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    t.setdefault("key", key)
    t.setdefault("elements", [])
    t.setdefault("layout", [])
    t.setdefault("status", "draft")
    return t


def save(key: str, tpl: dict[str, Any]) -> dict[str, Any]:
    if not key:
        raise ValueError("A station needs a Line/Operation.")
    tpl = {**tpl, "key": key, "updated_at": time.time()}
    path = _path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(tpl, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return tpl


def delete(key: str) -> None:
    if valid_key(key):
        _path(key).unlink(missing_ok=True)


def _ranges(nums: list[int]) -> str:
    """[1,2,3,5,7,8] -> '1-3, 5, 7-8'."""
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(f"{nums[i]}-{nums[j]}" if j > i else str(nums[i]))
        i = j + 1
    return ", ".join(out)


def issues(tpl: dict[str, Any]) -> list[str]:
    """What the editor should check, one line per kind (element numbers grouped)."""
    ids = {loc["id"] for loc in tpl.get("layout") or []}
    groups: dict[str, list[int]] = {}
    out = []
    for i, e in enumerate(tpl.get("elements") or [], 1):
        if not (e.get("breakpoint") or "").strip():
            groups.setdefault("nobp", []).append(i)
        elif e.get("breakpoint_suggested"):
            groups.setdefault("draftbp", []).append(i)
        elif e.get("breakpoint_visible") is False:
            out.append(f"Element {i} '{e['name']}': break point may not be visible ({e.get('issue') or 'check it'}).")
        if e.get("location_id") and e["location_id"] not in ids:
            out.append(f"Element {i} '{e['name']}' uses location {e['location_id']}, which is not in the layout.")
        elif not e.get("location_id"):
            groups.setdefault("noloc", []).append(i)
        elif e.get("location_suggested"):
            groups.setdefault("sugloc", []).append(i)
    text = {"nobp": "No break point: element(s) {}. Write one, or use Draft break points.",
            "draftbp": "Break points drafted by the app from the element wording: element(s) {}. Check the wording.",
            "noloc": "No place on the chart: element(s) {}.",
            "sugloc": "Places guessed from the video, not yet confirmed: element(s) {}. Check them on the chart, then Confirm places."}
    return [text[k].format(_ranges(v)) for k, v in groups.items()] + out


def draft_breakpoint(name: str) -> str:
    """A start-of-element break point from the element wording, for the engineer to check."""
    n = name.lower()
    if "tweezer" in n and re.search(r"\b(pick|grab|get|take)", n):
        return "hand reaches toward the tweezer"
    if re.search(r"\bdown\b", n):
        return "product starts moving down toward the bench"
    if re.search(r"\bpush", n):
        return "finger touches the part on the product"
    if re.search(r"\b(insert|fit|fix|twist|screw|press)", n):
        return "part touches the product"
    if re.search(r"\b(pick|grab|get|take)", n):
        obj = "drill" if "drill" in n else "part"
        return f"hand reaches toward the {obj}"
    if re.search(r"\b(place|put|position|set)", n):
        return "part starts moving toward the product"
    return "first visible movement of this element"


def draft_breakpoints(tpl: dict[str, Any]) -> list[int]:
    """Fill every missing break point with a draft (flagged). Returns the element numbers."""
    done = []
    for i, e in enumerate(tpl.get("elements") or [], 1):
        if not (e.get("breakpoint") or "").strip():
            e["breakpoint"] = draft_breakpoint(e["name"])
            e["breakpoint_suggested"] = True
            e["breakpoint_visible"] = True
            e["issue"] = ""
            done.append(i)
    return done


def confirm_places(tpl: dict[str, Any]) -> list[int]:
    done = []
    for i, e in enumerate(tpl.get("elements") or [], 1):
        if e.get("location_suggested") and e.get("location_id"):
            e["location_suggested"] = False
            done.append(i)
    return done


# ---------------------------------------------------------------- building templates

def from_study(study: dict[str, Any], header: dict[str, str], source: str = "video") -> dict[str, Any]:
    """Draft template from the first analyzed study: its instruction-cycle elements and layout."""
    rep = study["representative_cycle"]
    seen, elements = set(), []
    for s in sorted((s for s in study["steps"] if s["cycle"] == rep), key=lambda s: s["start"]):
        if s["element_id"] in seen:
            continue
        seen.add(s["element_id"])
        elements.append({"id": s["element_id"], "name": s["title"], "breakpoint": s.get("breakpoint", ""),
                         "location_id": s.get("location_id", ""), "category": s["category"],
                         "hand": s.get("hand", "both"), "walk": bool(s.get("walk")),
                         "breakpoint_visible": True, "issue": ""})
    return {"line": header.get("line", ""), "status": "draft", "source": source,
            "elements": elements, "layout": study.get("layout") or []}


def elements_from_sheet(sheet, layout: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, list]]:
    """Template elements from an imported paper sheet; locations guessed from names, to review."""
    elements, manual = [], {}
    for i, e in enumerate(sheet.elements, 1):
        eid = f"E{i}"
        cat = e.category if e.category in ("value_add", "necessary_nva", "waste") else "necessary_nva"
        elements.append({"id": eid, "name": e.name.strip(), "breakpoint": e.breakpoint.strip(),
                         "location_id": resolve_location("", e.name, layout) if layout else "",
                         "category": cat, "hand": e.hand if e.hand in ("L", "R", "both") else "both",
                         "walk": False, "breakpoint_visible": bool(e.breakpoint_visible and e.breakpoint.strip()),
                         "issue": e.issue.strip() or ("" if e.breakpoint.strip() else "no break point on the sheet")})
        manual[eid] = list(e.times[:10])
    return elements, manual


def clean_layout(layout: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize categories and keep editor positions (x, y)."""
    base = normalize_layout([EngineLocation.model_validate(l) for l in layout])
    for b, raw in zip(base, layout):
        for k in ("x", "y"):
            if raw.get(k) is not None:
                b[k] = round(float(raw[k]), 3)
    return base


# ---------------------------------------------------------------- grid positions (editor + chart)

def slot_positions(layout: list[dict[str, Any]]) -> dict[str, Any]:
    """Every place in slot units. Dragged places keep x/y; others follow column/row/order.

    Columns are as wide as their busiest row; empty columns keep a narrow gap. Stacked bins
    (same column, row and order) share a position."""
    widths, cells = {}, {}
    for loc in layout:
        cells.setdefault((loc["column"], loc["row"]), set()).add(loc["order"])
    for c in COLUMNS:
        widths[c] = max((len(v) for (col, _), v in cells.items() if col == c), default=0)
    x0, bands = 0.0, []
    for c in COLUMNS:
        w = widths[c] if widths[c] else 0.35
        bands.append({"column": c, "x0": round(x0, 3), "x1": round(x0 + w, 3)})
        x0 += w
    start = {b["column"]: b["x0"] for b in bands}
    pos = {}
    for loc in layout:
        if loc.get("x") is not None and loc.get("y") is not None:
            pos[loc["id"]] = (float(loc["x"]), float(loc["y"]))
            continue
        same = sorted(cells[(loc["column"], loc["row"])])
        offset = (widths[loc["column"]] - len(same)) / 2
        pos[loc["id"]] = (start[loc["column"]] + offset + same.index(loc["order"]) + 0.5, float(ROW_Y[loc["row"]]))
    width = max([x0] + [p[0] + 0.5 for p in pos.values()])
    return {"pos": pos, "bands": bands, "width": round(width, 3), "rows": 3}


def categories_from_position(x: float, y: float, bands: list[dict[str, Any]]) -> tuple[str, str]:
    row = ROWS[min(2, max(0, int(round(y))))]
    col = next((b["column"] for b in bands if b["x0"] <= x < b["x1"]), bands[-1]["column"] if x >= bands[-1]["x1"] else bands[0]["column"])
    return col, row


# ---------------------------------------------------------------- template-mode normalization

def normalize_timing(res: TimingResult, tpl: dict[str, Any], duration: float) -> dict[str, Any]:
    """Readings -> steps: every template element in every cycle, in template order.

    A missing or out-of-order reading leaves that element blank in that cycle; the element
    before it then also covers the gap and is marked uncertain."""
    els = tpl["elements"]
    n = len(els)
    warnings: list[str] = []
    by_cycle = {r.cycle: r for r in res.readings}
    events_by_cycle: dict[int, list[dict[str, Any]]] = {}
    valid_ids = {e["id"] for e in els}
    for ev in res.events:
        if ev.text.strip():
            events_by_cycle.setdefault(ev.cycle, []).append(
                {"time": ev.time, "element_id": ev.element_id if ev.element_id in valid_ids else "", "text": ev.text.strip()})

    def clamp(t):
        return max(0.0, min(float(t), duration))

    cycles, steps, k = [], [], 0
    for c in sorted(res.cycles, key=lambda c: c.start):
        r = by_cycle.get(c.index)
        times = list(r.times) if r else []
        if len(times) != n:
            warnings.append(f"Cycle {c.index}: {len(times)} readings for {n} elements; missing ones left blank.")
            times = (times + [None] * n)[:n]
        uncertain = set(r.uncertain) if r else set()
        end = clamp(c.end)
        clean, prev = [], -1.0
        for i, t in enumerate(times):
            if t is None or not (clamp(c.start) - 1.0 <= float(t) <= end + 0.5) or float(t) < prev:
                if t is not None:
                    warnings.append(f"Cycle {c.index}: reading for '{els[i]['name']}' at {t}s is out of order or "
                                    f"outside the cycle; left blank.")
                clean.append(None)
                continue
            t = clamp(t)
            clean.append(t)
            prev = t
        first = next((t for t in clean if t is not None), None)
        if first is None:
            warnings.append(f"Cycle {c.index}: no usable readings; dropped.")
            continue
        start = first if clean[0] is not None else clamp(c.start)
        if cycles and start < cycles[-1]["end"] - 0.05:
            start = cycles[-1]["end"]
        idx = len(cycles) + 1
        cycles.append({"index": idx, "_src": c.index, "start": round(start, 2), "end": round(max(end, start + 1), 2),
                       "abnormal": False, "x_suggested": bool(c.x), "note": "",
                       "x_reason": c.x_reason.strip() if c.x else "",
                       "x_time": c.x_time if c.x else None, "events": events_by_cycle.get(c.index, [])})
        missing = [els[i]["name"] for i, t in enumerate(clean) if t is None]
        if missing:
            warnings.append(f"Cycle {idx}: no reading for {', '.join(missing)}.")
        for i, t in enumerate(clean):
            if t is None:
                continue
            j = next((j for j in range(i + 1, n) if clean[j] is not None), None)
            stop = clean[j] if j is not None else cycles[-1]["end"]
            gap = j is not None and j > i + 1 or (j is None and i < n - 1)
            e = els[i]
            k += 1
            steps.append({
                "id": f"s{k}", "element_id": e["id"], "cycle": idx, "start": round(t, 2),
                "end": round(max(stop, t), 2), "title": e["name"], "breakpoint": e.get("breakpoint", ""),
                "description": e.get("description", ""), "category": e.get("category", "necessary_nva"), "hand": e.get("hand", "both"),
                "tools": [], "materials": [], "key_points": [], "transcript": [],
                "location_id": e.get("location_id", ""), "walk": bool(e.get("walk")),
                "confidence": "low" if gap or e["id"] in uncertain else "high",
                "covers_gap": bool(gap), "edited": False})
    if not cycles:
        from .engine import EngineError
        raise EngineError("No complete cycles were found with the station's elements.")
    src = {c["_src"]: c["index"] for c in cycles}
    rep = src.get(res.representative_cycle) or next((c["index"] for c in cycles if not c["abnormal"]), 1)
    for c in cycles:
        del c["_src"]
    return {"cycles": cycles, "steps": steps, "representative_cycle": rep, "warnings": warnings,
            "layout": tpl.get("layout") or []}


def ref_dir(key: str):
    if not valid_key(key):
        raise ValueError(f"Invalid station key: {key!r}")
    return store.stations_dir() / f"{key}-ref"


MAX_REFS = 3


def references(tpl: dict[str, Any]) -> list[dict[str, Any]]:
    """Marked reference cycles (older templates kept a single 'reference')."""
    refs = list(tpl.get("references") or [])
    old = tpl.get("reference")
    if old and old.get("marks") and not refs:
        refs = [{**old, "rid": "r1", "legacy": True}]
    return refs


def ref_image(key: str, ref: dict[str, Any], eid: str):
    sub = ref_dir(key) if ref.get("legacy") else ref_dir(key) / ref["rid"]
    return sub / f"{eid}.jpg"


def reference_for_engine(key: str, tpl: dict[str, Any]) -> dict[str, Any] | None:
    """Every marked cycle: break points as offsets from its cycle start, plus the frame at each."""
    out = []
    for ref in references(tpl):
        marks = {k: v for k, v in (ref.get("marks") or {}).items() if v is not None}
        if not marks:
            continue
        t0 = ref.get("cycle_start", min(marks.values()))
        out.append({"rid": ref["rid"], "study_id": ref.get("study_id"), "cycle_start": t0, "marks": marks,
                    "rel": {eid: round(t - t0, 2) for eid, t in marks.items()},
                    "images": {eid: str(ref_image(key, ref, eid)) for eid in marks if ref_image(key, ref, eid).exists()}})
    return {"refs": out} if out else None


def use_reference_marks(res, reference: dict[str, Any] | None, sid: str, elements: list[dict[str, Any]]):
    """Cycles marked in this study use the marks. Gemini's own readings for them (timed without that
    cycle's pictures, leave-one-out) measure its accuracy. Returns the report or None."""
    if not reference:
        return None
    ids = [e["id"] for e in elements]
    rows, errs, cycles = [], [], []
    from .engine import CycleReadings
    for ref in reference["refs"]:
        if ref.get("study_id") != sid:
            continue
        near = sorted(res.cycles, key=lambda c: abs(c.start - ref["cycle_start"]))
        cyc = near[0] if near and abs(near[0].start - ref["cycle_start"]) <= 5 else None
        if cyc is None:
            continue
        r = next((r for r in res.readings if r.cycle == cyc.index), None)
        gem = list(r.times) if r else [None] * len(ids)
        for eid, g in zip(ids, gem):
            m = ref["marks"].get(eid)
            err = round(g - m, 2) if g is not None and m is not None else None
            if err is not None:
                errs.append(abs(err))
            rows.append({"cycle_src": cyc.index, "element_id": eid, "marked": m, "gemini": g, "error": err})
        new_times = [ref["marks"].get(eid) for eid in ids]
        if r:
            r.times = new_times
        else:
            res.readings.append(CycleReadings(cycle=cyc.index, times=new_times))
        cycles.append({"cycle_src": cyc.index, "cycle_start": ref["cycle_start"]})
    if not cycles:
        return None
    return {"cycles": cycles, "rows": rows, "compared": len(errs),
            "mae": round(sum(errs) / len(errs), 2) if errs else None,
            "within_0_5": sum(e <= 0.5 for e in errs), "within_1": sum(e <= 1.0 for e in errs),
            "leave_one_out": True}


def apply_location_suggestions(tpl: dict[str, Any], suggestions: dict[str, str]) -> bool:
    """Fill places Gemini suggested for elements that have none; flagged for the engineer to check."""
    ids = {l["id"] for l in tpl.get("layout") or []}
    changed = False
    for e in tpl.get("elements") or []:
        sug = (suggestions or {}).get(e["id"], "")
        if not e.get("location_id") and sug in ids:
            e["location_id"] = sug
            e["location_suggested"] = True
            changed = True
    return changed


def element_lookup(study: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Template elements by id for a study's station (names/locations edited later show up)."""
    tpl = load(study.get("station_key") or station_key(study.get("header", {})))
    return {e["id"]: e for e in (tpl or {}).get("elements", [])} if tpl and study.get("template_used") else {}


__all__ = ["station_key", "load", "save", "delete", "issues", "from_study", "elements_from_sheet",
           "clean_layout", "slot_positions", "categories_from_position", "normalize_timing",
           "element_lookup", "LOCATION_TYPES", "SHELVES"]
