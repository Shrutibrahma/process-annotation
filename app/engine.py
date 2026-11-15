"""Video analysis engines.

Every engine returns an EngineResult (cycles + steps with start times). normalize() turns that
into consistent, non-overlapping steps; the UI and exports only ever see normalized data.
Swap engines with WORKSTUDY_ENGINE=gemini|mock. A local CV engine can be added behind the
same interface without touching the UI or exports.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from pydantic import BaseModel, Field, ValidationError, field_validator

log = logging.getLogger("workstudy.engine")
ProgressFn = Callable[[str], None]

CATEGORIES = ("value_add", "necessary_nva", "waste")
MIN_STEP_S = 0.2
MIN_CYCLE_S = 1.0
# Approximate tokens per video frame at default resolution (Google's figure for an image is 258).
TOKENS_PER_FRAME = 258


class EngineError(RuntimeError):
    pass


def _first_number(v: Any) -> Any:
    """Models sometimes give a time as a range [from, to]; use where it starts."""
    if isinstance(v, (list, tuple)):
        nums = [x for x in v if isinstance(x, (int, float))]
        return nums[0] if nums else None
    return v


class EngineCycle(BaseModel):
    index: int
    start: float
    end: float
    abnormal: bool = False      # suggested X: bad part, rework or equipment failure only
    note: str = ""
    x_reason: str = ""
    x_time: float | None = None

    _xt = field_validator("x_time", mode="before")(lambda cls, v: _first_number(v))


class EngineEvent(BaseModel):
    """Something visible that made an element take longer (coworker helps, fumbles screw...)."""
    cycle: int
    time: float | None = None
    element_id: str = ""
    text: str

    _t = field_validator("time", mode="before")(lambda cls, v: _first_number(v))


class EngineStep(BaseModel):
    element_id: str
    cycle: int
    start: float
    title: str = ""
    breakpoint: str = ""
    description: str = ""
    category: str = "value_add"
    hand: str = "both"
    tools: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    key_points: list[str] = Field(default_factory=list)
    confidence: str = "medium"
    location_id: str = ""     # where the hands go for this step; must be a layout id
    walk: bool = False        # operator walks / steps to reach the location


LOCATION_TYPES = ("bin", "bench", "fixture", "equipment", "tool", "output")
COLUMNS = ("far_left", "left", "center", "right", "far_right")
ROWS = ("back", "middle", "front")
SHELVES = ("top", "middle", "bottom")


class EngineLocation(BaseModel):
    """A place at the station, in categories only (no coordinates), from the operator's viewpoint."""
    id: str
    label: str
    type: str = "bench"
    column: str = "center"
    row: str = "middle"
    shelf: str | None = None
    order: int = 1


class _Layout(BaseModel):
    layout: list[EngineLocation]
    notes: str = ""


class TranscriptLine(BaseModel):
    t: float
    text: str


class _Transcript(BaseModel):
    lines: list[TranscriptLine]


class EngineResult(BaseModel):
    cycles: list[EngineCycle]
    steps: list[EngineStep]
    representative_cycle: int = 1
    notes: str = ""
    layout: list[EngineLocation] = Field(default_factory=list)
    events: list[EngineEvent] = Field(default_factory=list)


class TimingCycle(BaseModel):
    index: int
    start: float
    end: float
    x: bool = False
    x_reason: str = ""
    x_time: float | None = None

    _xt = field_validator("x_time", mode="before")(lambda cls, v: _first_number(v))


class CycleReadings(BaseModel):
    cycle: int
    times: list[float | None]
    uncertain: list[str] = Field(default_factory=list)


class TimingResult(BaseModel):
    """Timing-only pass against a fixed station template."""
    cycles: list[TimingCycle]
    readings: list[CycleReadings]
    events: list[EngineEvent] = Field(default_factory=list)
    representative_cycle: int = 1
    notes: str = ""
    locations: dict[str, str] = Field(default_factory=dict)  # suggestions for elements without a place


class CyclesOnly(BaseModel):
    """Pass 1 of per-cycle timing: cycle boundaries and X for the whole video."""
    cycles: list[TimingCycle]
    representative_cycle: int = 1
    notes: str = ""


class ClipReadings(BaseModel):
    """Pass 2: break point times in one cycle clip, seconds from the clip start, null = not visible."""
    times: list[float | None]
    events: list[EngineEvent] = Field(default_factory=list)
    notes: str = ""


class RefinedTime(BaseModel):
    time: float | None = None
    notes: str = ""

    _t = field_validator("time", mode="before")(lambda cls, v: _first_number(v))


MAX_EXAMPLES = 3


class SheetElement(BaseModel):
    name: str
    breakpoint: str = ""
    times: list[float | None] = Field(default_factory=list)
    category: str = "necessary_nva"
    hand: str = "both"
    breakpoint_visible: bool = True
    issue: str = ""


class SheetResult(BaseModel):
    header: dict[str, Any] = Field(default_factory=dict)
    elements: list[SheetElement]
    notes: str = ""


class Engine(Protocol):
    name: str

    def analyze(self, video: Path, header: dict[str, str], duration: float,
                progress: ProgressFn, layout: list[dict[str, Any]] | None = None) -> EngineResult: ...

    def transcribe(self, video: Path, steps: list[dict[str, Any]],
                   progress: ProgressFn) -> dict[str, list[dict[str, Any]]]: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------- prompt

