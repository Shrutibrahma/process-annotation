# Prompt: is Gemini the best model for this video time study tool?

Copy everything below the line into a research-capable agent (one with web search). It is written to
get a sourced, dated comparison and a test plan, not opinions.

---

You are an independent technical reviewer. Your job is to check whether Google Gemini is the best
choice for a specific production task, and if not, what is. Be skeptical of marketing claims, cite a
source (with date) for every factual claim about a model's capabilities, limits or price, and say
clearly when something is unknown or unverified. Today's date matters: models and prices change
monthly, so only use sources from the last 6 months for prices and limits.

## The task the model must do

A manufacturing engineer films a manual assembly workstation with a **handheld phone** (not a fixed
camera; the angle and distance change). Videos are 1-15 minutes long and contain 5-20 repeated
**cycles** of the same job (one finished product per cycle). The station has a fixed list of
**work elements** (about 20-25 per cycle, e.g. "grab part", "fix part with drill", "insert part"),
each with a **break point**: the visible moment the element starts (e.g. "screw touches part").

The model must, from the video:
1. Find every cycle's start and end (±1 s is acceptable).
2. For every cycle, give the time of every element's break point, to **0.1 s resolution**; the goal
   is within ±0.5 s of a trained engineer with a stopwatch. Elements not visible must be reported as
   unknown, not guessed.
3. Flag abnormal cycles (rework, dropped or bad part, interruption by another person, operator
   leaving) with the reason and video time.
4. Describe the station layout (bins, fixtures, tools and where they are relative to the operator)
   and which place each element happens at.
5. Return strict JSON.

Current solution: Gemini 3.x Flash via the Gemini API, uploading the whole video (Files API), with
per-cycle clips at 8 fps (video offsets), a 10 fps precision pass of 3 s around each break point, and
reference still images of each break point from cycles the engineer marked by hand. Measured
accuracy so far: cycles within ±1-3 s; clear element break points about ±0.5 s; ambiguous or
look-alike elements ("pick part" from neighbouring bins) often seconds off. Cost about $1-1.50 per
11-minute video. A free local fallback aligns each cycle to the marked cycles with DINOv2 features
and dynamic time warping; it works where the camera is steady and fails when the angle changes.

## What to evaluate

Compare at least these, and add any strong option you find:
- Google Gemini (current Pro and Flash models) - video input, fps control, max length, offsets.
- OpenAI (current GPT models with video or frame input) - native video or frames only?
- Anthropic Claude (current models) - frames only? limits on images per request?
- Twelve Labs (Pegasus, Marengo) - video-native, temporal grounding.
- Amazon (Nova models; Rekognition), Microsoft Azure (Video Indexer, GPT on Azure), Google Cloud
  Video Intelligence.
- Open models that can run on our own server or a rented GPU: Qwen2.5/3-VL, InternVL, VideoLLaMA,
  NVIDIA Cosmos Reason, LLaVA-Video, and temporal action segmentation / localization models
  (e.g. ActionFormer, ASFormer, MS-TCN) that could be trained on a few marked cycles.
- Dedicated industrial time-study products: Retrocausal (Kaizen Copilot), Drishti, MTM's AI video
  analysis, Timer Pro, AviX, Siemens/others. Do they work on handheld phone video, with a
  customer's own element list, and export element times? What do they cost?

For each, report:
| Criterion | What to find |
|---|---|
| Native video input | yes / frames only; max duration; fps control; clip offsets |
| Temporal precision | any published benchmark on temporal grounding / moment localization (e.g. Charades-STA, ActivityNet Captions, QVHighlights, Ego4D NLQ) and the number |
| Few-shot with reference images | can it take example frames alongside the video in one request? |
| Structured output | strict JSON / schema support |
| Price per minute of video | at the fps we need (5-10 fps), with the calculation shown |
| Data privacy | is uploaded video used for training? retention? enterprise / zero-retention terms? |
| On-premises option | can it run without sending video out of the factory? hardware needed? |
| Rate limits / reliability | for 20-50 videos a month |

## Deliverables

1. A comparison table (the criteria above) with a citation and date in every cell, or "unknown".
2. A ranked recommendation for THIS task, with the main reason and the main risk for each of the
   top three. Say plainly if Gemini is still the best choice.
3. A **bake-off test plan** we can run ourselves: use 2-3 cycles that the engineer marked by hand as
   ground truth; send the same 11-minute video and the same prompt and element list to the top 3
   candidates; score cycle-boundary error, break-point error (mean absolute error, % within 0.5 s and
   1 s), share of "unknown" answers, cost and time per video. Include the exact metrics and a
   pass/fail bar (e.g. 80% of break points within 0.5 s).
4. What would most improve accuracy regardless of model (camera setup, frame rate, break point
   definitions, a trained segmentation model), ranked by expected gain per effort.

Do not recommend anything you cannot back with a source. Flag every number that comes from a
vendor's own marketing rather than an independent benchmark.
