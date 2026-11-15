"""Workspace map: top view of the station with numbered steps and the operator's path.

The model gives places in categories only (column, row, shelf, order). This module turns them
into positions on a grid and draws the chart like the paper form. One drawing unit is one inch,
so sizes are fixed and the overlap check is exact. The same figure is saved as SVG (UI), PNG, PDF.
"""
from __future__ import annotations

import io
import math
import textwrap
from typing import Any

import matplotlib

matplotlib.use("Agg")
from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.patches import Circle, FancyArrowPatch, Polygon, Rectangle  # noqa: E402

COLS = ("far_left", "left", "center", "right", "far_right")
ROW_INDEX = {"back": 2, "middle": 1, "front": 0}
SHELF_ORDER = {"top": 0, "middle": 1, "bottom": 2, None: 3}

RC = 0.17            # step number circle radius
GAP = 0.08           # minimum clearance between numbers / shapes
LABEL_PT = 7         # location label font size
LABEL_LINE = LABEL_PT * 1.25 / 72
CHAR_W = LABEL_PT * 0.56 / 72
INK = "#1f1f1f"
PENCIL = "#3c4043"
GRID = "#d5dbe3"
SHAPES = {  # type -> (kind, size)
    "bin": ("circle", 0.30),
    "bench": ("box", (0.95, 0.5)),
    "fixture": ("box", (0.7, 0.45)),
    "equipment": ("xbox", (0.75, 0.5)),
    "tool": ("box", (0.42, 0.3)),
    "output": ("box", (0.75, 0.42)),
    "none": ("none", 0.25),
}


def chart_input(study: dict[str, Any]) -> dict[str, Any]:
    """What the chart draws: the station template (layout, element locations, element order) when
    the study used one. Only header text and the footer cycle time come from the study."""
    from .template import load, station_key

    tpl = load(study.get("station_key") or station_key(study.get("header", {}))) if study.get("template_used") else None
    if not tpl or not tpl.get("elements"):
        return study
    from .observation import cycle_total, sheet_data
    d = sheet_data(study)
    T = d["T"]
    steps = [{"id": e["id"], "cycle": 1, "start": float(i), "end": float(i + 1), "title": e["name"],
              "location_id": e.get("location_id", ""), "walk": bool(e.get("walk")),
              "category": e.get("category", "necessary_nva"), "quality": bool(e.get("quality")),
              "safety": bool(e.get("safety"))} for i, e in enumerate(tpl["elements"])]
    return {"header": study.get("header", {}), "layout": tpl.get("layout") or [], "steps": steps,
            "representative_cycle": 1, "cycles": [{"index": 1, "start": 0.0, "end": float(T)}] if T else []}


def template_chart_study(tpl: dict[str, Any], header: dict[str, str]) -> dict[str, Any]:
    """Chart of a template on its own (template editor preview)."""
    steps = [{"id": e["id"], "cycle": 1, "start": float(i), "end": float(i + 1), "title": e["name"],
              "location_id": e.get("location_id", ""), "walk": bool(e.get("walk")),
              "category": e.get("category", "necessary_nva"), "quality": bool(e.get("quality")),
              "safety": bool(e.get("safety"))} for i, e in enumerate(tpl.get("elements") or [])]
    return {"header": header, "layout": tpl.get("layout") or [], "steps": steps, "representative_cycle": 1, "cycles": []}


def instruction_steps(study: dict[str, Any]) -> list[dict[str, Any]]:
    """Steps of the instruction cycle in order. Their position + 1 is the step number everywhere."""
    rep = study["representative_cycle"]
    return sorted((s for s in study["steps"] if s["cycle"] == rep), key=lambda s: s["start"])


# ---------------------------------------------------------------- geometry

def _extent(kind: str, size: Any) -> float:
    if kind == "circle" or kind == "none":
        return size
    w, h = size
    return math.hypot(w / 2, h / 2)


def _half_h(kind: str, size: Any) -> float:
    return size if kind in ("circle", "none") else size[1] / 2


