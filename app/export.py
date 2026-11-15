"""Exports: work instructions (HTML + per-step image and clip, zipped) and time study (XLSX)."""
from __future__ import annotations

import html
import math
import os
import tempfile
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from . import media

CAT_LABEL = {"value_add": "Value-add", "necessary_nva": "Non-value-add", "waste": "Non-value-add"}
# Paper forms record whole seconds; override with WORKSTUDY_SHEET_DECIMALS=1 for tenths.
SHEET_DECIMALS = int(os.environ.get("WORKSTUDY_SHEET_DECIMALS", "1"))


class ExportError(RuntimeError):
    pass


def _cycle_steps(study: dict[str, Any], cycle: int) -> list[dict[str, Any]]:
    return sorted((s for s in study["steps"] if s["cycle"] == cycle), key=lambda s: s["start"])


# ---------------------------------------------------------------- work instructions

def build_instructions_zip(study: dict[str, Any], video: Path, out_zip: Path,
                           cycle: int | None = None) -> None:
    cycle = cycle or study["representative_cycle"]
    steps = _cycle_steps(study, cycle)
    if not steps:
        raise ExportError(f"Cycle {cycle} has no steps to export.")
    h = study.get("header", {})
    e = html.escape

    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        (tmpd / "media").mkdir()
        blocks = []
        for i, s in enumerate(steps, 1):
            img, clip = f"media/step_{i:02d}.jpg", f"media/step_{i:02d}.mp4"
            media.extract_frame(video, (s["start"] + s["end"]) / 2, tmpd / img)
            media.extract_clip(video, s["start"], s["end"], tmpd / clip)
            lists = "".join(
                f"<p><strong>{label}:</strong> {e(', '.join(s[key]))}</p>"
                for key, label in (("materials", "Parts"), ("tools", "Tools"),
                                   ("key_points", "Quality and safety notes")) if s[key])
            blocks.append(f"""
<section class="step">
  <h2>{i}. {e(s['title'])}</h2>
  <p class="meta">{s['end'] - s['start']:.1f} s &nbsp;|&nbsp; {CAT_LABEL[s['category']]} &nbsp;|&nbsp; Hand: {e(s['hand'])}</p>
  <div class="media"><img src="{img}" alt="Step {i}"><video src="{clip}" controls loop muted playsinline></video></div>
  {f"<p>{e(s['description'])}</p>" if s['description'] else ""}
  {_transcript_html(s)}
  {lists}
</section>""")
        c = next(c for c in study["cycles"] if c["index"] == cycle)
        doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Step guide: {e(h.get('job', ''))}</title>