PROMPT = """You are an industrial engineer creating standardized work from a video of one operator \
performing repeated production cycles at a workstation. The video is __DURATION__ seconds long.
Context from the engineer (may be empty): job "__JOB__", line/operation "__LINE__".

How the video was filmed: a person holding a mobile phone, NOT a fixed overhead camera. The view \
pans, tilts, zooms, shakes and sometimes loses the hands or bins for a moment. Camera movement is \
never a step and never a step boundary; judge everything by what the operator does. When the hands \
are briefly out of view, assume the current action continues unless the next view shows otherwise.
Parts are supplied from bins stacked on up to three levels (top, middle, bottom shelf), several bins \
per level. Identify a bin by level and position, e.g. "top shelf, left bin", "bottom shelf, second bin \
from right", and use the same name every time that bin appears.

Produce step-by-step work instructions AND the timing data for a time study.

1. CYCLES: find every complete production cycle. A cycle starts at the first step's start and ends \
where the next cycle's first step starts.
__XRULE__
Pick the most normal cycle as representative_cycle.

2. STEPS = WORK ELEMENTS: define the elements ONCE from the most normal cycle, then find the same \
elements in every cycle. The same element must use the same element_id in every cycle (E1, E2, ...).
__DETAIL__
A new element starts ONLY when the action changes. One continuous action is ONE element however long \
it lasts: driving a screw for several seconds, repeated turns of a driver, holding a part while it \
sets, or a short pause mid-action is a single element, never repeated as consecutive elements. \
Genuine repeats are different: driving 4 screws, or picking two different parts, are separate \
elements, each with its own element_id (E3, E4, ...).
Title: a short work element name of 2-6 words, like on a time observation sheet, e.g. "Grab housing", \
"Place housing in fixture", "Grab drill", "Drive screw", "Put drill down". If you cannot identify a \
part or tool, write "[unknown part]" or "[unknown tool]" rather than guessing. Put the details in \
description: the part's visible features so an operator picks the right one (colour, shape, size, \
markings, holes or threads), its orientation, which hole, how many.

3. TIMING: for each step in each cycle give its START time in seconds from the start of the video, \
to 0.1 s where possible. Do not give durations; they are computed from consecutive start times.

4. CLASSIFY each step: "value_add" (changes the product: assemble, fasten, insert), \
"necessary_nva" (required but adds no value: grasp, move, reach, inspect, aside), \
"waste" (searching, waiting, re-gripping, rework, unnecessary walking).

5. For each step also give: breakpoint (the observable instant that marks the START of the element, \
in 2-4 words without location ids, e.g. "hand in bin", "hands together", "screw enters part", "R hand moves"), hand ("L", "R" or "both"), tools and materials visible, key points \
(quality or safety detail an operator must get right, only if visible), and confidence \
("high", "medium", "low"). Use "low" whenever the start time or the action is unclear.

7. LOCATION: for each step give location_id, the id of the place in the LOCATIONS list below where \
the hands go for that step (where the part is picked from, worked on, or put down). Use only ids from \
the list. Give walk=true if the operator walks or steps to reach that place, otherwise false.

LOCATIONS (operator's viewpoint, facing the bench):
__LAYOUT__

Report only what is visible. Never invent cycles or steps.
In notes, mention stretches where the camera lost the hands or the bins, with their times.

To keep the answer short: give the full details of each step (title, breakpoint, description, \
category, hand, tools, materials, key_points, walk) ONLY in the representative cycle. In every other \
cycle give only element_id, cycle, start, location_id and confidence; the rest is copied from the \
same element.

Return ONLY JSON in exactly this shape:
{"cycles":[{"index":1,"start":0.0,"end":84.2,"abnormal":false,"x_reason":"","x_time":null},
            {"index":2,"start":84.2,"end":171.0,"abnormal":true,"x_reason":"bad part","x_time":120.5}],
 "events":[{"cycle":1,"time":45.2,"element_id":"E5","text":"struggles to get screw on driver"}],
 "representative_cycle":1,
 "steps":[{"element_id":"E1","cycle":1,"start":0.0,"title":"...","breakpoint":"...","description":"...",
           "category":"value_add","hand":"R","tools":[],"materials":[],"key_points":[],
           "confidence":"high","location_id":"L1","walk":false},
          {"element_id":"E1","cycle":2,"start":84.2,"location_id":"L1","confidence":"high"}],
 "notes":"anything limiting accuracy: camera angle, occlusion, blur"}
"""


LAYOUT_PROMPT = """This video shows one operator working at a manual assembly station. It was filmed \
with a handheld phone, so the camera moves; work out where things are from everything you see.
Context from the engineer (may be empty): job "__JOB__", line/operation "__LINE__".

Map the station as seen FROM THE OPERATOR'S POSITION, standing at the bench and facing it. Never use \
the camera's viewpoint: the operator's left is "left" even when the camera films from the other side.

List every place the operator's hands actually go to during the work: part bins, work surfaces, \
fixtures, equipment (press, machine, tester), tools (where a drill or driver is picked up and put \
back) and output places (tray, cart, conveyor). Leave out anything the operator never uses.

For each place give:
- id: L1, L2, ... in any order. One physical place = one id.
- label: short name an operator would recognise, e.g. "Top shelf bin, black brackets", "Fixture", \
"Drill holder", "Finished parts tray". Put the shelf in the label for stacked bins.
- type: bin | bench | fixture | equipment | tool | output
- column: far_left | left | center | right | far_right   (operator's left to right)
- row: back | middle | front   (front = nearest the operator)
- shelf: top | middle | bottom for bins on stacked shelves, otherwise null
- order: left-to-right rank among places with the same column and row (1, 2, 3...). Bins stacked \
directly above each other share the same column, row and order and differ only in shelf.

Categories only: no coordinates, distances or pixels.

Return ONLY JSON: {"layout":[{"id":"L1","label":"Top shelf bin, black brackets","type":"bin",
"column":"left","row":"back","shelf":"top","order":1}],"notes":"what limited the view"}
"""


TIMING_PROMPT = """You are timing a manual assembly station for a time study. The video is a handheld \
phone recording, __DURATION__ seconds long. Job "__JOB__", line/operation "__LINE__".

The industrial engineer has fixed this station's work elements. Their names, order and break points \
are final (a break point is the visible moment that STARTS the element):
__ELEMENTS__

Your job is ONLY timing. Never add, remove, merge, rename or reorder elements.

1. CYCLES: find every complete cycle. A cycle is ONE finished product: it starts at the break point \
of E1 and ends at the break point of E1 for the NEXT product (the last cycle ends when its last \
element is finished). If the operator repeats elements inside a cycle (a bad part is replaced, a \
part is redone, a dropped part is picked up again), that stays inside the SAME cycle: never start a \
new cycle in the middle of rework. Then give each element's reading for the attempt that produced \
the finished product, and mark the cycle x=true when the X rule below applies. \
Sanity check: every cycle must contain all elements and end with the finished product; a cycle much \
shorter than the others is a sign you split one product in two.
2. READINGS: for EVERY cycle, including cycles you mark X, give the video time in seconds (0.1 s) \
at which each element's break point happens: all __N__ elements, in the listed order, no gaps. If \
the operator does an element differently, still give the moment its break point happens. If a break \
point is hidden (camera off the hands), estimate it from what happens before and after, and put that \
element id in "uncertain" for that cycle.
3. X: __XRULE_SHORT__
4. EVENTS: list anything visible that makes an element take longer than usual: cycle, video time, \
the element it happens in, and a short text ("struggles to get screw on driver", "coworker helps", \
"searches for part", "empties trash", "drops part").
5. representative_cycle: the most normal cycle.
6. LOCATIONS: for each element marked "place: (none)", give the id of the place below where the \
hands go for that element, in "locations". Places (operator's viewpoint):
__PLACES__

Return ONLY JSON:
{"cycles":[{"index":1,"start":0.0,"end":115.0,"x":false,"x_reason":"","x_time":null}],
 "readings":[{"cycle":1,"times":[0.0,2.1,3.4],"uncertain":[]}],
 "events":[{"cycle":1,"time":45.2,"element_id":"E5","text":"struggles to get screw on driver"}],
 "representative_cycle":1,
 "locations":{"E3":"L2"},
 "notes":"what limited accuracy"}
Every "times" list has exactly __N__ numbers, one per element, in order.
"""

CYCLES_PROMPT = """This handheld phone video ({DURATION} s) shows one operator at a manual assembly \
station doing repeated cycles. Job "{JOB}", line/operation "{LINE}". The station's first element is \
"{FIRST}" (break point: {FIRST_BP}) and its last element is "{LAST}".

Find every complete cycle. A cycle is ONE finished product: it starts when the first element's break \
point happens and ends when it happens for the NEXT product (the last cycle ends when its last \
element is finished). If the operator repeats elements inside a cycle (a bad part is replaced, a part \
is redone, a dropped part is picked up again), that stays inside the SAME cycle: never start a new \
cycle in the middle of rework. A cycle much shorter than the others is a sign you split one product \
in two.
__XRULE_SHORT__
Pick the most normal cycle as representative_cycle.

Return ONLY JSON: {"cycles":[{"index":1,"start":0.0,"end":115.0,"x":false,"x_reason":"","x_time":null}],
"representative_cycle":1,"notes":"what limited accuracy"}
"""

