# Process Annotation · Learn any task, step by step

**For Deaf, hard-of-hearing and non-speaking workers.** Film someone doing a hands-on job once.
Process Annotation splits it into steps, plays each step on a loop at a speed you choose, writes out
what the hands do, shows where each step happens on a map of the workspace, and answers questions
you type. Nothing depends on hearing, speaking or an interpreter.

## Why

Hands-on jobs are usually taught by someone standing next to you and talking: "now grab the screw,
not that one, the short one". For a Deaf or hard-of-hearing worker, or someone who doesn't speak,
that teaching is easy to miss and hard to ask about. Videos help, but a real task is fast, the
important moment lasts half a second, and the spoken explanation, if there is one, isn't in the
picture.

Process Annotation turns one ordinary phone video into a lesson you can follow with your eyes and
your own pace.

## What a learner gets

| View | What it does |
|---|---|
| **Learn step by step** | One step at a time. The step repeats on a loop at 0.75× by default (down to 0.25×). Captions under the picture say what the hands do at that moment, so they never cover the work. Beside it: a plain explanation, which hand, how long it takes, what to watch out for, and the parts and tools you need. |
| **All steps** | The whole task as a table: what to do, where, which hands, seconds, and a line-by-line description. Choose a row to watch that step. |
| **Where it happens** | A top-down map of the workspace. Every step is a number at the spot where it happens, and arrows show where your hands go next. |
| **Ask** | Type a question in your own words ("What do I need before I start?", "Explain step 3"). Answers use plain language, describe what you can see rather than hear, and come only from this lesson. |
| **Station template** | For whoever sets up the station: the steps in your own words and order, where each one starts, which hand, the places on the map, and the workspace layout. Every new video of that station uses it. |

Also: **Larger text** and **High contrast** switches (remembered between visits), full keyboard
control (Space to play or pause, ↑ and ↓ to change step, ← and → to move frame by frame), a
**Step guide to keep** download with a clip of every step, and a printable **Workspace map (PDF)**.

If a step is hard to see in the video, the lesson says so plainly instead of pretending.

## Try it

You need Python 3.11+. ffmpeg comes with `imageio-ffmpeg`, so there's nothing else to install.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows    (Mac/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env          # Mac/Linux: cp .env.example .env; then add your GEMINI_API_KEY
python run.py                   # opens http://127.0.0.1:8000
```

A demo lesson is included: **Build an LED circuit on a breadboard** (18 steps, from wiring the power
rails to lighting three LEDs). It plays without an API key. The demo video is the YouTube tutorial
"How to use a BreadBoard - Electronics Basics 10"; all rights stay with its creator, and it's included
only to show how a lesson works. You need `GEMINI_API_KEY` only to turn a
new video into a lesson, to use **Ask**, or to use **Suggest a fix**.

To see the screens without a key or a real analysis, set `WORKSTUDY_ENGINE=mock`. That makes
synthetic steps, not a real lesson.

## How a video becomes a lesson

All video understanding is done by Gemini. There's no tracking or pixel measurement.

1. **The workspace.** Gemini maps the station from where the worker stands: bins, fixtures, tools
   and benches, by column, row and shelf. Code turns that into the map.
2. **The steps.** Gemini finds each step: when it starts and ends, which hand does it, and where the
   hands go. If the station has a template, the video is split into the template's steps, in your
   wording, so every video of that station reads the same way.
3. **What the hands do.** A line-by-line description of each step ("Right hand presses the
   screwdriver tip into the screw feeder"). These become the captions and the moment-by-moment list.
4. **Plain explanations.** For each step: a short explanation, the parts and tools, and what to
   watch out for.

The lesson is saved, so watching it never calls the model again.

## Getting it right: Station template and Suggest a fix

- **Station template.** Name the steps yourself, set where each one starts ("hand touches the
  part"), put them in order, mark where to check your work or take care, and drag the bins and
  fixtures into place. You can also import a photo of a paper step sheet. Saved per workplace and
  reused for every new video there.
- **Teach the exact step starts (optional).** Play a clean run and mark the frame where each step
  starts. The app then lines up the other runs with yours, frame by frame, on your computer
  (a small DINOv2 model, about 90 MB, downloaded once into `data/models`), and shows how close it
  gets to your marks.
- **Suggest a fix.** Type what's wrong in plain words ("step 4 is done with the left hand", "make
  the descriptions simpler"). You see the exact changes before anything is saved, and lasting fixes
  are remembered for that workplace.

## Settings

Everything is in `.env` (see `.env.example`):

| Setting | What it does |
|---|---|
| `GEMINI_API_KEY` | Needed for new lessons, Ask and Suggest a fix. Not needed to watch saved lessons. |
| `WORKSTUDY_GEMINI_MODEL`, `WORKSTUDY_TIMING_MODEL`, `WORKSTUDY_COPILOT_MODEL` | Which Gemini models do the steps, the timing and the answers. |
| `WORKSTUDY_FPS` | How many frames per second Gemini looks at (lowered automatically for long videos). |
| `WORKSTUDY_AUTH=1` | Turns on accounts and teams (the default in Docker). Off when you run it locally. |
| `WORKSTUDY_DATA` | Where lessons are stored (default `data/`). |

## Hosting

The `Dockerfile` runs anywhere that gives a container a persistent volume at `/data`. `render.yaml`
sets up a Render service with a disk, and `deploy/oracle/` has scripts for a free Oracle Cloud VM.
Keep one instance, because lessons are prepared in the web process. With accounts on, each team's
lessons and templates stay in their own workspace, and `WORKSTUDY_SIGNUP_CODE` controls who can
start a team.

## Privacy

To make a lesson, the video is uploaded to Google's Gemini API, and the uploaded copy is deleted
once the analysis is done. Everything else stays in `data/` on your machine or server. **Ask the
people in the video before filming them at work**, and get sign-off before using footage from a
workplace.

## Known limits

- Step times come from the model. Expect about ±0.5–1 s on very short steps. Teaching the exact step
  starts makes them tighter.
- The map is a schematic built from the places' positions (left/right, back/front, shelf), not a
  measured floor plan. Check it against the real station and drag places to fix it.
- Descriptions say what the camera shows. If a hand hides the part, the lesson can't see it either.
  It marks such steps as hard to see, and **Ask** says when the video doesn't show something.
- Ask answers in English text. It doesn't produce sign language.

## Layout

```
app/main.py            web API and lesson storage
app/engine.py          Gemini passes: workspace, steps, what the hands do
app/chart.py           the workspace map (SVG, PNG, PDF)
app/copilot.py         Ask: plain-language answers from the lesson only
app/feedback.py        Suggest a fix
app/template.py        station templates
app/align.py           optional frame-by-frame step timing from your marks
app/static/index.html  the whole interface
data/                  the demo lesson and its station template
```
