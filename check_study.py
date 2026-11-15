"""Check a finished study against its station template.

    python check_study.py            # newest study
    python check_study.py <study id>

Prints the station layout (to compare with the real station), every template element with its
place and times per cycle, the cycles, and the checks: every element has a valid place, chart
numbers = breakdown numbers, no overlaps on the chart, every element read in every cycle.
"""
import os
import sys
from pathlib import Path

env = Path(__file__).with_name(".env")
if env.exists():  # same data folder as the app
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("WORKSTUDY_DATA=") and "WORKSTUDY_DATA" not in os.environ:
            os.environ["WORKSTUDY_DATA"] = line.split("=", 1)[1].strip()

from app import chart, export, observation, store  # noqa: E402


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows console: allow Σ
    except Exception:
        pass
    rows = store.list_studies()
    sid = sys.argv[1] if len(sys.argv) > 1 else next((r["id"] for r in rows if r["status"] == "ready"), None)
    if not sid:
        print("No finished study found.")
        return 1
    study = store.load(sid)
    if study.get("status") != "ready":
        print(f"Study {sid} is {study.get('status')}: {study.get('error') or study.get('progress')}")
        return 1
    ci = chart.chart_input(study)
    layout = ci.get("layout") or []
    labels = {loc["id"]: loc["label"] for loc in layout}
    h = study.get("header", {})
    print(f"Study #{study.get('number')}  {h.get('line', '')}  ({sid})")
    print(f"Mode: {study.get('analysis_mode', '?')}   station template: {study.get('station_key') or '-'}"
          f"{'' if ci is not study else '  (no template used)'}\n")

    print("STATION LAYOUT (operator's view, facing the bench)")
    for loc in layout:
        print(f"  {loc['id']:4} {loc['label'][:36]:36} {loc['type']:9} {loc['column']:9} {loc['row']:6} "
              f"{str(loc.get('shelf') or '-'):6} x={loc.get('x', '-')} y={loc.get('y', '-')}")

    d = observation.sheet_data(study)
    print(f"\nCYCLES (T = {d['T']}, Σt = {d['sum_t']}, T - Σt = {d['t_minus']})")
    comments = observation.cycle_comments(study, d)
    for c in sorted(study["cycles"], key=lambda c: c["index"]):
        print(f"  {c['index']}{' X' if c['abnormal'] else '  '} {observation._mmss(c['start'])}-{observation._mmss(c['end'])} "
              f"{d['totals'].get(c['index'], observation.cycle_total(c))} s   {c.get('note') or comments.get(c['index'], '')}")

    steps = chart.instruction_steps(ci)
    print(f"\nELEMENTS (times per cycle {d['cycles']})")
    by_eid = {r["eid"]: r for r in d["rows"]}
    for n, s in enumerate(steps, 1):
        r = by_eid.get(s["id"]) or {}
        per = " ".join(f"{(r.get('per') or {}).get(i, '-')!s:>4}{'?' if i in (r.get('unsure') or set()) else ' '}" for i in d["cycles"])
        loc = s.get("location_id") or ""
        print(f"  {n:3} {s['title'][:30]:30} {labels.get(loc, 'NO PLACE')[:22]:22} | {per} | low {r.get('lr', '-')}")

    res = chart.check(study)
    chart_nums = [n for n, _ in res["numbers"]]
    bd = export.breakdown_rows(study)
    missing_loc = [n for n, s in enumerate(steps, 1) if s.get("location_id") not in labels]
    unread = [(r["title"], c) for r in d["rows"] for c in d["cycles"] if c not in r["per"]]
    checks = [
        ("Every element has a valid place", not missing_loc, f"missing on {missing_loc}"),
        ("Chart step numbers = breakdown step numbers", chart_nums == [r[0] for r in bd]
         and [s["title"] for s in steps] == [r[1] for r in bd], f"chart {chart_nums} vs breakdown {[r[0] for r in bd]}"),
        ("No overlapping numbers or shapes on the chart", not res["issues"], "; ".join(res["issues"])),
        ("Every element read in every cycle (no gaps)", not unread, f"{len(unread)} missing readings, e.g. {unread[:3]}"),
    ]
    print("\nCHECKS")
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{'' if ok else '  -> ' + detail}")
    if study.get("warnings"):
        print("\nNOTES FROM ANALYSIS")
        for w in study["warnings"][:15]:
            print(f"  - {w}")
    if study.get("notes"):
        print(f"\nMODEL NOTES\n  {study['notes']}")
    return 0 if all(ok for _, ok, _ in checks) else 2


if __name__ == "__main__":
    sys.exit(main())
