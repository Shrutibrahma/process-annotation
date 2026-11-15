"""Kaizen Copilot: questions, improvement ideas and analyses over a finished study.

Works from the study data (steps, times across cycles, transcripts), not the video, so answers
are fast and cheap. Ergonomics, P-FMEA and MODAPTS answers are screenings from what the steps
describe; the prompts make the model say so.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .engine import EngineError, _extract_json
from .export import lowest_repeatable

log = logging.getLogger("workstudy.copilot")

SYSTEM = """You help a worker learn a hands-on task from a video lesson. Many of the people you \
help are Deaf, hard of hearing or do not speak, and English may not be their first language. You \
answer from the lesson data below: the steps in order, which hand does what, where each part and \
tool is, and line-by-line descriptions of what the hands do in the video.

Rules:
- Use only this lesson. If the video does not show something, say so plainly and suggest asking a \
person, instead of guessing.
- Plain language: short sentences, common words, one idea per sentence. Start steps with a verb.
- Never rely on sound. Describe what to SEE and FEEL ("the screw sits flat", "the latch clicks into \
place and stops moving"), not what to hear.
- Refer to steps by number and name, e.g. "Step 3 (Drive the screw)". Say which hand when it matters.
- Use markdown: numbered lists for steps, short bullet lists otherwise. Avoid long paragraphs and jargon.
"""

KAIZEN_PROMPT = """Find improvement opportunities in this station. Look at: non-value-add and waste \
steps, long reaches or bending (top and bottom shelf bins), searching, re-gripping, waiting, \
one-handed work that could be two-handed, walking, and elements with high fluctuation across cycles.

Return 3 to 8 ideas, biggest saving first, as ONLY JSON:
{"ideas":[{"title":"short imperative, e.g. Move screw bin to middle shelf",
 "waste":"Motion|Transport|Waiting|Defects|Overprocessing|Inventory|Overproduction|Skills",
 "steps":[3,4],"saving_s":1.5,"effort":"low|medium|high",
 "detail":"what to change and why, 1-3 sentences, citing the observed times"}]}