CLIP_PROMPT = """This clip is ONE cycle of a manual assembly station, filmed with a handheld phone. It \
covers video time __A__ s to __B__ s (__LEN__ s long). Times you give are seconds FROM THE START OF THIS \
CLIP (0 = the first frame of the clip).

The engineer's fixed work elements, in order (a break point is the visible moment that STARTS the \
element):
__ELEMENTS__

For each element give the time in seconds from the start of this clip at which its break point \
happens, in the listed order: exactly __N__ values. If you cannot see a break point happen (hidden, off \
camera, or the element is not done in this clip), give null. Do not guess. Never add, remove, merge or \
reorder elements; times must increase down the list.
Also list events in this clip that make an element take longer than usual (time from clip start, the \
element id, short text: "struggles to get screw on driver", "coworker helps", "searches for part").

Return ONLY JSON: {"times":[0.4,2.1,null,5.0],"events":[{"cycle":0,"time":12.3,"element_id":"E5",
"text":"struggles to get screw on driver"}],"notes":""}
"""


REFINE_PROMPT = """This __LEN__ s clip is a close-up in time around ONE moment of a manual assembly cycle, \
filmed with a handheld phone. Find the exact moment the break point of element "__NAME__" happens: \
__BP__. Example frames of that moment (from other cycles) may be included above.
Give "time": seconds FROM THE START OF THIS CLIP (0.1 s precision) when it happens, or null if it does \
not happen in this clip. Return ONLY JSON: {"time": 1.4, "notes": ""}
"""


X_RULE_SHORT = ("Set x=true only for a time error that must not count in the baseline: a bad or defective part, rework, a dropped part, an interruption by another person, the operator leaving the station, or a cycle the camera did not fully show. Struggles, periodic work, searching and fixture trouble stay x=false (they are part of the real work); "
                "give x_reason and x_time (ONE number: the video second when it happens) for an X, and list the others as events.")


def build_timing_prompt(header: dict[str, str], duration: float, elements: list[dict[str, Any]],
                        layout: list[dict[str, Any]] | None = None) -> str:
    labels = {l["id"]: l["label"] for l in layout or []}
    lines = "\n".join(f"{e['id']} | {e['name']} | break point: {e.get('breakpoint') or '(first visible movement of this element)'}"
                      f" | place: {labels.get(e.get('location_id') or '', '(none)')}" for e in elements)
    return (TIMING_PROMPT.replace("__DURATION__", f"{duration:.1f}").replace("__JOB__", header.get("job", ""))
            .replace("__LINE__", header.get("line", "")).replace("__ELEMENTS__", lines)
            .replace("__N__", str(len(elements))).replace("__XRULE_SHORT__", X_RULE_SHORT)
            .replace("__PLACES__", layout_text(layout or [])))


SHEET_PROMPT = """This image is a photo of a filled-in paper Manual Time Observation Sheet. It may be \
rotated, handwritten, or have rows joined with braces. Read the table rows in order (the # column).

For each row give:
- name: the Work Element text as written (fix only obvious spelling; keep the operator's wording)
- breakpoint: the Break Point text as written, "" if empty (a brace across rows applies to each row)
- times: the numbers in cycle columns 1 to 10, null where a cell is empty
- category: "value_add" if the element changes the product (drive, insert, fasten, assemble, thread, \
twist into), otherwise "necessary_nva"
- hand: "L", "R" or "both" if the sheet says which hand, otherwise "both"
- breakpoint_visible: true if the break point is something you could see happen on video \
("hand in bin", "hands together", "screw enters part"); false if it is empty or not observable
- issue: why it is not a usable break point, else ""
Also read the header if filled in: job, line, date, prepared_by, takt.
Skip empty rows. Never invent rows.

Return ONLY JSON: {"header":{"job":"","line":""},
 "elements":[{"name":"Grab part","breakpoint":"hand in bin","times":[null,null,null,2],
 "category":"necessary_nva","hand":"both","breakpoint_visible":true,"issue":""}],"notes":""}
"""


def build_layout_prompt(header: dict[str, str]) -> str:
    return LAYOUT_PROMPT.replace("__JOB__", header.get("job", "")).replace("__LINE__", header.get("line", ""))


def layout_text(layout: list[dict[str, Any]]) -> str:
    if not layout:
        return "(none detected: leave location_id empty)"
    return "\n".join(
        f"- {l['id']}: {l['label']} ({l['type']}; {l['column']}, {l['row']}"
        f"{', ' + l['shelf'] + ' shelf' if l.get('shelf') else ''})" for l in layout)


TRANSCRIPT_PROMPT = """This clip is one step of a manual assembly operation, \
from __START__ s to __END__ s of the full video (__LEN__ s long).
Step __N__: "__TITLE__".

Transcribe exactly what the operator does in this clip, in order, as short lines \
of plain language a new operator could follow. One physical action per line: which hand \
(left / right / both), what it reaches for, grasps, moves, positions, inserts, turns, presses \
or releases, and where. Name parts and tools by what they look like if you do not know the \
name (colour, shape, markings). Name bins by shelf level and position ("top shelf, left bin"). \
The video is filmed by hand on a phone: ignore camera movement, and do not repeat a line just \
because the same action continues or the view changed. Include details that matter for doing it right: orientation, how many, which hole, \
until it clicks. Only describe what is visible.

For each line give "t": seconds from the START of this clip when the action begins.

Return ONLY JSON: {"lines":[{"t":0.0,"text":"Right hand reaches into the left bin"}]}
"""


def build_transcript_prompt(step: dict[str, Any], n: int) -> str:
    length = step["end"] - step["start"]
    return (TRANSCRIPT_PROMPT.replace("__START__", f"{step['start']:.1f}")
            .replace("__END__", f"{step['end']:.1f}").replace("__LEN__", f"{length:.1f}")
            .replace("__N__", str(n)).replace("__TITLE__", step["title"]))


def normalize_transcript(lines: list[TranscriptLine], step: dict[str, Any]) -> list[dict[str, Any]]:
    """Return absolute-time lines inside the step. Accepts clip-relative or absolute times."""
    start, end = step["start"], step["end"]
    length = end - start
    ts = [ln.t for ln in lines]
    absolute = bool(ts) and start > length + 1 and all(start - 0.5 <= t <= end + 0.5 for t in ts)
    out = []
    for ln in lines:
        text = ln.text.strip()
        if not text:
            continue
        t = ln.t if absolute else start + ln.t
        out.append({"t": round(min(max(t, start), end), 2), "text": text})
    out.sort(key=lambda x: x["t"])
    return out


STEP_DETAIL = {
    # fine: like a manual time observation sheet, one element per hand action at one place
    "fine": ("Split the work as finely as an industrial engineer does on a manual time observation sheet: "
             "one element per distinct hand action at one place, usually 1-8 seconds. Typical elements: grab "
             "or pick up a part or tool, place or position it, drive / insert / press / turn / fasten, put a "
             "tool down, inspect, transfer the part to the next place. Grabbing at one place and placing at "
             "another are two elements."),
    # coarse: work-instruction steps
    "coarse": "Steps are roughly 1-10 seconds each and may combine a grab with the place it goes.",
}


