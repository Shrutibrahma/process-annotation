"""Manual Time Observation Sheet, laid out like the paper form.

Rows are work elements (instruction-cycle order), columns 1-10 are observed cycles. Paper marks:
excluded cycles crossed out (X), highest number circled, lowest repeatable underlined.
The PDF draws those marks; the XLSX is the same layout, editable (circle = bold red there).
"""
from __future__ import annotations

import io
import re
import textwrap
from typing import Any

import matplotlib

matplotlib.use("Agg")
from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.patches import Ellipse, Rectangle  # noqa: E402
from openpyxl import Workbook  # noqa: E402
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402

from .engine import ids_to_labels  # noqa: E402
from .template import element_lookup  # noqa: E402
from .export import SHEET_DECIMALS, _breakpoint, _element_order, _r, lowest_repeatable  # noqa: E402

MAX_CYCLES = 10
ROWS_PER_PAGE = 24
INK = "#1f1f1f"
LINE = "#555b61"


# ---------------------------------------------------------------- numbers

DEV_MIN = 5  # seconds over the element's lowest that count as a fluctuation reason


def reading(t: float) -> float:
    """A stopwatch reading: the clock time at a break point, rounded half up."""
    return _r(t)


def elem_time(s: dict[str, Any]) -> float:
    """Element time = difference of rounded readings, so elements always add up to the cycle."""
    return _r(reading(s["end"]) - reading(s["start"]))


def cycle_total(c: dict[str, Any]) -> float:
    return _r(reading(c["end"]) - reading(c["start"]))