def _spots(layout: list[dict[str, Any]], steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group places into drawing spots. Places at the same grid position (stacked bins) share one."""
    from .template import slot_positions

    sp_pos = slot_positions(layout) if layout else {"pos": {}, "width": 1.0}
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for loc in layout:
        x, y = sp_pos["pos"][loc["id"]]
        groups.setdefault((round(x * 4) / 4, round(y * 4) / 4), []).append(loc)
    spots = []
    for (x, y), locs in groups.items():
        locs.sort(key=lambda l: SHELF_ORDER.get(l.get("shelf"), 3))
        kind_type = "bin" if any(l["type"] == "bin" for l in locs) else locs[0]["type"]
        spots.append({"key": (x, y), "xy": (x, y), "locs": locs, "type": kind_type,
                      "ids": {l["id"] for l in locs}, "steps": []})
    valid = {l["id"] for l in layout}
    if any(not s.get("location_id") or s["location_id"] not in valid for s in steps):
        spots.append({"key": ("none",), "xy": (sp_pos["width"] + 0.5, 0.0), "locs": [{"id": "", "label": "No location set",
                      "shelf": None}], "type": "none", "ids": {""}, "steps": []})
    return spots


def _label_lines(spot: dict[str, Any], width_in: float) -> list[str]:
    chars = max(8, int(width_in / CHAR_W))
    lines: list[str] = []
    multi = len(spot["locs"]) > 1
    for loc in spot["locs"]:
        text = loc["label"]
        if multi and loc.get("shelf") and loc["shelf"] not in text.lower():
            text = f"{loc['shelf'].title()}: {text}"
        part = textwrap.wrap(text, chars) or [text]
        if len(part) > 3:  # keep every place of a stacked spot readable
            part = part[:3]
            part[-1] = part[-1][: max(1, chars - 1)] + "…"
        lines += part
    return lines[:9]


def layout_geometry(study: dict[str, Any], grow: dict[tuple, float] | None = None) -> dict[str, Any]:
    layout = study.get("layout") or []
    steps = instruction_steps(study)
    spots = _spots(layout, steps)
    by_id = {i: sp for sp in spots for i in sp["ids"]}
    for n, s in enumerate(steps, 1):
        sp = by_id.get(s.get("location_id") or "") or by_id.get("")
        sp["steps"].append((n, s))

    # numbers fan out over the top 270 degrees of a ring around each spot; the label sits right
    # under the shape in the open bottom wedge. The ring is sized so numbers never touch.
    for sp in spots:
        kind, size = SHAPES.get(sp["type"], SHAPES["bench"])
        sp["kind"], sp["size"] = kind, size
        k = len(sp["steps"])
        sp["step_deg"] = min(60.0, 270.0 / (k - 1)) if k >= 2 else 0.0
        r_min = _extent(kind, size) + GAP + RC
        r_fit = (2 * RC + GAP) / (2 * math.sin(math.radians(sp["step_deg"]) / 2)) if k >= 2 else 0.0
        sp["R"] = max(r_min, r_fit) + (grow or {}).get(sp["key"], 0.0)
        # label must fit between the two numbers at the edges of the bottom wedge
        edge = sp["R"] * math.cos(math.radians(45)) - RC - 0.05
        sp["label"] = _label_lines(sp, max(0.85, 2 * edge))
        sp["label_top"] = _half_h(kind, size) + 0.06
        sp["down"] = max(sp["R"] + RC, sp["label_top"] + len(sp["label"]) * LABEL_LINE + 0.04)
    r_max = max(sp["R"] for sp in spots) if spots else 0.6
    down_max = max(sp["down"] for sp in spots) if spots else 0.6
    pitch_x = max(2 * r_max + 2 * RC + 0.3, 1.5)
    label_h = max(len(sp["label"]) for sp in spots) * LABEL_LINE if spots else 0
    pitch_y = r_max + RC + down_max + 0.35

    # places dragged close together: stretch the grid only in the crowded direction (left-right or
    # front-back) until rings of numbers and labels clear each other
    mx = my = 1.0
    for i, a in enumerate(spots):
        for b in spots[i + 1:]:
            dx = abs(a["xy"][0] - b["xy"][0]) * pitch_x
            dy = abs(a["xy"][1] - b["xy"][1]) * pitch_y
            if math.hypot(dx, dy) <= 0:
                continue
            ring = a["R"] + b["R"] + 2 * RC + GAP
            if math.hypot(dx, dy) >= ring and not (dy > 0 and dx < ring):
                continue
            if dx >= dy:
                mx = max(mx, ring / dx)
            else:
                upper, lower = (a, b) if a["xy"][1] < b["xy"][1] else (b, a)
                my = max(my, max(ring, upper["down"] + lower["R"] + RC + GAP) / dy)
    pitch_x, pitch_y = pitch_x * min(mx, 4.0), pitch_y * min(my, 4.0)
    for sp in spots:
        xu, yu = sp["xy"]
        sp["x"] = xu * pitch_x
        sp["y"] = (2 - yu) * pitch_y
        # numbers left to right over the top, centred on 12 o'clock
        k = len(sp["steps"])
        sp["pos"] = {}
        for i, (n, _) in enumerate(sp["steps"]):
            a = math.radians(90 + sp["step_deg"] * ((k - 1) / 2 - i))
            sp["pos"][n] = (sp["x"] + sp["R"] * math.cos(a), sp["y"] + sp["R"] * math.sin(a))
    xs = [sp["x"] for sp in spots] or [0]
    ys = [sp["y"] for sp in spots] or [0]
    op = ((min(xs) + max(xs)) / 2, min(ys) - (down_max + 0.55))
    return {"steps": steps, "spots": spots, "pitch": (pitch_x, pitch_y), "operator": op,
            "r_max": r_max, "label_h": label_h}


# ---------------------------------------------------------------- drawing

def _clock(s: float) -> str:
    return f"{s:.1f} s"


def build(study: dict[str, Any], grow: dict[tuple, float] | None = None) -> tuple[Figure, dict[str, Any]]:
    g = layout_geometry(study, grow)
    h = study.get("header", {})
    steps, spots = g["steps"], g["spots"]
    pad = 0.45
    xmin = min([sp["x"] - sp["R"] - RC for sp in spots] + [g["operator"][0] - 0.6]) - pad
    xmax = max([sp["x"] + sp["R"] + RC for sp in spots] + [g["operator"][0] + 0.6]) + pad
    ymin = g["operator"][1] - 0.55 - pad
    ymax = max(sp["y"] + sp["R"] + RC for sp in spots) + pad
    cw, ch = xmax - xmin, ymax - ymin
    head, foot, side = 1.15, 0.95, 0.4
    W = max(cw + 2 * side, 11.0)
    H = ch + head + foot + 0.3
    fig = Figure(figsize=(W, H), facecolor="white")
    FigureCanvasAgg(fig)
    ax = fig.add_axes(((W - cw) / 2 / W, (foot + 0.15) / H, cw / W, ch / H))
    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    ax.set_aspect("equal")
    ax.axis("off")
    meta: dict[str, Any] = {"numbers": [], "shapes": [], "issues": []}

    # graph-paper grid
    for gx in _frange(math.floor(xmin * 4) / 4, xmax, 0.25):
        ax.plot([gx, gx], [ymin, ymax], color=GRID, lw=0.4, ls=(0, (1, 2)), zorder=0)
    for gy in _frange(math.floor(ymin * 4) / 4, ymax, 0.25):
        ax.plot([xmin, xmax], [gy, gy], color=GRID, lw=0.4, ls=(0, (1, 2)), zorder=0)
    ax.add_patch(Rectangle((xmin, ymin), cw, ch, fill=False, ec=PENCIL, lw=1.0, zorder=1))

    # places
    for sp in spots:
        x, y, kind, size = sp["x"], sp["y"], sp["kind"], sp["size"]
        if kind == "circle":
            ax.add_patch(Circle((x, y), size, fc="#c9ced6", ec=PENCIL, lw=1.1, hatch="////", zorder=2))
            meta["shapes"].append(("circle", x, y, size, sp))
        elif kind == "none":
            ax.add_patch(Circle((x, y), size, fill=False, ec="#b3261e", lw=1, ls="--", zorder=2))
            ax.text(x, y, "?", ha="center", va="center", fontsize=10, color="#b3261e", zorder=3)
            meta["shapes"].append(("circle", x, y, size, sp))
        else:
            w, hh = size
            ax.add_patch(Rectangle((x - w / 2, y - hh / 2), w, hh, fc="white", ec=PENCIL, lw=1.1,
                                   ls="--" if sp["type"] == "output" else "-", zorder=2))
            if kind == "xbox":
                ax.plot([x - w / 2, x + w / 2], [y - hh / 2, y + hh / 2], color=PENCIL, lw=0.9, zorder=2)
                ax.plot([x - w / 2, x + w / 2], [y + hh / 2, y - hh / 2], color=PENCIL, lw=0.9, zorder=2)
            if sp["type"] == "fixture":
                ax.add_patch(Rectangle((x - w / 4, y - hh / 4), w / 2, hh / 2, fill=False, ec=PENCIL, lw=0.8, zorder=2))
            meta["shapes"].append(("rect", x - w / 2, y - hh / 2, x + w / 2, y + hh / 2, sp))
        top = y - sp["label_top"]
        t = ax.text(x, top, "\n".join(sp["label"]), ha="center", va="top", fontsize=LABEL_PT,
                    color=PENCIL, linespacing=1.15, zorder=5.5,
                    bbox={"boxstyle": "round,pad=0.15", "fc": "white", "ec": "none", "alpha": 0.9})
        meta["shapes"].append(("text", t, sp))

    # operator at the front edge
    ox, oy = g["operator"]
    ax.add_patch(Circle((ox, oy), 0.22, fc="#e8eef8", ec=INK, lw=1.2, zorder=3))
    ax.add_patch(Circle((ox, oy + 0.06), 0.07, fc=INK, ec=INK, zorder=4))
    ax.add_patch(Polygon([(ox - 0.12, oy - 0.12), (ox + 0.12, oy - 0.12), (ox, oy + 0.0)], closed=True, fc=INK, zorder=4))
    ot = ax.text(ox, oy - 0.3, "You", ha="center", va="top", fontsize=8, color=INK, zorder=3)
    meta["shapes"].append(("circle", ox, oy, 0.22, None))
    meta["shapes"].append(("text", ot, None))

    # path between consecutive steps
    pos = {n: p for sp in spots for n, p in sp["pos"].items()}
    spot_of = {n: id(sp) for sp in spots for n in sp["pos"]}
    used: dict[tuple, int] = {}
    for i in range(len(steps) - 1):
        a, b = i + 1, i + 2
        same = spot_of[a] == spot_of[b]
        key = (spot_of[a], spot_of[b])
        k = used.get(key, 0)
        used[key] = k + 1
        rad = 0.45 if same else 0.18 + 0.14 * k
        walk = bool(steps[i + 1].get("walk"))
        ax.add_patch(FancyArrowPatch(pos[a], pos[b], connectionstyle=f"arc3,rad={rad}",
                                     arrowstyle="-|>,head_length=5,head_width=2.6", lw=1.0,
                                     ls=(0, (4, 3)) if walk else "-", color="#0b57d0", alpha=0.85,
                                     shrinkA=RC * 72 + 1.5, shrinkB=RC * 72 + 1.5, zorder=4))

    # numbered circles last, on top
    for n, s in enumerate(steps, 1):
        x, y = pos[n]
        va = s.get("category") == "value_add"
        ax.add_patch(Circle((x, y), RC, fc="#e6f4ea" if va else "white", ec=INK, lw=1.2, zorder=5))
        ax.text(x, y, str(n), ha="center", va="center", fontsize=8.5 if n < 100 else 7,
                fontweight="bold", color=INK, zorder=6)
        meta["numbers"].append((n, s["id"], x, y))
        meta.setdefault("spot_of_number", {})[n] = next(sp["key"] for sp in spots if n in sp["pos"])
        _step_marks(ax, meta, n, s, x, y, next(sp for sp in spots if n in sp["pos"]))

    _header(fig, W, H, h, steps)
    _footer(fig, W, foot, h, study, steps)
    meta["issues"] = _overlaps(fig, ax, meta)
    return fig, meta


def _frange(a: float, b: float, step: float):
    v = a
    while v <= b + 1e-9:
        yield v
        v += step


def _header(fig: Figure, W: float, H: float, h: dict[str, str], steps: list[dict[str, Any]]) -> None:
    hx = fig.add_axes((0, 1 - 1.15 / H, 1, 1.15 / H))
    hx.set_xlim(0, W)
    hx.set_ylim(0, 1.15)
    hx.axis("off")
    hx.text(W / 2, 0.92, "Where each step happens", ha="center", va="center", fontsize=15, fontweight="bold", color=INK)
    frm = steps[0]["title"] if steps else ""
    to = steps[-1]["title"] if steps else ""
    left = [("Task", h.get("job", "") or h.get("project", "")),
            ("Steps", f"First: {_short(frm, 48)}    Last: {_short(to, 48)}")]
    right = [("Where", h.get("line", "")), ("Date", h.get("date", ""))]
    lw = min(7.4, W * 0.62)
    for i, (k, v) in enumerate(left):
        y = 0.52 - i * 0.3
        hx.add_patch(Rectangle((0.4, y - 0.14), lw, 0.28, fill=False, ec=PENCIL, lw=0.8))
        hx.text(0.5, y, f"{k}:", ha="left", va="center", fontsize=8.5, fontweight="bold", color=INK)
        hx.text(1.35, y, v, ha="left", va="center", fontsize=8.5, color=INK)
    rx = W - 0.4 - 3.4
    for i, (k, v) in enumerate(right):
        y = 0.52 - i * 0.3
        hx.add_patch(Rectangle((rx, y - 0.14), 3.4, 0.28, fill=False, ec=PENCIL, lw=0.8))
        hx.text(rx + 0.1, y, f"{k}:", ha="left", va="center", fontsize=8.5, fontweight="bold", color=INK)
        hx.text(rx + 1.25, y, v, ha="left", va="center", fontsize=8.5, color=INK)


def _footer(fig: Figure, W: float, foot: float, h: dict[str, str], study: dict[str, Any],
            steps: list[dict[str, Any]]) -> None:
    fx = fig.add_axes((0, 0.1 / fig.get_figheight(), 1, foot / fig.get_figheight()))
    fx.set_xlim(0, W)
    fx.set_ylim(0, foot)
    fx.axis("off")
    rep = next((c for c in study["cycles"] if c["index"] == study["representative_cycle"]), None)
    cycle = _clock(rep["end"] - rep["start"]) if rep else ""
    takt = f"{h['takt']} s" if h.get("takt") else ""
    cells = [("Check your work here", "quality", ""), ("Take care here", "safety", ""),
             ("Whole task takes", None, cycle), ("Steps", None, str(len(steps)))]
    cw = (W - 0.8) / len(cells)
    for i, (label, sym, val) in enumerate(cells):
        x = 0.4 + i * cw
        fx.add_patch(Rectangle((x, 0.1), cw, foot - 0.2, fill=False, ec=PENCIL, lw=0.8))
        fx.text(x + cw / 2, foot - 0.22, label, ha="center", va="center", fontsize=7.5, color=PENCIL)
        cx, cy = x + cw / 2, 0.36
        if sym == "quality":
            fx.add_patch(Polygon([(cx, cy + 0.15), (cx + 0.15, cy), (cx, cy - 0.15), (cx - 0.15, cy)], closed=True, fill=False, ec=INK, lw=1.1))
        elif sym == "safety":
            fx.add_patch(Circle((cx, cy), 0.15, fill=False, ec=INK, lw=1.1))
            fx.plot([cx - 0.09, cx + 0.09], [cy, cy], color=INK, lw=1.6)
            fx.plot([cx, cx], [cy - 0.09, cy + 0.09], color=INK, lw=1.6)
        elif sym == "swip":
            fx.add_patch(Circle((cx, cy), 0.12, fc=INK, ec=INK))
        else:
            fx.text(cx, cy, val, ha="center", va="center", fontsize=11, fontweight="bold", color=INK)


def _short(t: str, n: int) -> str:
    return t if len(t) <= n else t[: n - 1] + "…"


# ---------------------------------------------------------------- overlap check

MARK_R = 0.085


def _step_marks(ax, meta: dict[str, Any], n: int, s: dict[str, Any], x: float, y: float, sp: dict[str, Any]) -> None:
    """Quality diamond / safety cross just outside the step number, pointing away from the place."""
    kinds = [k for k in ("quality", "safety") if s.get(k)]
    if not kinds:
        return
    dx, dy = x - sp["x"], y - sp["y"]
    d = math.hypot(dx, dy) or 1.0
    ux, uy = dx / d, dy / d
    for j, k in enumerate(kinds):
        ang = (j - (len(kinds) - 1) / 2) * 0.9  # side by side when both
        cx = x + (RC + MARK_R + 0.03) * (ux * math.cos(ang) - uy * math.sin(ang))
        cy = y + (RC + MARK_R + 0.03) * (uy * math.cos(ang) + ux * math.sin(ang))
        if k == "quality":
            r = MARK_R
            ax.add_patch(Polygon([(cx, cy + r), (cx + r, cy), (cx, cy - r), (cx - r, cy)], closed=True,
                                 fc="white", ec=INK, lw=1.0, zorder=6))
        else:
            ax.add_patch(Circle((cx, cy), MARK_R, fc="white", ec=INK, lw=1.0, zorder=6))
            ax.plot([cx - MARK_R * 0.6, cx + MARK_R * 0.6], [cy, cy], color=INK, lw=1.1, zorder=7)
            ax.plot([cx, cx], [cy - MARK_R * 0.6, cy + MARK_R * 0.6], color=INK, lw=1.1, zorder=7)
        meta.setdefault("marks", []).append((n, cx, cy, sp["key"]))


def _overlaps(fig: Figure, ax, meta: dict[str, Any]) -> list[tuple[str, set]]:
    """Exact check: numbers vs numbers, numbers vs places/labels, places/labels vs each other."""
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    inv = ax.transData.inverted()
    items: list[tuple[str, tuple, Any]] = []
    for n, _sid, x, y in meta["numbers"]:
        items.append((f"step {n}", ("c", x, y, RC), ("n", n), meta["spot_of_number"][n]))
    for n, x, y, key in meta.get("marks", []):
        items.append((f"mark of step {n}", ("c", x, y, MARK_R), ("m", n), key))
    for sh in meta["shapes"]:
        owner = id(sh[-1]) if sh[-1] is not None else "op"
        name = (sh[-1]["locs"][0]["label"] if sh[-1] else "operator")
        if sh[0] == "circle":
            items.append((f"place '{name}'", ("c", sh[1], sh[2], sh[3]), ("p", owner), _key(sh[-1])))
        elif sh[0] == "rect":
            items.append((f"place '{name}'", ("r", *sh[1:5]), ("p", owner), _key(sh[-1])))
        else:
            bb = sh[1].get_window_extent(renderer)
            (x0, y0), (x1, y1) = inv.transform([(bb.x0, bb.y0), (bb.x1, bb.y1)])
            items.append((f"label of '{name}'", ("r", x0, y0, x1, y1), ("p", owner), _key(sh[-1])))
    issues = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            if a[2][0] == "p" and b[2][0] == "p" and a[2][1] == b[2][1]:
                continue  # a place and its own label
            if _hit(a[1], b[1]):
                issues.append((f"{a[0]} overlaps {b[0]}", {a[3], b[3]} - {None}))
    return issues


def _key(sp: dict[str, Any] | None) -> tuple | None:
    return sp["key"] if sp else None


def _hit(a: tuple, b: tuple) -> bool:
    if a[0] == "c" and b[0] == "c":
        return math.hypot(a[1] - b[1], a[2] - b[2]) < a[3] + b[3] - 1e-6
    if a[0] == "r" and b[0] == "r":
        return a[1] < b[3] and b[1] < a[3] and a[2] < b[4] and b[2] < a[4]
    c, r = (a, b) if a[0] == "c" else (b, a)
    nx, ny = min(max(c[1], r[1]), r[3]), min(max(c[2], r[2]), r[4])
    return math.hypot(c[1] - nx, c[2] - ny) < c[3] - 1e-6


# ---------------------------------------------------------------- output

def build_clean(study: dict[str, Any]) -> tuple[Figure, dict[str, Any]]:
    """Draw, and while anything overlaps, push the rings of the spots involved outward and redraw."""
    grow: dict[tuple, float] = {}
    for _ in range(8):
        fig, meta = build(study, grow)
        if not meta["issues"]:
            break
        for _msg, keys in meta["issues"]:
            for k in keys:
                grow[k] = grow.get(k, 0.0) + 0.15
    meta["issues"] = [m for m, _ in meta["issues"]]
    return fig, meta


def render(study: dict[str, Any], fmt: str) -> bytes:
    fig, _ = build_clean(chart_input(study))
    buf = io.BytesIO()
    fig.savefig(buf, format=fmt, dpi=200 if fmt == "png" else None, facecolor="white")
    return buf.getvalue()


def render_template(tpl: dict[str, Any], header: dict[str, str], fmt: str) -> bytes:
    fig, _ = build_clean(template_chart_study(tpl, header))
    buf = io.BytesIO()
    fig.savefig(buf, format=fmt, dpi=200 if fmt == "png" else None, facecolor="white")
    return buf.getvalue()


def check(study: dict[str, Any]) -> dict[str, Any]:
    """For tests and the UI: step numbers on the chart and any overlaps."""
    _, meta = build_clean(chart_input(study))
    return {"numbers": [(n, sid) for n, sid, _, _ in meta["numbers"]], "issues": meta["issues"]}