X_RULE = """Set abnormal=true only for a time error that must not count in the baseline: a bad or defective part, rework, a dropped part, an interruption by another person, the operator leaving the station, or a cycle the camera did not fully show. Struggles, periodic work, searching and fixture trouble stay abnormal=false (they are part of the real work) but get a note. For an X give x_reason \
(what happened) and x_time (video seconds when it happens).
Also list events: anything visible that makes an element take longer than usual, with the cycle, \
video time, the element it happens in and a short text, e.g. "struggles to get screw on driver", \
"coworker helps with fixture", "empties trash", "searches for part"."""


def build_prompt(header: dict[str, str], duration: float,
                 layout: list[dict[str, Any]] | None = None) -> str:
    detail = STEP_DETAIL.get(os.environ.get("WORKSTUDY_STEP_DETAIL", "fine").lower(), STEP_DETAIL["fine"])
    return (PROMPT.replace("__DURATION__", f"{duration:.1f}")
            .replace("__DETAIL__", detail)
            .replace("__XRULE__", X_RULE)
            .replace("__JOB__", header.get("job", ""))
            .replace("__LINE__", header.get("line", ""))
            .replace("__LAYOUT__", layout_text(layout or [])))


def _drop_null_fields(obj: Any) -> Any:
    """{"notes": null} -> {} so field defaults apply; nulls inside lists (times) are kept."""
    if isinstance(obj, dict):
        return {k: _drop_null_fields(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_drop_null_fields(v) for v in obj]
    return obj


def _extract_json(text: str) -> Any:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model output")
    return _drop_null_fields(json.loads(text[start:end + 1]))


# ---------------------------------------------------------------- engines

def _file_state(f: Any) -> str:
    s = getattr(f, "state", None)
    return str(getattr(s, "name", s) or "").upper().replace("FILESTATE.", "")


class GeminiEngine:
    name = "gemini"

    def __init__(self) -> None:
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise EngineError("GEMINI_API_KEY is not set. Add it to your environment or .env file.")
        try:
            from google import genai
            from google.genai import types
        except ImportError as e:
            raise EngineError("google-genai is not installed: pip install google-genai") from e
        self.types = types
        self.client = genai.Client(api_key=key)
        self.model = os.environ.get("WORKSTUDY_GEMINI_MODEL", "gemini-3.8-flash")
        # the passes that decide accuracy (element timing, precision pass, sheet reading) can use a
        # stronger model; finding cycles, layout and transcripts stay on WORKSTUDY_GEMINI_MODEL
        self.timing_model = os.environ.get("WORKSTUDY_TIMING_MODEL") or self.model
        self.refine_resolution = os.environ.get("WORKSTUDY_REFINE_RESOLUTION", "high").lower()
        self.fps = float(os.environ.get("WORKSTUDY_FPS", "5"))
        # static: fixed-fps pass over the whole video (best for timing every step)
        # agentic: model navigates the timeline itself (fps/offsets are ignored)
        self.transcript_fps = float(os.environ.get("WORKSTUDY_TRANSCRIPT_FPS", "10"))
        self.workers = max(1, int(os.environ.get("WORKSTUDY_TRANSCRIPT_WORKERS", "4")))
        self._files: dict[str, Any] = {}
        self.processing = os.environ.get("WORKSTUDY_MEDIA_PROCESSING", "static").upper()
        if self.processing not in ("STATIC", "AGENTIC"):
            raise EngineError("WORKSTUDY_MEDIA_PROCESSING must be 'static' or 'agentic'.")

    def _upload(self, video: Path, progress: ProgressFn) -> Any:
        key = str(video)
        if key in self._files:
            return self._files[key]
        progress("Uploading video to Gemini")
        f = self.client.files.upload(file=key)
        self._files[key] = f
        deadline = time.monotonic() + 15 * 60
        while _file_state(f) == "PROCESSING":
            if time.monotonic() > deadline:
                raise EngineError("Gemini took longer than 15 minutes to process the video.")
            progress("Gemini is processing the video")
            time.sleep(3)
            f = self.client.files.get(name=f.name)
        if _file_state(f) != "ACTIVE":
            raise EngineError(f"Gemini could not process the video (state {_file_state(f)}).")
        self._files[key] = f
        return f

    def _json_call(self, contents: list[Any], model_cls: type[BaseModel], what: str,
                   progress: ProgressFn | None = None, model: str | None = None,
                   resolution: str | None = None) -> Any:
        model = model or self.model
        max_out = int(os.environ.get("WORKSTUDY_MAX_OUTPUT_TOKENS", "65536"))
        res = {"high": self.types.MediaResolution.MEDIA_RESOLUTION_HIGH,
               "medium": self.types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
               "low": self.types.MediaResolution.MEDIA_RESOLUTION_LOW}.get(resolution or "")
        extra = {"media_resolution": res} if res else {}
        config = self.types.GenerateContentConfig(
            response_mime_type="application/json", temperature=0.1, max_output_tokens=max_out, **extra)
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                resp = self.client.models.generate_content(model=model, contents=contents, config=config)
            except Exception as e:
                msg = str(e).lower()
                if "max_output_tokens" in msg or "output token" in msg:  # model refuses the output limit
                    log.warning("model rejected max_output_tokens=%d (%s); using its default", max_out, e)
                    config = self.types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1, **extra)
                elif model != self.model:  # the stronger model failed: this call falls back, the run goes on
                    log.warning("%s failed for %s (%s); using %s", model, what, e, self.model)
                    model = self.model
                else:
                    raise
                resp = self.client.models.generate_content(model=model, contents=contents, config=config)
            text = resp.text or ""
            try:
                return model_cls.model_validate(_extract_json(text))
            except (ValueError, ValidationError) as e:
                last_err = e
                finish = ""
                try:
                    finish = str(resp.candidates[0].finish_reason)
                except Exception:
                    pass
                log.warning("invalid %s output (attempt %d, %d chars, finish=%s): %s",
                            what, attempt + 1, len(text), finish, str(e)[:300])
                if progress:
                    progress("Model output was malformed, retrying")
        raise EngineError(f"The model returned malformed {what} results twice: {last_err}")

    def analyze(self, video: Path, header: dict[str, str], duration: float,
                progress: ProgressFn, layout: list[dict[str, Any]] | None = None) -> EngineResult:
        f = self._upload(video, progress)
        fps = self._fit_fps(duration)
        if layout:
            progress("Using the saved layout for this station")
            locs = normalize_layout([EngineLocation.model_validate(l) for l in layout])
            layout_notes = ""
        else:
            # an overview at 1 fps is enough to map the station and keeps this pass cheap
            progress(f"Mapping the station layout with {self.model}")
            res = self._json_call([self._video_part(f, min(1.0, fps)), build_layout_prompt(header) + getattr(self, "layout_rules", "")],
                                  _Layout, "layout", progress)
            locs, layout_notes = normalize_layout(res.layout), res.notes.strip()
        progress(f"Analyzing steps with {self.model} at {fps:g} fps")
        result = self._json_call([self._video_part(f, fps), build_prompt(header, duration, locs) + getattr(self, "rules", "")],
                                 EngineResult, "step", progress)
        result.layout = [EngineLocation.model_validate(l) for l in locs]
        if layout_notes:
            result.notes = f"{result.notes.strip()}\nLayout: {layout_notes}".strip()
        return result

    def _fit_fps(self, duration: float) -> float:
        """Lower fps so the whole video fits the token budget (and the 1M context window)."""
        budget = int(os.environ.get("WORKSTUDY_MAX_VIDEO_TOKENS", "600000"))
        fitted = budget / (TOKENS_PER_FRAME * max(duration, 1.0))
        fps = min(self.fps, fitted)
        if fps < self.fps:
            log.info("fps lowered from %g to %.2f to fit %d tokens for %.0fs of video",
                     self.fps, fps, budget, duration)
        return max(round(fps, 2), 0.25)

    def time_elements(self, video: Path, header: dict[str, str], duration: float,
                      progress: ProgressFn, elements: list[dict[str, Any]],
                      layout: list[dict[str, Any]] | None = None) -> TimingResult:
        """Template mode: only the moments each fixed break point happens, every cycle."""
        f = self._upload(video, progress)
        fps = self._fit_fps(duration)
        progress(f"Timing {len(elements)} template elements with {self.model} at {fps:g} fps")
        prompt = build_timing_prompt(header, duration, elements, layout)
        res = self._json_call([self._video_part(f, fps), prompt], TimingResult, "timing", progress)
        bad = [r.cycle for r in res.readings if len(r.times) != len(elements)]
        if bad:  # one corrective retry: the answer must have a reading for every element
            progress("Some cycles were missing readings, asking again")
            fix = (prompt + f"\nYour previous answer had the wrong number of times for cycles {bad}. "
                   f"Each cycle needs exactly {len(elements)} times.")
            again = self._json_call([self._video_part(f, fps), fix], TimingResult, "timing", progress)
            if sum(len(r.times) == len(elements) for r in again.readings) >= sum(
                    len(r.times) == len(elements) for r in res.readings):
                res = again
        return res

    def time_elements_per_cycle(self, video: Path, header: dict[str, str], duration: float,
                                progress: ProgressFn, elements: list[dict[str, Any]],
                                reference: dict[str, Any] | None = None) -> TimingResult:
        """Pass 1: cycles + X on the whole video. Pass 2: each cycle's clip at a higher frame rate,
        break points only, seconds from the clip start; code converts to video time."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        f = self._upload(video, progress)
        fps = self._fit_fps(duration)
        progress(f"Finding cycles with {self.model} at {fps:g} fps")
        first, last = elements[0], elements[-1]
        p1 = (CYCLES_PROMPT.replace("{DURATION}", f"{duration:.1f}").replace("{JOB}", header.get("job", ""))
              .replace("{LINE}", header.get("line", "")).replace("{FIRST}", first["name"])
              .replace("{FIRST_BP}", first.get("breakpoint") or "first visible movement")
              .replace("{LAST}", last["name"]).replace("__XRULE_SHORT__", X_RULE_SHORT)) + getattr(self, "rules", "")
        cyc = self._json_call([self._video_part(f, fps), p1], CyclesOnly, "cycles", progress)
        lines = "\n".join(f"{e['id']} | {e['name']} | break point: {e.get('breakpoint') or '(first visible movement of this element)'}"
                          + (f" | engineer's note: {e['note']}" if e.get("note") else "") for e in elements)
        clip_fps = float(os.environ.get("WORKSTUDY_CLIP_FPS", "8"))
        budget = int(os.environ.get("WORKSTUDY_CLIP_TOKENS", "250000"))
        t = self.types
        n = len(elements)

        def one(c: TimingCycle) -> tuple[int, ClipReadings, float]:
            a = max(0.0, c.start - 1.0)
            b = min(duration, c.end + 1.0)
            cfps = max(1.0, min(clip_fps, budget / (TOKENS_PER_FRAME * max(b - a, 1.0))))
            part = t.Part(file_data=t.FileData(file_uri=f.uri, mime_type=f.mime_type),
                          video_metadata=t.VideoMetadata(start_offset=f"{a:.2f}s", end_offset=f"{b:.2f}s", fps=cfps))
            prompt = (CLIP_PROMPT.replace("__A__", f"{a:.1f}").replace("__B__", f"{b:.1f}")
                      .replace("__LEN__", f"{b - a:.1f}").replace("__ELEMENTS__", lines).replace("__N__", str(n))
                      + getattr(self, "rules", ""))
            ref_parts = self._reference_parts(elements, reference, exclude_start=c.start)
            res = self._json_call([part, *ref_parts, prompt], ClipReadings, "clip timing", model=self.timing_model)
            if len(res.times) != n:  # one corrective retry
                res2 = self._json_call([part, *ref_parts, prompt + f"\nYour answer had {len(res.times)} times; give exactly {n}."],
                                       ClipReadings, "clip timing", model=self.timing_model)
                if len(res2.times) == n:
                    res = res2
            return c.index, res, a

        readings, events, done = [], [], 0
        progress(f"Timing {n} elements in {len(cyc.cycles)} cycles with {self.timing_model}, one clip at a time")
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futs = [pool.submit(one, c) for c in cyc.cycles]
            for fut in as_completed(futs):
                idx, res, a = fut.result()
                c = next(x for x in cyc.cycles if x.index == idx)
                length = min(duration, c.end + 1.0) - a
                vals = [float(v) for v in res.times if v is not None]
                # asked for clip-relative seconds; accept video-time answers when every value fits the clip window
                absolute = bool(vals) and a > length + 1 and all(a - 1 <= v <= a + length + 1 for v in vals)
                base = 0.0 if absolute else a
                if absolute:
                    log.info("cycle %s: model answered in video time; used as is", idx)
                times = [None if v is None else round(base + float(v), 2) for v in (res.times + [None] * n)[:n]]
                readings.append(CycleReadings(cycle=idx, times=times))
                for ev in res.events:
                    events.append(EngineEvent(cycle=idx, time=None if ev.time is None else round(base + ev.time, 2),
                                              element_id=ev.element_id, text=ev.text))
                done += 1
                progress(f"Timing cycles ({done} of {len(cyc.cycles)})")
        if os.environ.get("WORKSTUDY_REFINE", "1") != "0":
            progress("Precision pass: pinpointing each break point")
            moved = self.refine_readings(f, readings, cyc.cycles, elements, reference, duration, progress)
            log.info("precision pass moved %d break points", moved)
        return TimingResult(cycles=cyc.cycles, readings=readings, events=events,
                            representative_cycle=cyc.representative_cycle, notes=cyc.notes)

    def _reference_parts(self, elements: list[dict[str, Any]], reference: dict[str, Any] | None,
                         exclude_start: float | None = None, only: str | None = None) -> list[Any]:
        """The engineer's marked frames as examples. A clip of a marked cycle never sees its own
        pictures (leave-one-out), so the accuracy check stays honest. only= one element."""
        refs = [r for r in (reference or {}).get("refs", [])
                if exclude_start is None or abs(r["cycle_start"] - exclude_start) > 5]
        if not refs:
            return []
        t = self.types
        rel = refs[0]["rel"]
        els = [e for e in elements if only is None or e["id"] == only]
        parts: list[Any] = []
        if only is None:
            spacing = ", ".join(f"{e['id']} +{rel[e['id']]:.1f} s" for e in elements if rel.get(e["id"]) is not None)
            parts.append(
                f"REFERENCE: the engineer marked {len(refs)} other cycle(s). Each image below is the exact frame "
                "where that element's break point happens. In a reference cycle the break points came at these "
                f"seconds from the cycle start: {spacing}. Find the same moments in the clip; order and rough "
                "spacing are usually similar, but go by what you see in the clip.")
        for e in els:
            imgs = [r["images"][e["id"]] for r in refs if r.get("images", {}).get(e["id"]) and Path(r["images"][e["id"]]).exists()]
            if not imgs:
                continue
            parts.append(f"{e['id']} {e['name']} (break point: {e.get('breakpoint') or 'as shown'}), example(s):")
            for img in imgs[:MAX_EXAMPLES]:
                parts.append(t.Part.from_bytes(data=Path(img).read_bytes(), mime_type="image/jpeg"))
        return parts

    def refine_readings(self, f: Any, readings: list[CycleReadings], cycles: list[TimingCycle],
                        elements: list[dict[str, Any]], reference: dict[str, Any] | None, duration: float,
                        progress: ProgressFn) -> int:
        """Precision pass: each break point gets a 3 s window at a high frame rate, with its example
        frames, and Gemini pinpoints the moment. Kept only if the element order still holds."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        t = self.types
        fps = float(os.environ.get("WORKSTUDY_REFINE_FPS", "10"))
        half = float(os.environ.get("WORKSTUDY_REFINE_WINDOW", "1.5"))
        jobs = []
        for r in readings:
            c = next((c for c in cycles if c.index == r.cycle), None)
            for i, v in enumerate(r.times):
                if v is not None and c is not None:
                    jobs.append((r, i, v, c))
        if not jobs:
            return 0

        def one(job):
            r, i, v, c = job
            e = elements[i]
            a, b = max(0.0, v - half), min(duration, v + half)
            part = t.Part(file_data=t.FileData(file_uri=f.uri, mime_type=f.mime_type),
                          video_metadata=t.VideoMetadata(start_offset=f"{a:.2f}s", end_offset=f"{b:.2f}s", fps=fps))
            prompt = REFINE_PROMPT.replace("__LEN__", f"{b - a:.1f}").replace("__NAME__", e["name"]).replace(
                "__BP__", e.get("breakpoint") or "the first visible movement of this element") + (
                f"\nEngineer's note for this element: {e['note']}" if e.get("note") else "") + getattr(self, "rules", "")
            ex = self._reference_parts(elements, reference, exclude_start=c.start, only=e["id"])
            res = self._json_call([part, *ex, prompt], RefinedTime, "refine", model=self.timing_model,
                                  resolution=self.refine_resolution)
            if res.time is None:
                return r, i, None
            val = float(res.time)
            if a - 0.5 <= val <= b + 0.5 and val > (b - a) + 1:  # answered in video time
                return r, i, round(val, 2)
            return r, i, round(a + val, 2) if 0 <= val <= (b - a) + 0.2 else None

        done, changed = 0, 0
        new = {}
        with ThreadPoolExecutor(max_workers=max(self.workers, 6)) as pool:
            futs = [pool.submit(one, j) for j in jobs]
            for fut in as_completed(futs):
                try:
                    r, i, v = fut.result()
                    if v is not None:
                        new[(r.cycle, i)] = v
                except Exception as ex:  # one failed refinement keeps the coarse reading
                    log.warning("refine failed: %s", ex)
                done += 1
                if done % 10 == 0 or done == len(jobs):
                    progress(f"Precision pass ({done} of {len(jobs)} break points)")
        for r in readings:
            times = list(r.times)
            for i in range(len(times)):
                v = new.get((r.cycle, i))
                if v is None or times[i] is None:
                    continue
                before = next((times[j] for j in range(i - 1, -1, -1) if times[j] is not None), None)
                after = next((times[j] for j in range(i + 1, len(times)) if times[j] is not None), None)
                if (before is None or v > before) and (after is None or v < after):
                    if abs(v - times[i]) >= 0.05:
                        changed += 1
                    times[i] = v
            r.times = times
        return changed

    def read_sheet(self, image: Path, mime: str) -> SheetResult:
        part = self.types.Part.from_bytes(data=image.read_bytes(), mime_type=mime)
        return self._json_call([part, SHEET_PROMPT], SheetResult, "sheet", model=self.timing_model)

    def transcribe(self, video: Path, steps: list[dict[str, Any]],
                   progress: ProgressFn) -> dict[str, list[dict[str, Any]]]:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        f = self._upload(video, progress)
        t = self.types
        out: dict[str, list[dict[str, Any]]] = {}
        done = 0

        def one(n: int, step: dict[str, Any]) -> list[dict[str, Any]]:
            part = t.Part(
                file_data=t.FileData(file_uri=f.uri, mime_type=f.mime_type),
                video_metadata=t.VideoMetadata(start_offset=f"{step['start']:.2f}s",
                                               end_offset=f"{step['end']:.2f}s",
                                               fps=self.transcript_fps))
            res = self._json_call([part, build_transcript_prompt(step, n)], _Transcript, "transcript")
            return normalize_transcript(res.lines, step)

        progress(f"Transcribing {len(steps)} steps")
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futs = {pool.submit(one, n, st): st["id"] for n, st in enumerate(steps, 1)}
            for fut in as_completed(futs):
                sid = futs[fut]
                try:
                    out[sid] = fut.result()
                except Exception as e:  # one failed step must not sink the study
                    log.warning("transcript failed for step %s: %s", sid, e)
                done += 1
                progress(f"Transcribing steps ({done} of {len(steps)})")
        return out

    def close(self) -> None:
        for f in self._files.values():
            try:
                self.client.files.delete(name=f.name)
            except Exception:  # cleanup only; Gemini also expires files on its own
                log.warning("could not delete uploaded file %s", getattr(f, "name", "?"))
        self._files.clear()

    def _video_part(self, f: Any, fps: float) -> Any:
        t = self.types
        file_data = t.FileData(file_uri=f.uri, mime_type=f.mime_type)
        try:
            if self.processing == "AGENTIC":
                return t.Part(file_data=file_data, media_processing=t.MediaProcessing.AGENTIC)
            return t.Part(file_data=file_data, video_metadata=t.VideoMetadata(fps=fps))
        except Exception as e:  # older SDK without these fields: fall back to default 1 fps
            log.warning("video options not supported by this SDK (%s); using defaults. "
                        "Upgrade with: pip install -U google-genai", e)
            return f