<style>
body{{font-family:"IBM Plex Sans",Arial,sans-serif;color:#1d2329;max-width:960px;margin:24px auto;padding:0 16px;line-height:1.45}}
table{{border-collapse:collapse;margin-bottom:24px}} td{{border:1px solid #c9d0d6;padding:4px 10px}}
.step{{border-top:2px solid #1d2329;padding:12px 0 20px;break-inside:avoid}}
h2{{font-size:1.15rem;margin:0 0 4px}} .meta{{color:#5b6670;margin:0 0 10px}}
.media{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}
.transcript{{padding-left:20px;margin:10px 0}} .transcript li{{margin:2px 0}} .ts{{color:#5b6670;font-variant-numeric:tabular-nums;margin-right:6px}} .media img,.media video{{width:100%;border:1px solid #c9d0d6}}
@media print{{video{{display:none}} .media{{grid-template-columns:1fr}}}}
</style></head><body>
<h1>Step guide: {e(h.get('job', '') or h.get('project', ''))}</h1>
<p class="meta">{len(steps)} steps · about {c['end'] - c['start']:.0f} seconds in all{(' · ' + e(h.get('line', ''))) if h.get('line') else ''}. Every step is shown and written out; no sound is needed.</p>
{''.join(blocks)}
</body></html>"""
        (tmpd / "instructions.html").write_text(doc, encoding="utf-8")
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(tmpd.rglob("*")):
                if p.is_file():
                    zf.write(p, p.relative_to(tmpd).as_posix())


def _transcript_html(step: dict[str, Any]) -> str:
    lines = step.get("transcript") or []
    if not lines:
        return ""
    items = "".join(f"<li><span class=\"ts\">{_clock_tenths(ln['t'] - step['start'])}</span> "
                    f"{html.escape(ln['text'])}</li>" for ln in lines)
    return f'<ol class="transcript">{items}</ol>'


def _clock_tenths(t: float) -> str:
    return f"+{max(t, 0):.1f} s"


# ---------------------------------------------------------------- time study

def lowest_repeatable(values: list[float]) -> tuple[float | None, bool]:
    """Lowest valid value (callers pass non-X values only). With WORKSTUDY_LOWEST=repeat: lowest value
    that occurs at least twice, falling back to the minimum (flag=True) if none repeats."""
    if not values:
        return None, False
    if os.environ.get("WORKSTUDY_LOWEST", "lowest").lower() != "repeat":
        return min(values), False
    repeated = [v for v, n in Counter(values).items() if n >= 2]
    return (min(repeated), False) if repeated else (min(values), True)


def _r(x: float) -> float:
    """Round half up, like a stopwatch reading (Python's round() sends 6.5 to 6)."""
    k = 10 ** SHEET_DECIMALS
    v = math.floor(x * k + 0.5 + 1e-9) / k
    return int(v) if SHEET_DECIMALS == 0 else round(v, SHEET_DECIMALS)


THIN = Side(style="thin", color="9AA4AD")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
HEAD = Font(bold=True)
HEAD_FILL = PatternFill("solid", fgColor="E3E7EB")
HIGH = Font(bold=True, color="B83A3A")          # paper: circle highest
LOW = Font(underline="single")                  # paper: underline lowest repeatable
XOUT = Font(strike=True, color="8A949C")        # paper: X / cross out


def _header_block(ws, study: dict[str, Any], title: str) -> int:
    h = study.get("header", {})
    ws["A1"] = title
    ws["A1"].font = Font(bold=True, size=14)
    rows = [("Job", h.get("job", "")), ("Line/Operation", h.get("line", "")),
            ("Takt time", h.get("takt", "")), ("Operator", h.get("operator", "")),
            ("Prepared by", h.get("prepared_by", "")), ("Date", h.get("date", ""))]
    for i, (k, v) in enumerate(rows):
        ws.cell(row=2 + i // 3, column=1 + (i % 3) * 2, value=k).font = HEAD
        ws.cell(row=2 + i // 3, column=2 + (i % 3) * 2, value=v)
    return 5


def build_time_study_xlsx(study: dict[str, Any], out: Path) -> None:
    cycles = study["cycles"]
    if not cycles:
        raise ExportError("Study has no cycles.")
    wb = Workbook()

    # --- Observation Preparation Sheet
    ws = wb.active
    ws.title = "Observation Prep"
    r = _header_block(ws, study, "Observation Preparation Sheet")
    for col, label in enumerate(["#", "Total cycle time", "Duration (s)", "Comment for fluctuation reason"], 1):
        cell = ws.cell(row=r, column=col, value=label)
        cell.font, cell.fill, cell.border = HEAD, HEAD_FILL, BOX
    totals = {c["index"]: _r(c["end"] - c["start"]) for c in cycles}
    normal = [totals[c["index"]] for c in cycles if not c["abnormal"]]
    t_lr, _ = lowest_repeatable(normal)
    t_hi = max(normal) if normal else None
    for i, c in enumerate(cycles, 1):
        row = r + i
        span = f"{_clock(c['start'])} - {_clock(c['end'])}"
        vals = [f"{c['index']}{' X' if c['abnormal'] else ''}", span, totals[c["index"]], c.get("note", "")]
        for col, v in enumerate(vals, 1):
            cell = ws.cell(row=row, column=col, value=v)
            cell.border = BOX
        dcell = ws.cell(row=row, column=3)
        if c["abnormal"]:
            dcell.font = XOUT
        elif totals[c["index"]] == t_hi and t_hi != t_lr:
            dcell.font = HIGH
        elif totals[c["index"]] == t_lr:
            dcell.font = LOW
    fr = r + len(cycles) + 2
    ws.cell(row=fr, column=1, value="Fluctuation (s)").font = HEAD
    ws.cell(row=fr, column=3, value=(t_hi - t_lr) if normal else None)
    _legend(ws, fr + 2)
    _widths(ws, [8, 22, 14, 60])

    # --- Manual Time Observation Sheet
    ws = wb.create_sheet("Time Observation")
    r = _header_block(ws, study, "Manual Time Observation Sheet")
    cyc_idx = [c["index"] for c in cycles]
    abnormal = {c["index"] for c in cycles if c["abnormal"]}
    elements = _element_order(study)
    headers = ["#", "Work element", "Break point"] + [str(i) for i in range(1, len(cyc_idx) + 1)] + \
              ["Lowest repeatable", "t adjusted", "Highest", "Fluctuation", "Comments"]
    for col, label in enumerate(headers, 1):
        cell = ws.cell(row=r, column=col, value=label)
        cell.font, cell.fill, cell.border = HEAD, HEAD_FILL, BOX
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    n_c = len(cyc_idx)
    sum_t = 0.0
    for i, (eid, title) in enumerate(elements, 1):
        row = r + i
        per_cycle = {}
        for ci in cyc_idx:
            ss = [s for s in study["steps"] if s["cycle"] == ci and s["element_id"] == eid]
            if ss:
                per_cycle[ci] = _r(sum(s["end"] - s["start"] for s in ss))
        vals = [per_cycle[ci] for ci in cyc_idx if ci in per_cycle and ci not in abnormal]
        lr, fallback = lowest_repeatable(vals)
        hi = max(vals) if vals else None
        sum_t += lr or 0
        comments = sorted({s["description"] for s in study["steps"]
                           if s["element_id"] == eid and s["category"] == "waste" and s["description"]})
        ws.cell(row=row, column=1, value=i)
        ws.cell(row=row, column=2, value=title)
        ws.cell(row=row, column=3, value=_breakpoint(study, eid))
        for j, ci in enumerate(cyc_idx):
            cell = ws.cell(row=row, column=4 + j, value=per_cycle.get(ci))
            if ci in abnormal:
                cell.font = XOUT
            elif per_cycle.get(ci) is not None and per_cycle[ci] == hi and hi != lr:
                cell.font = HIGH
            elif per_cycle.get(ci) is not None and per_cycle[ci] == lr:
                cell.font = LOW
        base = 4 + n_c
        ws.cell(row=row, column=base, value=f"{lr}*" if fallback else lr)
        ws.cell(row=row, column=base + 2, value=hi)
        ws.cell(row=row, column=base + 3, value=(hi - lr) if lr is not None and hi is not None else None)
        ws.cell(row=row, column=base + 4, value="; ".join(comments))
        for col in range(1, len(headers) + 1):
            ws.cell(row=row, column=col).border = BOX
    sr = r + len(elements) + 2
    sum_t = _r(sum_t)
    ws.cell(row=sr, column=1, value="Σt =").font = HEAD
    ws.cell(row=sr, column=3, value=sum_t)
    ws.cell(row=sr + 1, column=1, value="T =").font = HEAD
    ws.cell(row=sr + 1, column=3, value=t_lr)
    ws.cell(row=sr + 2, column=1, value="T − Σt =").font = HEAD
    ws.cell(row=sr + 2, column=3, value=(t_lr - sum_t) if t_lr is not None else None)
    ws.cell(row=sr + 2, column=4, value="Allocate to elements in 't adjusted' by judgment.")
    ws.cell(row=sr + 3, column=1, value="* no repeated value; lowest observed used.")
    _legend(ws, sr + 5)
    _widths(ws, [5, 44, 18] + [7] * n_c + [11, 10, 9, 11, 40])

    # --- Work sequence (feeds the standardized work chart)
    ws = wb.create_sheet("Work Sequence")
    r = _header_block(ws, study, f"Work sequence (cycle {study['representative_cycle']})")
    for col, label in enumerate(["Step", "Work element", "Hand", "Category", "Tools", "Parts",
                                 "Quality and safety notes", "Time (s)", "Transcript"], 1):
        cell = ws.cell(row=r, column=col, value=label)
        cell.font, cell.fill, cell.border = HEAD, HEAD_FILL, BOX
    for i, s in enumerate(_cycle_steps(study, study["representative_cycle"]), 1):
        vals = [i, s["title"], s["hand"], CAT_LABEL[s["category"]], ", ".join(s["tools"]),
                ", ".join(s["materials"]), "; ".join(s["key_points"]), _r(s["end"] - s["start"]),
                "\n".join(ln["text"] for ln in s.get("transcript") or [])]
        for col, v in enumerate(vals, 1):
            cell = ws.cell(row=r + i, column=col, value=v)
            cell.border = BOX
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    _widths(ws, [6, 44, 7, 22, 22, 22, 36, 9, 70])

    wb.save(out)


def _element_order(study: dict[str, Any]) -> list[tuple[str, str]]:
    """Elements ordered by the representative cycle, then by first appearance elsewhere."""
    seen: dict[str, str] = {}
    rep = study["representative_cycle"]
    ordered = _cycle_steps(study, rep) + sorted(
        (s for s in study["steps"] if s["cycle"] != rep), key=lambda s: (s["cycle"], s["start"]))
    for s in ordered:
        seen.setdefault(s["element_id"], s["title"])
    return list(seen.items())


def _breakpoint(study: dict[str, Any], eid: str) -> str:
    rep = study["representative_cycle"]
    cands = [s for s in study["steps"] if s["element_id"] == eid and s.get("breakpoint")]
    cands.sort(key=lambda s: (s["cycle"] != rep, s["cycle"]))
    return cands[0]["breakpoint"] if cands else ""


def _clock(t: float) -> str:
    m, s = divmod(int(round(t)), 60)
    return f"{m}:{s:02d}"


def _legend(ws, row: int) -> None:
    ws.cell(row=row, column=1, value="Legend").font = HEAD
    ws.cell(row=row + 1, column=1, value="strikethrough = X, cross out time error").font = XOUT
    ws.cell(row=row + 2, column=1, value="bold red = circled highest number").font = HIGH
    ws.cell(row=row + 3, column=1, value="underline = lowest repeatable").font = LOW


def _widths(ws, widths: list[int]) -> None:
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


# ---------------------------------------------------------------- step-wise breakdown

BREAKDOWN_COLUMNS = ["Step #", "Step description", "Location", "Hand", "Time (s)",
                     "Value-add / Non-value-add", "Transcript"]
HAND_LABEL = {"L": "Left", "R": "Right", "both": "Both"}


def breakdown_rows(study: dict[str, Any]) -> list[list[Any]]:
    """Instruction cycle only, numbered exactly like the work chart."""
    from .chart import chart_input, instruction_steps
    from .observation import elem_time

    ci = chart_input(study)
    labels = {loc["id"]: loc["label"] for loc in ci.get("layout") or []}
    if ci is not study:  # template study: one row per template element, numbered like the chart
        rep = study["representative_cycle"]
        by_el = {}
        for s in study["steps"]:
            if s["cycle"] == rep:
                by_el.setdefault(s["element_id"], []).append(s)
        rows = []
        for n, e in enumerate(ci["steps"], 1):
            ss = by_el.get(e["id"], [])
            transcript = "\n".join(f"+{max(0.0, ln['t'] - s['start']):.1f} s  {ln['text']}"
                                   for s in ss for ln in s.get("transcript") or [])
            hand = ss[0]["hand"] if ss else ""
            desc = next((s["description"] for s in ss if s.get("description")), "")
            rows.append([n, e["title"] + (f"\n{desc}" if desc else ""), labels.get(e["location_id"], ""), HAND_LABEL.get(hand, hand),
                         round(sum(elem_time(s) for s in ss), 1) if ss else None,
                         "Value-add" if e["category"] == "value_add" else "Non-value-add", transcript])
        return rows
    rows = []
    for n, s in enumerate(instruction_steps(study), 1):
        transcript = "\n".join(f"+{max(0.0, ln['t'] - s['start']):.1f} s  {ln['text']}"
                               for ln in s.get("transcript") or [])
        rows.append([n, s["title"], labels.get(s.get("location_id") or "", ""),
                     HAND_LABEL.get(s["hand"], s["hand"]), round(s["end"] - s["start"], 1),
                     "Value-add" if s["category"] == "value_add" else "Non-value-add", transcript])
    return rows


def build_breakdown_xlsx(study: dict[str, Any], out: Path) -> None:
    wb = Workbook()
    add_breakdown_sheet(wb, study, wb.active)
    wb.save(out)


def build_observation_xlsx(study: dict[str, Any], out: Path) -> None:
    """Manual Time Observation Sheet first, step-wise breakdown second."""
    from . import observation

    if not breakdown_rows(study):
        raise ExportError("The instruction cycle has no steps.")
    wb = Workbook()
    observation.add_sheet(wb, study)                      # uses the active sheet
    observation.add_prep_sheet(wb, study, wb.create_sheet(index=0))
    add_breakdown_sheet(wb, study, wb.create_sheet())
    wb.active = 0
    wb.save(out)


def add_breakdown_sheet(wb: Workbook, study: dict[str, Any], ws) -> None:
    rows = breakdown_rows(study)
    if not rows:
        raise ExportError("The instruction cycle has no steps.")
    ws.title = "Step breakdown"
    r = _header_block(ws, study, f"Step-wise breakdown (cycle {study['representative_cycle']})")
    for col, label in enumerate(BREAKDOWN_COLUMNS, 1):
        cell = ws.cell(row=r, column=col, value=label)
        cell.font, cell.fill, cell.border = HEAD, HEAD_FILL, BOX
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for i, vals in enumerate(rows, 1):
        for col, v in enumerate(vals, 1):
            cell = ws.cell(row=r + i, column=col, value=v)
            cell.border = BOX
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(row=r + i, column=5).number_format = "0.0"
    total = r + len(rows) + 1
    ws.cell(row=total, column=4, value="Total").font = HEAD
    ws.cell(row=total, column=5, value=f"=SUM(E{r + 1}:E{r + len(rows)})").number_format = "0.0"
    ws.freeze_panes = ws.cell(row=r + 1, column=1)
    _widths(ws, [7, 46, 30, 8, 9, 16, 70])