saving_s is your estimate of seconds saved per cycle; use 0 if it is a quality or safety idea."""


class Idea(BaseModel):
    title: str
    waste: str = ""
    steps: list[int] = Field(default_factory=list)
    saving_s: float = 0.0
    effort: str = "medium"
    detail: str = ""


class _Ideas(BaseModel):
    ideas: list[Idea]


# ---------------------------------------------------------------- study context

def _cycle_steps(study: dict[str, Any], cycle: int) -> list[dict[str, Any]]:
    return sorted((s for s in study["steps"] if s["cycle"] == cycle), key=lambda s: s["start"])


def context(study: dict[str, Any]) -> str:
    h = study.get("header", {})
    rep = study["representative_cycle"]
    cycles = study["cycles"]
    lines = ["# Study",
             *(f"- {k}: {v}" for k, v in h.items() if v),
             f"- Observed cycles: {len(cycles)}; representative cycle: {rep}", "", "## Cycles"]
    for c in cycles:
        flag = f" EXCLUDED ({c.get('note') or 'abnormal'})" if c["abnormal"] else ""
        lines.append(f"- Cycle {c['index']}: {c['end'] - c['start']:.1f} s{flag}")

    # element times across cycles, like the Manual Time Observation sheet
    normal = [c["index"] for c in cycles if not c["abnormal"]]
    order: dict[str, str] = {}
    for s in _cycle_steps(study, rep) + sorted(study["steps"], key=lambda s: (s["cycle"], s["start"])):
        order.setdefault(s["element_id"], s["title"])
    lines += ["", "## Element times (s) across normal cycles",
              "| Element | Title | Times | Lowest repeatable | Highest |", "|---|---|---|---|---|"]
    for eid, title in order.items():
        per = []
        for ci in normal:
            t = sum(s["end"] - s["start"] for s in study["steps"] if s["cycle"] == ci and s["element_id"] == eid)
            if t:
                per.append(round(t, 1))
        lr, _ = lowest_repeatable(per)
        lines.append(f"| {eid} | {title} | {', '.join(map(str, per))} | {lr if lr is not None else ''} | "
                     f"{max(per) if per else ''} |")

    lines += ["", f"## Steps of representative cycle {rep}"]
    for i, s in enumerate(_cycle_steps(study, rep), 1):
        lines.append(f"\n### Step {i} ({s['element_id']}): {s['title']}")
        lines.append(f"- {s['end'] - s['start']:.1f} s, {s['category']}, hand {s['hand']}")
        for key in ("materials", "tools", "key_points"):
            if s.get(key):
                lines.append(f"- {key}: {'; '.join(s[key])}")
        if s.get("description"):
            lines.append(f"- notes: {s['description']}")
        for ln in s.get("transcript") or []:
            lines.append(f"  - +{ln['t'] - s['start']:.1f} s {ln['text']}")
    if study.get("notes"):
        lines += ["", f"## Analysis notes\n{study['notes']}"]
    return "\n".join(lines)


# ---------------------------------------------------------------- model calls

class _Gemini:
    def __init__(self) -> None:
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise EngineError("GEMINI_API_KEY is not set. Add it to your .env file.")
        try:
            from google import genai
            from google.genai import types
        except ImportError as e:
            raise EngineError("google-genai is not installed: pip install google-genai") from e
        self.types = types
        self.client = genai.Client(api_key=key)
        self.model = (os.environ.get("WORKSTUDY_COPILOT_MODEL")
                      or os.environ.get("WORKSTUDY_GEMINI_MODEL", "gemini-3.8-flash"))

    def generate(self, system: str, turns: list[tuple[str, str]], json_mode: bool = False) -> str:
        t = self.types
        contents = [t.Content(role="user" if role == "user" else "model", parts=[t.Part(text=text)])
                    for role, text in turns]
        config = t.GenerateContentConfig(
            system_instruction=system, temperature=0.3,
            response_mime_type="application/json" if json_mode else None)
        resp = self.client.models.generate_content(model=self.model, contents=contents, config=config)
        return (resp.text or "").strip()


def _mock() -> bool:
    return os.environ.get("WORKSTUDY_ENGINE", "gemini").lower() == "mock"


def chat(study: dict[str, Any], history: list[dict[str, Any]], message: str) -> str:
    if _mock():
        return ("**Mock copilot** (WORKSTUDY_ENGINE=mock): set the engine to gemini for real answers.\n\n"
                f"You asked: _{message[:200]}_")
    system = SYSTEM + "\n\n" + context(study)
    turns = [(m["role"], m["text"]) for m in history[-12:]] + [("user", message)]
    try:
        text = _Gemini().generate(system, turns)
    except EngineError:
        raise
    except Exception as e:
        log.exception("copilot chat failed")
        raise EngineError(f"Copilot request failed: {e}") from e
    if not text:
        raise EngineError("The model returned an empty answer. Try again.")
    return text


def kaizen_ideas(study: dict[str, Any]) -> list[dict[str, Any]]:
    if _mock():
        return [Idea(title="Move the most used bin to the middle shelf", waste="Motion", steps=[1],
                     saving_s=1.0, effort="low", detail="Mock idea for testing the UI.").model_dump()]
    system = SYSTEM + "\n\n" + context(study)
    g = _Gemini()
    last: Exception | None = None
    for _ in range(2):
        try:
            raw = g.generate(system, [("user", KAIZEN_PROMPT)], json_mode=True)
            ideas = _Ideas.model_validate(_extract_json(raw)).ideas
            out = []
            for i in ideas:
                d = i.model_dump()
                d["effort"] = d["effort"].lower() if d["effort"].lower() in ("low", "medium", "high") else "medium"
                d["saving_s"] = max(0.0, round(float(d["saving_s"]), 1))
                d["waste"] = re.sub(r"[^A-Za-z ]", "", d["waste"]).strip().title()
                out.append(d)
            return out
        except (ValueError, ValidationError, json.JSONDecodeError) as e:
            last = e
            log.warning("invalid kaizen output: %s", e)
        except Exception as e:
            log.exception("kaizen request failed")
            raise EngineError(f"Copilot request failed: {e}") from e
    raise EngineError(f"The model returned malformed ideas twice: {last}")