class MockEngine:
    """Synthetic steps for testing the UI and exports without a model. Not real analysis."""
    name = "mock"
    TEMPLATE = [
        ("Grasp housing from top shelf bin and place in fixture", "necessary_nva", "R", 0.10, "L1", False),
        ("Grasp drill", "necessary_nva", "R", 0.08, "L5", False),
        ("Drive screws into subassembly", "value_add", "R", 0.25, "L4", False),
        ("Release drill", "necessary_nva", "R", 0.07, "L5", False),
        ("Grasp spring from middle shelf bin", "necessary_nva", "L", 0.08, "L2", False),
        ("Insert spring and close housing", "value_add", "both", 0.22, "L4", False),
        ("Place finished part in output tray", "necessary_nva", "L", 0.20, "L6", True),
    ]
    LAYOUT = [
        EngineLocation(id="L1", label="Top shelf bin, housings", type="bin", column="left", row="back", shelf="top"),
        EngineLocation(id="L2", label="Middle shelf bin, springs", type="bin", column="left", row="back", shelf="middle"),
        EngineLocation(id="L3", label="Bench", type="bench", column="center", row="front"),
        EngineLocation(id="L4", label="Fixture", type="fixture", column="center", row="middle"),
        EngineLocation(id="L5", label="Drill holder", type="tool", column="right", row="middle"),
        EngineLocation(id="L6", label="Finished parts tray", type="output", column="far_right", row="front"),
        EngineLocation(id="L7", label="Press", type="equipment", column="center", row="back"),
    ]

    def analyze(self, video: Path, header: dict[str, str], duration: float,
                progress: ProgressFn, layout: list[dict[str, Any]] | None = None) -> EngineResult:
        progress("Generating demo steps (mock engine, not real analysis)")
        time.sleep(1)
        n = max(1, min(4, int(duration // 20)))
        length = duration / n
        cycles, steps = [], []
        for c in range(n):
            c0 = c * length
            cycles.append(EngineCycle(index=c + 1, start=c0, end=c0 + length, abnormal=(c == 2),
                                      x_reason="bad part (demo)" if c == 2 else "", x_time=c0 + 3 if c == 2 else None))
            t = c0
            for i, (title, cat, hand, frac, loc, walk) in enumerate(self.TEMPLATE):
                steps.append(EngineStep(element_id=f"E{i + 1}", cycle=c + 1, start=t, title=title,
                                        breakpoint="hand starts moving",
                                        category=cat, hand=hand, location_id=loc, walk=walk,
                                        confidence="low" if i == 4 else "high"))
                t += length * frac
        locs = [EngineLocation.model_validate(l) for l in layout] if layout else self.LAYOUT
        return EngineResult(cycles=cycles, steps=steps, representative_cycle=1, layout=locs,
                            notes="Mock engine: synthetic data for testing only.")

    def time_elements(self, video: Path, header: dict[str, str], duration: float,
                      progress: ProgressFn, elements: list[dict[str, Any]],
                      layout: list[dict[str, Any]] | None = None) -> TimingResult:
        progress("Generating demo timings (mock engine, not real analysis)")
        n = max(1, min(4, int(duration // 20)))
        length = duration / n
        cycles, readings, events = [], [], []
        for c in range(n):
            c0 = c * length
            cycles.append(TimingCycle(index=c + 1, start=c0, end=c0 + length, x=(c == 2),
                                      x_reason="bad part" if c == 2 else "", x_time=c0 + 3 if c == 2 else None))
            step = length / max(1, len(elements))
            times = [round(c0 + i * step * (1.3 if (c == 1 and i == 1) else 1.0), 2) for i in range(len(elements))]
            readings.append(CycleReadings(cycle=c + 1, times=times))
        if len(elements) > 1 and len(readings) > 1:
            events.append(EngineEvent(cycle=2, time=readings[1].times[1], element_id=elements[1]["id"],
                                      text="struggles to get part (demo)"))
        return TimingResult(cycles=cycles, readings=readings, events=events, representative_cycle=1,
                            notes="Mock engine: synthetic timings for testing only.")

    def read_sheet(self, image: Path, mime: str) -> SheetResult:
        return SheetResult(elements=[SheetElement(name="Grab part", breakpoint="hand in bin", times=[2]),
                                     SheetElement(name="Place part", breakpoint="hands together", times=[1]),
                                     SheetElement(name="Grab drill", breakpoint="", times=[4],
                                                  breakpoint_visible=False, issue="no break point written")],
                           notes="Mock sheet")

    def transcribe(self, video: Path, steps: list[dict[str, Any]],
                   progress: ProgressFn) -> dict[str, list[dict[str, Any]]]:
        progress("Generating demo transcripts (mock engine)")
        out = {}
        for st in steps:
            d = st["end"] - st["start"]
            out[st["id"]] = [
                {"t": round(st["start"], 2), "text": "Right hand reaches toward the bin"},
                {"t": round(st["start"] + d * 0.4, 2), "text": f"Demo action for: {st['title']}"},
                {"t": round(st["start"] + d * 0.8, 2), "text": "Hand releases and returns"},
            ]
        return out

    def close(self) -> None:
        pass


def get_engine() -> Engine:
    name = os.environ.get("WORKSTUDY_ENGINE", "gemini").lower()
    if name == "mock":
        return MockEngine()
    if name == "gemini":
        return GeminiEngine()
    raise EngineError(f"Unknown WORKSTUDY_ENGINE '{name}'. Use 'gemini' or 'mock'.")


# ---------------------------------------------------------------- normalization

def _norm_hand(h: str) -> str:
    h = (h or "").strip().lower()
    if h in ("l", "left", "left hand"):
        return "L"
    if h in ("r", "right", "right hand"):
        return "R"
    return "both"


def _norm_category(c: str) -> str:
    c = (c or "").strip().lower().replace("-", "_").replace(" ", "_")
    if c in CATEGORIES:
        return c
    if "waste" in c or c in ("nva", "non_value_add"):
        return "waste"
    if "necessary" in c or "nnva" in c:
        return "necessary_nva"
    return "value_add"


def _norm_list(xs: list[str]) -> list[str]:
    return [x.strip() for x in xs if isinstance(x, str) and x.strip()]


_SYN = {
    "column": {"far left": "far_left", "farleft": "far_left", "leftmost": "far_left", "centre": "center",
               "middle": "center", "mid": "center", "far right": "far_right", "farright": "far_right",
               "rightmost": "far_right"},
    "row": {"rear": "back", "far": "back", "top": "back", "center": "middle", "centre": "middle",
            "mid": "middle", "near": "front", "close": "front", "bottom": "front"},
    "shelf": {"upper": "top", "top shelf": "top", "mid": "middle", "middle shelf": "middle",
              "lower": "bottom", "bottom shelf": "bottom"},
    "type": {"tray": "output", "conveyor": "output", "machine": "equipment", "press": "equipment",
             "table": "bench", "workbench": "bench", "jig": "fixture", "container": "bin", "box": "bin"},
}


def _enum(value: Any, allowed: tuple[str, ...], kind: str, default: str | None) -> str | None:
    v = str(value or "").strip().lower().replace("-", " ").replace("_", " ")
    v = _SYN[kind].get(v, v).replace(" ", "_")
    return v if v in allowed else default


def normalize_layout(layout: list[EngineLocation]) -> list[dict[str, Any]]:
    """Clean model categories; unique ids; stacked bins share column/row/order."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for loc in layout:
        lid = re.sub(r"\s+", "", loc.id.strip()) or f"L{len(out) + 1}"
        if lid.lower() in seen:
            continue
        seen.add(lid.lower())
        out.append({
            "id": lid,
            "label": loc.label.strip() or lid,
            "type": _enum(loc.type, LOCATION_TYPES, "type", "bench"),
            "column": _enum(loc.column, COLUMNS, "column", "center"),
            "row": _enum(loc.row, ROWS, "row", "middle"),
            "shelf": _enum(loc.shelf, SHELVES, "shelf", None),
            "order": max(1, int(loc.order or 1)),
        })
    return out


def _tokens(text: str) -> set[str]:
    stop = {"the", "a", "an", "of", "and", "from", "to", "in", "on", "bin", "shelf", "with"}
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in stop}


def resolve_location(raw: str, title: str, layout: list[dict[str, Any]]) -> str:
    """Map a model location reference to a layout id: exact id, exact label, then best word match."""
    if not layout:
        return ""
    r = raw.strip().lower()
    for loc in layout:
        if r and r in (loc["id"].lower(), loc["label"].lower()):
            return loc["id"]
    words = _tokens(raw) or _tokens(title)
    if not words:
        return ""
    best, score = "", 0.0
    for loc in layout:
        lw = _tokens(loc["label"]) | ({loc["shelf"]} if loc.get("shelf") else set())
        if not lw:
            continue
        sc = len(words & lw) / len(words | lw)
        if sc > score:
            best, score = loc["id"], sc
    return best if score >= 0.2 else ""


def ids_to_labels(text: str, layout: list[dict[str, Any]]) -> str:
    """Element names should name the place, not its layout id: "to L10" -> "to lower receiver fixture"."""
    labels = {loc["id"]: loc["label"] for loc in layout}

    def sub(m: re.Match) -> str:
        return labels[m.group(0)][:1].lower() + labels[m.group(0)][1:] if m.group(0) in labels else ""

    out = re.sub(r"\bL\d{1,2}\b", sub, text)
    return re.sub(r" {2,}", " ", out).replace(" ()", "").strip()


def _same_step(a: EngineStep, b: EngineStep) -> bool:
    """The model split one continuous action: consecutive steps with the same element_id.

    Only element_id counts, never similar titles: four screws or two "pick part" rows are
    separate elements with their own ids and stay separate. Merging only consecutive steps of
    the same element within a cycle never changes the Time Observation sheet, which already
    totals an element's time per cycle, so the columns stay aligned across cycles."""
    return bool(a.element_id.strip()) and a.element_id.strip() == b.element_id.strip()


DETAIL_FIELDS = ("title", "breakpoint", "description", "category", "hand", "tools", "materials",
                 "key_points", "walk")


def _fill_from_element(result: EngineResult) -> None:
    """Steps outside the representative cycle carry only timing; copy the details of the same element."""
    ref: dict[str, EngineStep] = {}
    order = sorted(result.steps, key=lambda st: (st.cycle != result.representative_cycle, st.cycle, st.start))
    for st in order:
        if st.title.strip():
            ref.setdefault(st.element_id.strip(), st)
    for st in result.steps:
        src = ref.get(st.element_id.strip())
        if st.title.strip() or src is None:
            continue
        for f in DETAIL_FIELDS:
            setattr(st, f, getattr(src, f))
        if not st.location_id:
            st.location_id = src.location_id


def normalize(result: EngineResult, duration: float) -> dict[str, Any]:
    """Clamp, sort and de-overlap engine output. Step end = next step start (or cycle end)."""
    warnings: list[str] = []

    def clamp(t: float) -> float:
        return max(0.0, min(float(t), duration))

    cycles: list[dict[str, Any]] = []
    for c in sorted(result.cycles, key=lambda c: c.start):
        s, e = clamp(c.start), clamp(c.end)
        if cycles and s < cycles[-1]["end"]:
            s = cycles[-1]["end"]
        if e - s < MIN_CYCLE_S:
            warnings.append(f"Dropped cycle {c.index}: shorter than {MIN_CYCLE_S}s or overlapping.")
            continue
        cycles.append({"index": len(cycles) + 1, "_src": c.index, "start": round(s, 2),
                       "end": round(e, 2), "abnormal": False, "x_suggested": c.abnormal, "note": "",
                       "x_reason": c.x_reason.strip() if c.abnormal else "",
                       "x_time": c.x_time if c.abnormal else None,
                       "events": [{"time": ev.time, "element_id": ev.element_id.strip(), "text": ev.text.strip()}
                                  for ev in result.events if ev.cycle == c.index and ev.text.strip()]})
    if not cycles:
        raise EngineError("No complete cycles were detected. Check that the video shows at least "
                          "one full cycle with the operator's hands in view.")

    layout = normalize_layout(result.layout)
    _fill_from_element(result)
    steps: list[dict[str, Any]] = []
    n = 0
    for c in cycles:
        raw = sorted((st for st in result.steps if st.cycle == c["_src"]), key=lambda st: st.start)
        kept: list[tuple[float, EngineStep]] = []
        prev_kept = False  # was the step right before this one (in time, same cycle) kept?
        for st in raw:
            t = clamp(st.start)
            if t < c["start"] - 1.0 or t >= c["end"]:
                warnings.append(f"Dropped step '{st.title}' at {t:.1f}s: outside cycle {c['index']}.")
                continue
            t = max(t, c["start"])
            if kept and t - kept[-1][0] < MIN_STEP_S:
                warnings.append(f"Dropped step '{st.title}' at {t:.1f}s: overlaps previous step.")
                prev_kept = False
                continue
            if kept and prev_kept and _same_step(kept[-1][1], st):
                # the same action continuing: the earlier step runs longer instead of repeating
                warnings.append(f"Merged repeated '{st.title}' at {t:.1f}s into the step before.")
                continue
            kept.append((t, st))
            prev_kept = True
        if not kept:
            warnings.append(f"Cycle {c['index']} has no steps.")
            continue
        if kept[0][0] > c["start"]:
            kept[0] = (c["start"], kept[0][1])
        for i, (t, st) in enumerate(kept):
            end = kept[i + 1][0] if i + 1 < len(kept) else c["end"]
            loc = resolve_location(st.location_id, st.title, layout)
            if layout and not loc:
                warnings.append(f"Step '{st.title}' (cycle {c['index']}): location "
                                f"'{st.location_id}' is not in the layout; assign it on the chart.")
            elif loc and loc != st.location_id.strip():
                warnings.append(f"Step '{st.title}': location '{st.location_id}' matched to {loc}.")
            n += 1
            steps.append({
                "id": f"s{n}",
                "element_id": st.element_id.strip() or f"E{n}",
                "cycle": c["index"],
                "start": round(t, 2),
                "end": round(end, 2),
                "title": ids_to_labels(st.title, layout) or "[untitled step]",
                "breakpoint": re.sub(r"\s*\(?\bL\d{1,2}\b\)?", "", st.breakpoint).strip(),
                "description": st.description.strip(),
                "category": _norm_category(st.category),
                "hand": _norm_hand(st.hand),
                "tools": _norm_list(st.tools),
                "materials": _norm_list(st.materials),
                "key_points": _norm_list(st.key_points),
                "transcript": [],
                "location_id": loc,
                "walk": bool(st.walk),
                "confidence": st.confidence if st.confidence in ("high", "medium", "low") else "medium",
                "edited": False,
            })

    src_to_new = {c["_src"]: c["index"] for c in cycles}
    rep = src_to_new.get(result.representative_cycle)
    if rep is None:
        rep = next((c["index"] for c in cycles if not c["abnormal"]), cycles[0]["index"])
    for c in cycles:
        del c["_src"]
    return {"cycles": cycles, "steps": steps, "representative_cycle": rep, "warnings": warnings,
            "layout": layout}