def _elements(study: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(element id, name, break point) in sheet order: the station template's when the study used it."""
    lookup = element_lookup(study)
    present = {s["element_id"] for s in study["steps"]}
    if lookup and present & set(lookup):
        tpl_order = [eid for eid in lookup]  # dict keeps template order
        present = {s["element_id"] for s in study["steps"]}
        return [(eid, lookup[eid]["name"], lookup[eid].get("breakpoint", "")) for eid in tpl_order if eid in present]
    layout = study.get("layout") or []
    return [(eid, ids_to_labels(title, layout), _no_ids(_breakpoint(study, eid))) for eid, title in _element_order(study)]


def sheet_data(study: dict[str, Any]) -> dict[str, Any]:
    h = study.get("header", {})
    cycles = sorted(study["cycles"], key=lambda c: c["index"])[:MAX_CYCLES]
    abnormal = {c["index"] for c in cycles if c["abnormal"]}
    normal = [c["index"] for c in cycles if not c["abnormal"]]
    totals = {c["index"]: cycle_total(c) for c in cycles}
    T, _ = lowest_repeatable([totals[i] for i in normal])
    rows = []
    for eid, name, bp in _elements(study):
        per, unsure = {}, set()
        for c in cycles:
            ss = [s for s in study["steps"] if s["cycle"] == c["index"] and s["element_id"] == eid]
            if ss:
                per[c["index"]] = _r(sum(elem_time(s) for s in ss))
                if any(s.get("covers_gap") for s in ss):
                    unsure.add(c["index"])
        # outlier guard: a reading far from this element's typical time is kept out of lowest/highest
        # unless Gemini saw a reason in that element (then it is a real fluctuation)
        typical = sorted(per[i] for i in normal if i in per and i not in unsure)
        check = set()
        if len(typical) >= 3:
            q = typical[(len(typical) - 1) // 4]  # lower quartile: a few wrong big readings cannot drag it up
            for i in normal:
                v = per.get(i)
                if v is None or i in unsure:
                    continue
                edited = any(s.get("edited") for s in study["steps"] if s["cycle"] == i and s["element_id"] == eid)
                if v - q >= max(8, 2 * q) and not _has_event(study, i, eid) and not edited:
                    check.add(i)
        unsure |= check
        vals = [per[i] for i in normal if i in per and i not in unsure]
        lr, fallback = lowest_repeatable(vals)
        hi = max(vals) if vals else None
        steps = [s for s in study["steps"] if s["element_id"] == eid]
        notes = []
        if check:
            notes.append("? far from typical time, check")
        elif unsure:
            notes.append("? includes a missed break point")
        elif any(s.get("confidence") == "low" for s in steps):
            notes.append("check timing")
        rows.append({"eid": eid, "title": name, "breakpoint": bp, "per": per, "unsure": unsure,
                     "lr": lr, "fallback": fallback, "hi": hi,
                     "fl": _r(hi - lr) if lr is not None and hi is not None else None,
                     "comment": ", ".join(notes)})
    sum_t = _r(sum(r["lr"] or 0 for r in rows))
    names = [r["title"] for r in rows]
    return {
        "header": {
            "job": h.get("job", "") or h.get("project", ""),
            "from": names[0] if names else "", "to": names[-1] if names else "",
            "line": h.get("line", ""), "date": h.get("date", ""), "shift": h.get("shift", ""),
            "takt": f"{h['takt']} s" if h.get("takt") else "", "prepared_by": h.get("prepared_by", ""),
            "operator": h.get("operator", ""),
        },
        "cycles": [c["index"] for c in cycles], "abnormal": abnormal, "totals": totals,
        "rows": rows, "sum_t": sum_t, "T": T,
        "t_minus": _r(T - sum_t) if T is not None else None,
        "more_cycles": max(0, len(study["cycles"]) - MAX_CYCLES),
    }


def _has_event(study: dict[str, Any], cycle: int, eid: str) -> bool:
    c = next((c for c in study["cycles"] if c["index"] == cycle), None)
    return bool(c) and any(ev.get("element_id") == eid for ev in c.get("events") or [])


def cycle_comments(study: dict[str, Any], d: dict[str, Any] | None = None) -> dict[int, str]:
    """Fluctuation reason per cycle, from the numbers: elements well over their lowest valid time.
    A visible event Gemini reported is appended only when it happened in that same element."""
    d = d or sheet_data(study)
    out = {}
    for c in study["cycles"]:
        i = c["index"]
        if c.get("abnormal"):
            why = c.get("x_reason") or "excluded"
            at = f" at {_mmss(c['x_time'])}" if c.get("x_time") is not None else ""
            out[i] = f"X: {why}{at}"
            continue
        if c.get("x_suggested"):  # not ticked: show Gemini's reason so the engineer can decide
            why = c.get("x_reason") or "suggested"
            at = f" at {_mmss(c['x_time'])}" if c.get("x_time") is not None else ""
            sugg = f"Suggested X: {why}{at}"
        else:
            sugg = ""
        events = {}
        for ev in c.get("events") or []:
            if ev.get("element_id"):
                events.setdefault(ev["element_id"], []).append(ev["text"])
        devs = []
        for r in d["rows"]:
            v = r["per"].get(i)
            if v is None or r["lr"] is None or i in r["unsure"]:
                continue
            if v - r["lr"] >= DEV_MIN:
                devs.append((v - r["lr"], r))
        devs.sort(key=lambda x: -x[0])
        parts = []
        for dv, r in devs[:2]:
            ev = "; ".join(dict.fromkeys(events.get(r["eid"], [])))
            parts.append(f"{r['title']} +{_fmt(dv)} s" + (f" ({ev})" if ev else ""))
        checks = [f"{r['title']} {_fmt(r['per'][i])} s?" for r in d["rows"] if i in r["unsure"] and i in r["per"]]
        if checks:
            parts.append("check: " + ", ".join(checks[:2]))
        if sugg:
            parts.insert(0, sugg)
        out[i] = "; ".join(parts)
    return out


def _no_ids(text: str) -> str:
    """Drop layout ids the model sometimes leaves in text ("hand reaches bin L10")."""
    return re.sub(r"\s*\(?\bL\d{1,2}\b\)?", "", text).strip()


def _fmt(v: Any) -> str:
    """Sheet number: tenths as 4.9 / 5.0 / 5.1 (whole seconds when WORKSTUDY_SHEET_DECIMALS=0)."""
    if v is None:
        return ""
    if SHEET_DECIMALS:
        return f"{float(v):.{SHEET_DECIMALS}f}"
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"


# ---------------------------------------------------------------- PDF (paper look)

# column widths in inches: # | element | break point | 1..10 | LR | t adj | highest | fluct | comments
COLW = [0.32, 2.15, 1.05] + [0.37] * MAX_CYCLES + [0.6, 0.52, 0.52, 0.6, 1.05]
HEADS = ["#", "Work Element - t", "Break Point"] + [str(i) for i in range(1, MAX_CYCLES + 1)] + \
        ["Lowest\nRepeatable", "t adjusted", "Highest", "Fluctuation", "Comments"]
PAGE_W, PAGE_H = 11.0, 8.5
X0 = (PAGE_W - sum(COLW)) / 2
ROW_H = 0.235


def render_pdf(study: dict[str, Any]) -> bytes:
    d = sheet_data(study)
    rows = d["rows"]
    pages = max(1, -(-max(len(rows), 1) // ROWS_PER_PAGE))
    buf = io.BytesIO()
    with PdfPages(buf) as pdf:
        for p in range(pages):
            chunk = rows[p * ROWS_PER_PAGE:(p + 1) * ROWS_PER_PAGE]
            fig = _page(d, chunk, p * ROWS_PER_PAGE, last=p == pages - 1, page=(p + 1, pages))
            pdf.savefig(fig)
    return buf.getvalue()


def render_png(study: dict[str, Any]) -> bytes:
    """First page as an image (preview)."""
    d = sheet_data(study)
    fig = _page(d, d["rows"][:ROWS_PER_PAGE], 0, last=len(d["rows"]) <= ROWS_PER_PAGE, page=(1, 1))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=170, facecolor="white")
    return buf.getvalue()


def _page(d: dict[str, Any], chunk: list[dict[str, Any]], start: int, last: bool,
          page: tuple[int, int]) -> Figure:
    fig = Figure(figsize=(PAGE_W, PAGE_H), facecolor="white")
    FigureCanvasAgg(fig)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, PAGE_W)
    ax.set_ylim(PAGE_H, 0)  # y grows downward, like reading the form
    ax.axis("off")

    def box(x, y, w, h, lw=0.8):
        ax.add_patch(Rectangle((x, y), w, h, fill=False, ec=LINE, lw=lw))

    def text(x, y, s, size=8, bold=False, ha="left", va="center", color=INK):
        ax.text(x, y, s, fontsize=size, fontweight="bold" if bold else "normal", ha=ha, va=va, color=color)

    # title
    text(PAGE_W / 2 + 0.6, 0.42, "Manual Time Observation Sheet ©", 15, True, ha="center")
    if page[1] > 1:
        text(PAGE_W - X0, 0.42, f"Page {page[0]} of {page[1]}", 7.5, ha="right", color=LINE)

    # header block: Job/From, Content/To | Line/Operation, Takt | Date, Prepared by | Shift, Operator
    hd = d["header"]
    y0, hh = 0.68, 0.3
    W = sum(COLW)
    blocks = [(0.0, 0.62, [("Job", ""), ("Content", "")]),
              (0.62, 3.5, [("From:", hd["from"]), ("To:", hd["to"])]),
              (4.12, 2.6, [("Line/Operation:", hd["line"]), ("Takt Time", hd["takt"])]),
              (6.72, 2.0, [("Date:", hd["date"]), ("Prepared By:", hd["prepared_by"])]),
              (8.72, W - 8.72, [("Shift:", hd["shift"]), ("Operator:", hd["operator"])])]
    for bx, bw, items in blocks:
        for i, (k, v) in enumerate(items):
            y = y0 + i * hh
            box(X0 + bx, y, bw, hh)
            text(X0 + bx + 0.06, y + hh / 2, k, 7.5, True)
            if v:
                kw = 0.62 if k in ("From:", "To:") else (1.15 if k in ("Line/Operation:", "Prepared By:") else 0.75)
                text(X0 + bx + kw, y + hh / 2, _clip(v, bw - kw - 0.1, 8), 8)

    # table header
    ty = y0 + 2 * hh + 0.12
    head_h = 0.42
    x = X0
    for w, label in zip(COLW, HEADS):
        ax.add_patch(Rectangle((x, ty), w, head_h, fc="#eef1f4", ec=LINE, lw=0.8))
        size = 7.5 if len(label) <= 2 else 6.3
        text(x + w / 2, ty + head_h / 2, label, size, True, ha="center")
        x += w

    # rows: always fill the page like the paper form
    ry = ty + head_h
    n_rows = ROWS_PER_PAGE
    col_x = [X0 + sum(COLW[:i]) for i in range(len(COLW))]
    c0 = 3  # first cycle column
    for r in range(n_rows):
        y = ry + r * ROW_H
        for i, w in enumerate(COLW):
            box(col_x[i], y, w, ROW_H, lw=0.5)
        if r >= len(chunk):
            continue
        row = chunk[r]
        cy = y + ROW_H / 2
        text(col_x[0] + COLW[0] / 2, cy, str(start + r + 1), 7.5, ha="center")
        _cell_text(ax, col_x[1], cy, COLW[1], row["title"], 7.6)
        _cell_text(ax, col_x[2], cy, COLW[2], row["breakpoint"], 6.4, color=LINE)
        for j, ci in enumerate(d["cycles"]):
            v = row["per"].get(ci)
            if v is None:
                continue
            cx = col_x[c0 + j] + COLW[c0 + j] / 2
            text(cx, cy, _fmt(v) + ("?" if ci in row["unsure"] else ""), 8, ha="center")
            if ci in row["unsure"]:
                continue
            if ci in d["abnormal"]:  # X: cross out time error
                hw = COLW[c0 + j] * 0.32
                ax.plot([cx - hw, cx + hw], [cy - ROW_H * 0.36, cy + ROW_H * 0.36], color="#b3261e", lw=0.9)
                ax.plot([cx - hw, cx + hw], [cy + ROW_H * 0.36, cy - ROW_H * 0.36], color="#b3261e", lw=0.9)
            elif row["hi"] is not None and v == row["hi"] and row["hi"] != row["lr"]:  # circle highest
                ax.add_patch(Ellipse((cx, cy), COLW[c0 + j] * 0.8, ROW_H * 0.86, fill=False, ec=INK, lw=0.9))
            elif row["lr"] is not None and v == row["lr"]:  # underline lowest repeatable
                ax.plot([cx - 0.11, cx + 0.11], [cy + 0.075, cy + 0.075], color=INK, lw=0.9)
        k = c0 + MAX_CYCLES
        lr = _fmt(row["lr"])
        text(col_x[k] + COLW[k] / 2, cy, lr, 8, True, ha="center")
        text(col_x[k + 2] + COLW[k + 2] / 2, cy, _fmt(row["hi"]), 8, ha="center")
        text(col_x[k + 3] + COLW[k + 3] / 2, cy, _fmt(row["fl"]), 8, ha="center")
        _cell_text(ax, col_x[k + 4], cy, COLW[k + 4], row["comment"], 6.4, color=LINE)

    # bottom: Σt box and legend (last page)
    by = ry + n_rows * ROW_H + 0.18
    if last:
        box(X0, by, 3.6, 0.72)
        text(X0 + 0.15, by + 0.22, "Σt =", 9, True)
        text(X0 + 0.75, by + 0.22, _fmt(d["sum_t"]), 9)
        ax.plot([X0 + 0.7, X0 + 1.6], [by + 0.3, by + 0.3], color=LINE, lw=0.6)
        text(X0 + 0.15, by + 0.52, "T - Σt =", 9, True)
        tm = _fmt(d["t_minus"]) + (f"   (T = {_fmt(d['T'])})" if d["T"] is not None else "")
        text(X0 + 0.95, by + 0.52, tm, 9)
        ax.plot([X0 + 0.9, X0 + 1.6], [by + 0.6, by + 0.6], color=LINE, lw=0.6)
        text(X0 + 2.15, by + 0.52, "(Adjust Elements times)", 7, color=LINE)
        lx = X0 + W - 6.4
        text(lx, by + 0.1, "Legend:", 8, True)
        ax.plot([lx + 0.05, lx + 0.2], [by + 0.3, by + 0.44], color="#b3261e", lw=1)
        ax.plot([lx + 0.05, lx + 0.2], [by + 0.44, by + 0.3], color="#b3261e", lw=1)
        text(lx + 0.3, by + 0.37, "Cross out Time error", 7.5)
        ax.add_patch(Ellipse((lx + 1.85, by + 0.37), 0.2, 0.18, fill=False, ec=INK, lw=0.9))
        text(lx + 2.02, by + 0.37, "Circle Highest Number", 7.5)
        ax.plot([lx + 3.55, lx + 3.8], [by + 0.43, by + 0.43], color=INK, lw=1)
        text(lx + 3.88, by + 0.37, "Underline Lowest Repeatable", 7.5)
        text(lx + 0.05, by + 0.62, "Lowest = lowest valid (non-X) time.  ? = check (missed break point or far from typical), not used", 7, color=LINE)
        if d["more_cycles"]:
            text(lx + 3.0, by + 0.62, f"{d['more_cycles']} more cycle(s) not shown (form has 10 columns)", 7, color="#b3261e")
    return fig


def _clip(s: str, width_in: float, size: float) -> str:
    chars = max(4, int(width_in / (size * 0.55 / 72)))
    return s if len(s) <= chars else s[: chars - 1] + "…"


def _cell_text(ax, x: float, cy: float, w: float, s: str, size: float, color: str = INK) -> None:
    if not s:
        return
    chars = max(4, int((w - 0.08) / (size * 0.53 / 72)))
    if len(s) <= chars:
        ax.text(x + 0.05, cy, s, fontsize=size, va="center", color=color)
        return
    small = size - 1.2
    chars = max(4, int((w - 0.08) / (small * 0.53 / 72)))
    lines = textwrap.wrap(s, chars)[:2]
    if len(textwrap.wrap(s, chars)) > 2:
        lines[-1] = lines[-1][: chars - 1] + "…"
    ax.text(x + 0.05, cy, "\n".join(lines), fontsize=small, va="center", color=color, linespacing=1.0)


# ---------------------------------------------------------------- XLSX (same layout, editable)

THIN = Side(style="thin", color="7F868D")
NUMFMT = "0.0" if SHEET_DECIMALS else "0"
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
B = Font(bold=True)
HEAD_FILL = PatternFill("solid", fgColor="EEF1F4")
HIGH = Font(bold=True, color="B83A3A")      # circled on paper
LOW = Font(bold=True, underline="single")    # underlined on paper
XOUT = Font(strike=True, color="8A949C")     # crossed out on paper


def add_sheet(wb: Workbook, study: dict[str, Any]) -> None:
    d = sheet_data(study)
    ws = wb.active
    ws.title = "Time Observation"
    ncol = 3 + MAX_CYCLES + 5
    last = get_column_letter(ncol)
    ws.merge_cells(f"A1:{last}1")
    ws["A1"] = "Manual Time Observation Sheet ©"
    ws["A1"].font = Font(bold=True, size=15)
    ws["A1"].alignment = Alignment(horizontal="center")
    hd = d["header"]
    # header: same blocks as the form
    layout = [  # (row, first col, last col, text)
        (2, 1, 1, "Job"), (2, 2, 3, f"From: {hd['from']}"),
        (2, 4, 9, f"Line/Operation: {hd['line']}"), (2, 10, 14, f"Date: {hd['date']}"),
        (2, 15, ncol, f"Shift: {hd['shift']}"),
        (3, 1, 1, "Content"), (3, 2, 3, f"To: {hd['to']}"),
        (3, 4, 9, f"Takt Time: {hd['takt']}"), (3, 10, 14, f"Prepared By: {hd['prepared_by']}"),
        (3, 15, ncol, f"Operator: {hd['operator']}"),
    ]
    for row, c1, c2, val in layout:
        if c2 > c1:
            ws.merge_cells(start_row=row, start_column=c1, end_row=row, end_column=c2)
        cell = ws.cell(row=row, column=c1, value=val)
        cell.font = B if c1 == 1 else Font()
        for c in range(c1, c2 + 1):
            ws.cell(row=row, column=c).border = BOX
    r = 5
    for col, label in enumerate(HEADS, 1):
        cell = ws.cell(row=r, column=col, value=label.replace("\n", " "))
        cell.font, cell.fill, cell.border = B, HEAD_FILL, BOX
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[r].height = 30
    n_rows = max(ROWS_PER_PAGE, len(d["rows"]))
    for i in range(n_rows):
        rr = r + 1 + i
        for col in range(1, ncol + 1):
            ws.cell(row=rr, column=col).border = BOX
        if i >= len(d["rows"]):
            continue
        row = d["rows"][i]
        ws.cell(row=rr, column=1, value=i + 1).alignment = Alignment(horizontal="center")
        ws.cell(row=rr, column=2, value=row["title"])
        ws.cell(row=rr, column=3, value=row["breakpoint"]).font = Font(size=9, color="555B61")
        for j, ci in enumerate(d["cycles"]):
            v = row["per"].get(ci)
            cell = ws.cell(row=rr, column=4 + j, value=f"{_fmt(v)}?" if v is not None and ci in row["unsure"] else v)
            cell.alignment = Alignment(horizontal="center")
            if isinstance(cell.value, (int, float)):
                cell.number_format = NUMFMT
            if v is None or ci in row["unsure"]:
                continue
            if ci in d["abnormal"]:
                cell.font = XOUT
            elif row["hi"] is not None and v == row["hi"] and row["hi"] != row["lr"]:
                cell.font = HIGH
            elif row["lr"] is not None and v == row["lr"]:
                cell.font = LOW
        k = 4 + MAX_CYCLES
        lr = ws.cell(row=rr, column=k, value=row["lr"])
        lr.font, lr.alignment, lr.number_format = B, Alignment(horizontal="center"), NUMFMT
        ws.cell(row=rr, column=k + 2).number_format = NUMFMT
        ws.cell(row=rr, column=k + 3).number_format = NUMFMT
        ws.cell(row=rr, column=k + 2, value=row["hi"]).alignment = Alignment(horizontal="center")
        ws.cell(row=rr, column=k + 3, value=row["fl"]).alignment = Alignment(horizontal="center")
        ws.cell(row=rr, column=k + 4, value=row["comment"]).font = Font(size=9, color="555B61")
    br = r + n_rows + 2
    ws.cell(row=br, column=1, value="Σt =").font = B
    ws.cell(row=br, column=3, value=d["sum_t"])
    ws.cell(row=br + 1, column=1, value="T - Σt =").font = B
    ws.cell(row=br + 1, column=3, value=d["t_minus"])
    ws.cell(row=br + 1, column=4, value=f"(Adjust Elements times)   T = {_fmt(d['T'])}")
    ws.cell(row=br, column=10, value="Legend:").font = B
    ws.cell(row=br, column=11, value="strikethrough = X, cross out time error").font = XOUT
    ws.cell(row=br + 1, column=11, value="bold red = circled highest number").font = HIGH
    ws.cell(row=br + 2, column=11, value="underline = lowest repeatable").font = LOW
    ws.cell(row=br + 3, column=11, value="lowest = lowest valid (non-X) time; ? = check (missed break point or far from typical)")
    widths = [5, 30, 16] + [5.5] * MAX_CYCLES + [10, 9, 8, 10, 18]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = f"{r}:{r}"


# ---------------------------------------------------------------- Observation Preparation Sheet

def _mmss(t: float) -> str:
    t = int(t + 0.5)
    return f"{t // 60}:{t % 60:02d}"


def prep_data(study: dict[str, Any]) -> dict[str, Any]:
    """One row per observed cycle: span, total, comment; lowest repeatable / highest / fluctuation."""
    d = sheet_data(study)
    auto = cycle_comments(study, d)
    cycles = sorted(study["cycles"], key=lambda c: c["index"])
    totals = {c["index"]: cycle_total(c) for c in cycles}
    normal = [totals[c["index"]] for c in cycles if not c["abnormal"]]
    low, fallback = lowest_repeatable(normal)
    high = max(normal) if normal else None
    rep = study["representative_cycle"]
    elements = [r["title"] for r in d["rows"]]
    return {"header": d["header"], "elements": elements, "low": low, "low_fallback": fallback, "high": high,
            "fluct": _r(high - low) if normal else None,
            "rows": [{"index": c["index"], "span": f"{_mmss(c['start'])} - {_mmss(c['end'])}",
                      "secs": totals[c["index"]], "x": c["abnormal"],
                      "note": (c.get("note") or "").strip() or auto.get(c["index"], "")}
                     for c in cycles]}


PREP_COLW = [0.6, 2.2, 3.5, 4.1]  # # | total cycle time | comment | draft for work elements


def prep_page(study: dict[str, Any]) -> Figure:
    p = prep_data(study)
    fig = Figure(figsize=(PAGE_W, PAGE_H), facecolor="white")
    FigureCanvasAgg(fig)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, PAGE_W)
    ax.set_ylim(PAGE_H, 0)
    ax.axis("off")
    W = sum(PREP_COLW)
    x0 = (PAGE_W - W) / 2

    def box(x, y, w, h, lw=0.8, fc="none"):
        ax.add_patch(Rectangle((x, y), w, h, fc=fc, ec=LINE, lw=lw))

    def text(x, y, s, size=8, bold=False, ha="left", va="center", color=INK):
        ax.text(x, y, s, fontsize=size, fontweight="bold" if bold else "normal", ha=ha, va=va, color=color)

    text(PAGE_W / 2 + 0.6, 0.42, "Observation Preparation Sheet ©", 15, True, ha="center")
    hd, y0, hh = p["header"], 0.68, 0.3
    blocks = [(0.0, 0.7, [("Job", ""), ("Content", "")]),
              (0.7, 3.7, [("From:", hd["from"]), ("To:", hd["to"])]),
              (4.4, 3.0, [("Line/Operation:", hd["line"]), ("Takt Time", hd["takt"])]),
              (7.4, W - 7.4, [("Date:", hd["date"]), ("Prepared By:", hd["prepared_by"])])]
    for bx, bw, items in blocks:
        for i, (k, v) in enumerate(items):
            y = y0 + i * hh
            box(x0 + bx, y, bw, hh)
            text(x0 + bx + 0.06, y + hh / 2, k, 7.5, True)
            if v:
                kw = 0.55 if k in ("From:", "To:") else (1.15 if k in ("Line/Operation:", "Prepared By:") else 0.8)
                text(x0 + bx + kw, y + hh / 2, _clip(v, bw - kw - 0.1, 8), 8)

    ty, head_h = y0 + 2 * hh + 0.15, 0.4
    heads = ["#", "Total Cycle Time", "Comment for Fluctuation Reason", "Draft for Work Elements"]
    cx = [x0 + sum(PREP_COLW[:i]) for i in range(4)]
    for i, (w, label) in enumerate(zip(PREP_COLW, heads)):
        box(cx[i], ty, w, head_h, fc="#eef1f4")
        text(cx[i] + w / 2, ty + head_h / 2, label, 8, True, ha="center")

    n = max(10, len(p["rows"]))
    avail = PAGE_H - (ty + head_h) - 1.0
    row_h = min(0.5, avail / n)
    ry = ty + head_h
    for r in range(n):
        y = ry + r * row_h
        for i in range(3):
            box(cx[i], y, PREP_COLW[i], row_h, lw=0.5)
        if r >= len(p["rows"]):
            continue
        row = p["rows"][r]
        mid = y + row_h / 2
        text(cx[0] + PREP_COLW[0] / 2, mid, f"{row['index']}{' X' if row['x'] else ''}", 10, ha="center")
        secs = f"{_fmt(row['secs'])} secs"
        text(cx[1] + 0.12, mid - row_h * 0.2, row["span"], 9)
        sx = cx[1] + 1.25
        sy = mid + row_h * 0.2
        text(sx, sy, secs, 9, ha="center")
        if row["x"]:  # cross out time error
            ax.plot([cx[1] + 0.08, cx[1] + PREP_COLW[1] - 0.08], [y + 0.05, y + row_h - 0.05], color="#b3261e", lw=1)
            ax.plot([cx[1] + 0.08, cx[1] + PREP_COLW[1] - 0.08], [y + row_h - 0.05, y + 0.05], color="#b3261e", lw=1)
        elif p["high"] is not None and row["secs"] == p["high"] and p["high"] != p["low"]:
            ax.add_patch(Ellipse((sx, sy), 0.95, min(0.24, row_h * 0.5), fill=False, ec=INK, lw=1))
        elif p["low"] is not None and row["secs"] == p["low"]:
            ax.plot([sx - 0.4, sx + 0.4], [sy + 0.09, sy + 0.09], color=INK, lw=1)
        if row["note"]:
            _cell_text(ax, cx[2], mid, PREP_COLW[2], row["note"], 8.5)

    # draft for work elements: one block, numbered in reading order, two columns when needed
    bh = n * row_h
    box(cx[3], ry, PREP_COLW[3], bh, lw=0.5)
    els = p["elements"]
    per_col = max(1, int((bh - 0.15) / 0.17))
    cols = 1 if len(els) <= per_col else 2
    colw = (PREP_COLW[3] - 0.15) / cols
    for i, title in enumerate(els[: per_col * cols]):
        c, rr = divmod(i, per_col)
        text(cx[3] + 0.1 + c * colw, ry + 0.15 + rr * 0.17, _clip(f"{i + 1}. {title}", colw - 0.05, 7.2), 7.2)
    if len(els) > per_col * cols:
        text(cx[3] + 0.1, ry + bh - 0.1, f"+ {len(els) - per_col * cols} more (see Time Observation)", 7, color=LINE)

    by = ry + bh + 0.25
    box(x0, by, 0.95, 0.4)
    text(x0 + 0.08, by + 0.2, "Fluctuation", 8, True)
    box(x0 + 0.95, by, 1.85, 0.4)
    if p["fluct"] is not None:
        text(x0 + 1.15, by + 0.2, f"{_fmt(p['fluct'])} secs", 10)
    note = "= highest − lowest (valid, non-X cycles)"
    text(x0, by + 0.55, note, 7, color=LINE)
    lx = x0 + W - 5.6
    text(lx, by + 0.05, "Legend:", 8, True)
    ax.plot([lx + 0.05, lx + 0.2], [by + 0.25, by + 0.39], color="#b3261e", lw=1)
    ax.plot([lx + 0.05, lx + 0.2], [by + 0.39, by + 0.25], color="#b3261e", lw=1)
    text(lx + 0.3, by + 0.32, "Cross out Time error", 7.5)
    ax.add_patch(Ellipse((lx + 1.85, by + 0.32), 0.2, 0.18, fill=False, ec=INK, lw=0.9))
    text(lx + 2.02, by + 0.32, "Circle Highest Number", 7.5)
    ax.plot([lx + 3.55, lx + 3.8], [by + 0.38, by + 0.38], color=INK, lw=1)
    text(lx + 3.88, by + 0.32, "Underline Lowest Repeatable", 7.5)
    return fig


def render_prep_png(study: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    prep_page(study).savefig(buf, format="png", dpi=170, facecolor="white")
    return buf.getvalue()


def render_study_pdf(study: dict[str, Any]) -> bytes:
    """Observation Preparation Sheet, then the Manual Time Observation Sheet pages."""
    d = sheet_data(study)
    rows = d["rows"]
    pages = max(1, -(-max(len(rows), 1) // ROWS_PER_PAGE))
    buf = io.BytesIO()
    with PdfPages(buf) as pdf:
        pdf.savefig(prep_page(study))
        for pg in range(pages):
            chunk = rows[pg * ROWS_PER_PAGE:(pg + 1) * ROWS_PER_PAGE]
            pdf.savefig(_page(d, chunk, pg * ROWS_PER_PAGE, last=pg == pages - 1, page=(pg + 1, pages)))
    return buf.getvalue()


def add_prep_sheet(wb: Workbook, study: dict[str, Any], ws) -> None:
    p = prep_data(study)
    ws.title = "Observation Prep"
    ws.merge_cells("A1:F1")
    ws["A1"] = "Observation Preparation Sheet ©"
    ws["A1"].font = Font(bold=True, size=15)
    ws["A1"].alignment = Alignment(horizontal="center")
    hd = p["header"]
    for row, c1, c2, val, bold in [(2, 1, 1, "Job", True), (2, 2, 3, f"From: {hd['from']}", False),
                                   (2, 4, 4, f"Line/Operation: {hd['line']}", False),
                                   (2, 5, 6, f"Date: {hd['date']}", False),
                                   (3, 1, 1, "Content", True), (3, 2, 3, f"To: {hd['to']}", False),
                                   (3, 4, 4, f"Takt Time: {hd['takt']}", False),
                                   (3, 5, 6, f"Prepared By: {hd['prepared_by']}", False)]:
        if c2 > c1:
            ws.merge_cells(start_row=row, start_column=c1, end_row=row, end_column=c2)
        ws.cell(row=row, column=c1, value=val).font = B if bold else Font()
        for c in range(c1, c2 + 1):
            ws.cell(row=row, column=c).border = BOX
    r = 5
    labels = ["#", "Total Cycle Time", "Secs", "Comment for Fluctuation Reason", "", "Draft for Work Elements"]
    for col, label in enumerate(labels, 1):
        cell = ws.cell(row=r, column=col, value=label or None)
        cell.font, cell.fill, cell.border = B, HEAD_FILL, BOX
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.merge_cells(start_row=r, start_column=4, end_row=r, end_column=5)
    n = max(10, len(p["rows"]))
    for i in range(n):
        rr = r + 1 + i
        ws.merge_cells(start_row=rr, start_column=4, end_row=rr, end_column=5)
        for col in range(1, 6):
            ws.cell(row=rr, column=col).border = BOX
        ws.row_dimensions[rr].height = 22
        if i >= len(p["rows"]):
            continue
        row = p["rows"][i]
        ws.cell(row=rr, column=1, value=f"{row['index']}{' X' if row['x'] else ''}").alignment = Alignment(horizontal="center")
        ws.cell(row=rr, column=2, value=row["span"])
        sc = ws.cell(row=rr, column=3, value=row["secs"])
        sc.alignment, sc.number_format = Alignment(horizontal="center"), NUMFMT
        if row["x"]:
            sc.font = XOUT
            ws.cell(row=rr, column=2).font = XOUT
        elif p["high"] is not None and row["secs"] == p["high"] and p["high"] != p["low"]:
            sc.font = HIGH
        elif p["low"] is not None and row["secs"] == p["low"]:
            sc.font = LOW
        ws.cell(row=rr, column=4, value=row["note"] or None).alignment = Alignment(wrap_text=True, vertical="center")
    ws.merge_cells(start_row=r + 1, start_column=6, end_row=r + n, end_column=6)
    draft = ws.cell(row=r + 1, column=6, value="\n".join(f"{i}. {t}" for i, t in enumerate(p["elements"], 1)))
    draft.alignment = Alignment(wrap_text=True, vertical="top")
    for rr in range(r + 1, r + n + 1):
        ws.cell(row=rr, column=6).border = BOX
    fr = r + n + 2
    ws.cell(row=fr, column=1, value="Fluctuation").font = B
    ws.cell(row=fr, column=3, value=p["fluct"])
    ws.cell(row=fr, column=4, value="secs (highest − lowest repeatable)")
    ws.cell(row=fr + 2, column=1, value="Legend").font = B
    ws.cell(row=fr + 3, column=1, value="strikethrough = X, cross out time error").font = XOUT
    ws.cell(row=fr + 4, column=1, value="bold red = circled highest number").font = HIGH
    ws.cell(row=fr + 5, column=1, value="underline = lowest repeatable").font = LOW
    for i, w in enumerate([6, 18, 8, 30, 14, 48], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.page_setup.orientation = "landscape"
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
