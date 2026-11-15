"""Teach the app: the engineer's plain-language feedback -> proposed changes they approve.

"The bins on the left are closer together", "element 5 starts when the screw touches the part",
"make the step descriptions more detailed". The model turns the text into concrete changes to the
station template (layout positions and labels, element fields, descriptions) and standing rules
the station remembers for every later analysis. Nothing changes until the engineer ticks and
applies a change; every change is checked against the template before it is shown or applied.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from . import template
from .engine import EngineError

log = logging.getLogger("workstudy.feedback")

SCOPES = {"timing": "Timing (break points, cycles, X)", "chart": "Work chart and layout",
          "descriptions": "Step descriptions", "sheets": "Sheets and comments", "general": "General"}
ELEMENT_FIELDS = {"name", "breakpoint", "location_id", "category", "hand", "walk", "quality", "safety",
                  "description", "note"}
LOCATION_FIELDS = {"label", "type", "shelf", "column", "row"}
CHOICES = {"category": ["value_add", "necessary_nva", "waste"], "hand": ["L", "R", "both"],
           "type": ["bin", "bench", "fixture", "equipment", "tool", "output"],
           "column": ["far_left", "left", "center", "right", "far_right"], "row": ["back", "middle", "front"],
           "shelf": ["top", "middle", "bottom", None]}
MAX_RULES = 40


class _Change(BaseModel):
    kind: str
    target: str = ""
    field: str = ""
    value: Any = None
    x: float | None = None
    y: float | None = None
    ids: list[str] = Field(default_factory=list)
    factor: float | None = None
    why: str = ""


class _Rule(BaseModel):
    scope: str = "general"
    text: str


class _Proposal(BaseModel):
    reply: str = ""
    changes: list[_Change] = Field(default_factory=list)
    rules: list[_Rule] = Field(default_factory=list)
    write_descriptions: bool = False
    description_style: str = ""


class _Descriptions(BaseModel):
    descriptions: dict[str, str]


PROMPT = """You help an industrial engineer correct and teach a work-standardization app. The app turned a \
phone video of a manual assembly station into a station template (elements = work steps in the engineer's \
own wording, with break points), a layout of places (bins, fixtures, tools) drawn on a Standardized Work \
Chart, and timed every element in every cycle.

The engineer now gives feedback in plain words. Turn it into concrete, minimal changes to the template and/or \
standing rules for future analyses. Return JSON only:
{"reply": "If the engineer ASKS something (why, what, how long...), answer it fully and concretely from the data \
below (times per cycle, what was seen in the step), in up to 8 plain sentences, then say what change would fix the \
cause. Otherwise 1-3 sentences: what you understood and will change, or ONE short question if the feedback is too \
vague to act on (then give no changes)",
 "changes": [ ...see kinds below... ],
 "rules": [{"scope": "timing|chart|descriptions|sheets|general", "text": "a standing instruction, in the \
engineer's words where possible"}],
 "write_descriptions": false, "description_style": ""}

Change kinds (use ids exactly as listed below):
- {"kind": "move_location", "target": "<location id>", "x": <number>, "y": <number>, "why": "..."}
  Positions are in slot units: x grows to the operator's right, 1 unit = about one bin width; y is the row: \
0 = back (far from the operator), 1 = middle, 2 = front (closest). Move only what the feedback is about.
- {"kind": "spacing", "ids": ["<location id>", ...], "factor": <0.3-2.0>, "why": "..."} \
  "closer together" = factor below 1 (0.6-0.8 for "slightly"), "further apart" = above 1. Scales the \
distances of these places from their common centre.
- {"kind": "edit_location", "target": "<location id>", "field": "label|type|shelf|column|row", "value": ..., "why": "..."}
- {"kind": "edit_element", "target": "<element id>", "field": "breakpoint|location_id|category|hand|walk|\
quality|safety|description|note|name", "value": ..., "why": "..."}
  category: value_add | necessary_nva | waste; hand: L | R | both; walk/quality/safety: true/false; \
location_id: a location id from the list; note: a hint for timing this element in later videos.
  Change "name" ONLY if the engineer explicitly asks to rename or reword that element: the element \
wording is the engineer's and must otherwise stay exactly as it is.
- {"kind": "add_element", "target": "<element id it comes AFTER, or empty for first>", "value": "<short name in \
the engineer's style, e.g. 'tighten castle nut'>", "field": "<its break point: the visible moment it starts>", "why": "..."}
  Use when time is spent on work that has no element of its own (the last element of a cycle absorbs everything
  until the next cycle starts). Add them in the order they happen. New elements have no times until re-timed.
- Set "write_descriptions": true when the engineer wants the step descriptions written, rewritten, longer, \
shorter or in a different style; put the style they asked for in "description_style".

Rules are remembered for this station and given to the AI in every later analysis of it. Add a rule when \
the feedback is a lasting preference or a fact about the station ("always...", "on this station...", \
"descriptions should..."), not for a one-off fix. Keep each rule one sentence.

Do not invent problems the engineer did not mention. Never change times directly; timing feedback becomes \
an element note, a break point the engineer reworded, or a timing rule (the engineer then re-times).

The engineer is looking at: __TAB__
__CONTEXT__

Engineer's feedback:
\"\"\"__TEXT__\"\"\""""

DESC_PROMPT = """Write the step description for every element of this manual assembly station, for a \
step-wise breakdown and work instructions used to train operators.
Style asked for: __STYLE__
Use the element wording, break point, place and hand given; you may use the transcript lines (what was \
seen in the video in that step). Do not change or repeat the element name; describe how the step is done. \
Plain imperative sentences, no marketing, no invented tools or parts beyond what is given.
__RULES__
Elements:
__ELEMENTS__

Return JSON only: {"descriptions": {"<element id>": "<description>", ...}} with every element id."""


def _ctx(study: dict[str, Any], tpl: dict[str, Any]) -> str:
    grid = template.slot_positions(tpl.get("layout") or [])["pos"]
    locs = "\n".join(f"{l['id']} | {l['label']} | {l['type']} | {l['column']}/{l['row']}"
                     f"{'/' + l['shelf'] if l.get('shelf') else ''} | x={grid[l['id']][0]:.2f} y={grid[l['id']][1]:.2f}"
                     for l in tpl.get("layout") or [])
    labels = {l["id"]: l["label"] for l in tpl.get("layout") or []}
    times: dict[str, list[float]] = {}
    for s in study.get("steps") or []:
        times.setdefault(s["element_id"], []).append(max(0.0, s["end"] - s["start"]))
    rep = study.get("representative_cycle")
    per_cycle: dict[str, dict[int, float]] = {}
    seen: dict[str, list[str]] = {}
    for s in study.get("steps") or []:
        per_cycle.setdefault(s["element_id"], {})[s["cycle"]] = round(max(0.0, s["end"] - s["start"]), 1)
        if s["cycle"] == rep:
            seen.setdefault(s["element_id"], []).extend(
                f"+{max(0.0, ln['t'] - s['start']):.1f}s {ln['text']}" for ln in s.get("transcript") or [])
    els = []
    for n, e in enumerate(tpl.get("elements") or [], 1):
        t = sorted(times.get(e["id"], []))
        med = f"{t[len(t) // 2]:.1f} s" if t else "-"
        pc = per_cycle.get(e["id"], {})
        med += " | per cycle: " + ", ".join(f"c{c} {v}" for c, v in sorted(pc.items())) if pc else ""
        flags = [k for k in ("walk", "quality", "safety") if e.get(k)]
        els.append(f"{e['id']} (#{n}) | {e['name']} | break point: {e.get('breakpoint') or '-'} | at "
                   f"{e.get('location_id') or '-'} ({labels.get(e.get('location_id'), '')}) | {e.get('category')} | "
                   f"hand {e.get('hand')} | {' '.join(flags) or '-'} | median {med}"
                   f"{' | note: ' + e['note'] if e.get('note') else ''}"
                   f"{' | description: ' + e['description'][:120] if e.get('description') else ''}"
                   + (f"\n    seen in cycle {rep}: " + " / ".join(seen[e["id"]])[:700] if seen.get(e["id"]) else ""))
    rules = "\n".join(f"- [{r['scope']}] {r['text']}" for r in tpl.get("rules") or []) or "(none)"
    cyc = "; ".join(f"c{c['index']} {c['start']:.1f}-{c['end']:.1f}s{' (suggested X: ' + c.get('x_reason', '') + ')' if c.get('x_suggested') else ''}"
                    for c in study.get("cycles") or [])
    return (f"Station: {tpl.get('line') or tpl.get('key')}\nCycles: {cyc}\nRepresentative cycle: {rep}\n\nPlaces (id | label | type | column/row/shelf | position):\n"
            f"{locs or '(none)'}\n\nElements (id | wording | break point | place | category | hand | marks | median time):\n"
            + "\n".join(els) + f"\n\nRules already remembered:\n{rules}")


def _gemini():
    from .copilot import _Gemini
    return _Gemini()


def _mock() -> bool:
    from .copilot import _mock as m
    return m()


def _json(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("no JSON object in the answer")
    return json.loads(m.group(0))


def _ask(prompt: str, model_cls):
    last: Exception | None = None
    g = _gemini()
    for _ in range(2):
        try:
            return model_cls.model_validate(_json(g.generate("Return JSON only.", [("user", prompt)], json_mode=True)))
        except (ValueError, ValidationError, json.JSONDecodeError) as e:
            last = e
            log.warning("malformed feedback answer: %s", e)
        except EngineError:
            raise
        except Exception as e:
            log.exception("feedback request failed")
            raise EngineError(f"Feedback request failed: {e}") from e
    raise EngineError(f"The model returned a malformed answer twice: {last}")


def _fmt(v: Any) -> str:
    if v is None or v == "":
        return "(empty)"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def _validate(p: _Proposal, tpl: dict[str, Any], user_text: str) -> dict[str, Any]:
    """Model output -> only changes that fit the template, with readable before/after."""
    els = {e["id"]: (n, e) for n, e in enumerate(tpl.get("elements") or [], 1)}
    locs = {l["id"]: l for l in tpl.get("layout") or []}
    pos = template.slot_positions(tpl.get("layout") or [])["pos"]
    items, notes = [], []

    def add(kind, label, before, after, op, why=""):
        items.append({"id": f"c{len(items) + 1}", "kind": kind, "label": label, "before": before, "after": after,
                      "op": op, "why": why.strip()})

    for c in p.changes:
        if c.kind == "move_location" and c.target in locs and c.x is not None and c.y is not None:
            x, y = round(min(max(c.x, -1.0), 40.0), 2), round(min(max(c.y, -0.5), 2.5), 2)
            bx, by = pos[c.target]
            add("layout", f"Move {locs[c.target]['label']} ({c.target})", f"x {bx:.2f}, y {by:.2f}",
                f"x {x:.2f}, y {y:.2f}", {"type": "pos", "moves": {c.target: [x, y]}}, c.why)
        elif c.kind == "spacing" and c.factor and len([i for i in c.ids if i in locs]) >= 2:
            ids = [i for i in dict.fromkeys(c.ids) if i in locs]
            f = min(max(float(c.factor), 0.3), 2.0)
            cx = sum(pos[i][0] for i in ids) / len(ids)
            cy = sum(pos[i][1] for i in ids) / len(ids)
            moves = {i: [round(cx + (pos[i][0] - cx) * f, 2), round(cy + (pos[i][1] - cy) * f, 2)] for i in ids}
            word = "closer together" if f < 1 else "further apart"
            add("layout", f"{', '.join(locs[i]['label'] for i in ids)}: {word} ({f:.2f}x the spacing)",
                "; ".join(f"{i} x {pos[i][0]:.2f} y {pos[i][1]:.2f}" for i in ids),
                "; ".join(f"{i} x {moves[i][0]:.2f} y {moves[i][1]:.2f}" for i in ids),
                {"type": "pos", "moves": moves}, c.why)
        elif c.kind == "edit_location" and c.target in locs and c.field in LOCATION_FIELDS:
            v = c.value if c.value not in ("", "null", "none") else None
            if c.field in CHOICES and v not in CHOICES[c.field]:
                notes.append(f"Skipped: {c.field} '{v}' is not a valid choice.")
                continue
            if c.field == "label":
                v = str(v or "").strip()[:120]
                if not v:
                    continue
            before = locs[c.target].get(c.field)
            if before == v:
                continue
            add("layout", f"{locs[c.target]['label']} ({c.target}): {c.field}", _fmt(before), _fmt(v),
                {"type": "loc", "id": c.target, "field": c.field, "value": v}, c.why)
        elif c.kind == "edit_element" and c.target in els and c.field in ELEMENT_FIELDS:
            n, e = els[c.target]
            v = c.value
            if c.field in ("walk", "quality", "safety"):
                v = v if isinstance(v, bool) else str(v).strip().lower() in ("true", "yes", "1")
            elif c.field in CHOICES:
                if v not in CHOICES[c.field]:
                    notes.append(f"Skipped: {c.field} '{v}' is not a valid choice.")
                    continue
            elif c.field == "location_id":
                v = str(v or "")
                if v and v not in locs:
                    notes.append(f"Skipped: place '{v}' is not in the layout.")
                    continue
            else:
                v = str(v or "").strip()[:600 if c.field == "description" else 160]
            if c.field == "name" and not re.search(r"renam|reword|call it|wording|name", user_text, re.I):
                notes.append(f"Kept the wording of element {n}: the feedback did not ask to rename it.")
                continue
            before = e.get(c.field)
            if before == v or (before in (None, "") and v in (None, "")):
                continue
            add("element", f"Element {n} '{e['name']}': {c.field.replace('_', ' ')}", _fmt(before), _fmt(v),
                {"type": "el", "id": c.target, "field": c.field, "value": v}, c.why)
    adds = [c for c in p.changes if c.kind == "add_element" and str(c.value or "").strip()]
    for c in adds:
        after = c.target if c.target in els else ""
        name = str(c.value).strip()[:120]
        if any(e["name"].strip().lower() == name.lower() for e in tpl.get("elements") or []) and not after:
            continue
        where = f"after element {els[after][0]} '{els[after][1]['name']}'" if after else "at the start"
        add("element", f"Add element '{name}' {where}", "(not in the template)",
            f"break point: {str(c.field or '').strip()[:160] or 'first visible movement'}",
            {"type": "add", "after": after, "name": name, "breakpoint": str(c.field or "").strip()[:160]}, c.why)
    rules = []
    have = {r["text"].strip().lower() for r in tpl.get("rules") or []}
    for r in p.rules:
        t = r.text.strip()
        if t and t.lower() not in have:
            rules.append({"id": f"r{len(rules) + 1}", "scope": r.scope if r.scope in SCOPES else "general", "text": t[:300]})
    desc = {"style": (p.description_style or "clear, 1-2 sentences per step").strip()[:200]} if p.write_descriptions else None
    return {"reply": p.reply.strip(), "items": items, "rules": rules, "descriptions": desc, "notes": notes}


def propose(study: dict[str, Any], tpl: dict[str, Any], text: str, tab: str) -> dict[str, Any]:
    if _mock():
        p = _Proposal(reply="Mock mode (WORKSTUDY_ENGINE=mock): your feedback is offered as a rule. "
                            "Set the engine to gemini to get concrete changes.",
                      rules=[_Rule(scope="general", text=text.strip()[:300])],
                      write_descriptions=bool(re.search(r"descri", text, re.I)))
    else:
        prompt = (PROMPT.replace("__TAB__", tab or "the study").replace("__CONTEXT__", _ctx(study, tpl))
                  .replace("__TEXT__", text.strip()[:2000]))
        p = _ask(prompt, _Proposal)
    out = _validate(p, tpl, text)
    out.update(pid=uuid.uuid4().hex[:12], text=text.strip(), tab=tab, at=time.time())
    return out


def write_descriptions(study: dict[str, Any], tpl: dict[str, Any], style: str) -> dict[str, str]:
    els = tpl.get("elements") or []
    if _mock():
        return {e["id"]: f"{e['name']}: {e.get('breakpoint') or 'see video'} (mock description)." for e in els}
    labels = {l["id"]: l["label"] for l in tpl.get("layout") or []}
    rep = study.get("representative_cycle")
    tr: dict[str, list[str]] = {}
    for s in study.get("steps") or []:
        if s["cycle"] == rep:
            tr.setdefault(s["element_id"], []).extend(ln["text"] for ln in s.get("transcript") or [])
    lines = "\n".join(f"{e['id']} | {e['name']} | break point: {e.get('breakpoint') or '-'} | place: "
                      f"{labels.get(e.get('location_id'), '-')} | hand: {e.get('hand', 'both')}"
                      f"{' | seen: ' + ' / '.join(tr[e['id']])[:400] if tr.get(e['id']) else ''}" for e in els)
    rules = "\n".join(f"- {r['text']}" for r in tpl.get("rules") or [] if r["scope"] in ("descriptions", "general"))
    prompt = (DESC_PROMPT.replace("__STYLE__", style).replace("__ELEMENTS__", lines)
              .replace("__RULES__", f"The engineer's standing instructions:\n{rules}" if rules else ""))
    d = _ask(prompt, _Descriptions).descriptions
    ids = {e["id"] for e in els}
    return {k: str(v).strip()[:600] for k, v in d.items() if k in ids and str(v).strip()}


def apply(tpl: dict[str, Any], prop: dict[str, Any], item_ids: list[str], rule_ids: list[str]) -> list[str]:
    """Apply the ticked changes and rules to the template in place. Returns what was done."""
    done = []
    locs = {l["id"]: l for l in tpl.get("layout") or []}
    els = {e["id"]: e for e in tpl.get("elements") or []}
    last_added: dict[str, str] = {}  # anchor -> id of the element added after it most recently
    for it in prop["items"]:
        if it["id"] not in item_ids:
            continue
        op = it["op"]
        if op["type"] == "pos":
            for lid, (x, y) in op["moves"].items():
                if lid in locs:
                    locs[lid]["x"], locs[lid]["y"] = x, y
        elif op["type"] == "loc" and op["id"] in locs:
            locs[op["id"]][op["field"]] = op["value"]
        elif op["type"] == "add":
            lst = tpl.setdefault("elements", [])
            n = len(lst) + 1
            while any(e["id"] == f"E{n}" for e in lst):
                n += 1
            anchor = last_added.get(op["after"], op["after"])
            idx = next((i + 1 for i, e in enumerate(lst) if e["id"] == anchor), 0 if not op["after"] else len(lst))
            ref = els.get(op["after"], {})
            new = {"id": f"E{n}", "name": op["name"], "breakpoint": op["breakpoint"], "location_id": "",
                   "category": "necessary_nva", "hand": ref.get("hand", "both"), "walk": False,
                   "breakpoint_visible": bool(op["breakpoint"]), "issue": "", "breakpoint_suggested": True,
                   "location_suggested": False, "quality": False, "safety": False}
            lst.insert(idx, new)
            els[new["id"]] = new
            last_added[op["after"]] = new["id"]
        elif op["type"] == "el" and op["id"] in els:
            els[op["id"]][op["field"]] = op["value"]
            if op["field"] == "location_id":
                els[op["id"]]["location_suggested"] = False
        done.append(f"{it['label']}: {it['before']} -> {it['after']}")
    rules = tpl.setdefault("rules", [])
    for r in prop["rules"]:
        if r["id"] in rule_ids:
            rules.append({"id": uuid.uuid4().hex[:8], "scope": r["scope"], "text": r["text"],
                          "at": time.time(), "from": prop["text"][:300]})
            done.append(f"Remembered ({r['scope']}): {r['text']}")
    del rules[:-MAX_RULES]
    return done


def rules_text(tpl: dict[str, Any] | None, scopes: tuple[str, ...]) -> str:
    """Standing instructions for a prompt (empty when there are none)."""
    rs = [r["text"] for r in (tpl or {}).get("rules") or [] if r.get("scope") in scopes]
    if not rs:
        return ""
    return ("\nThe engineer's standing instructions for this station (they override the general guidance above "
            "where they conflict):\n" + "\n".join(f"- {t}" for t in rs) + "\n")

