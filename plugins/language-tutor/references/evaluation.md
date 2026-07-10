# Evaluation — section gates & placement

Decides progression. Two modes: **section gate** (after a section's lessons are Done) and **placement** (during setup for non-beginners).

## Section gate

1. **Build the test** from the section's Objectives, its lessons' Grammar Points, and its vocabulary — plus ~20% spiral items from earlier sections. 12–20 items across formats: multiple choice, fill-in-the-blank, translation in **both** directions, and 2–3 free-production sentences. Everything phrased in the instruction language.
2. **Deliver** via `assets/quiz.html`: CONFIG `payload.items` = [{id, type, prompt, options?, answer?, rubric?}]. MCQ and exact fill-ins grade locally in JS; free-form answers go to the artifact's Claude call, which returns JSON {score, feedback} — feedback written in the instruction language. Chat-mode fallback if the learner prefers.
3. **Score** = weighted percentage (free production counts double). Thresholds:
   - **≥ 80** — pass. Section Status → Done, Eval Score saved, Current Section → next section (Status likely Skeleton → hand to `planning.md § Level 2`).
   - **60–79** — pass with debt. Advance as above, but log the weak points; the planner must open the next section with a consolidation lesson.
   - **< 60** — not yet. Section stays In Progress; pick the 1–2 weakest lessons to re-run (fresh examples, don't repeat verbatim); re-test after.
4. **Log:** Type Evaluation, Summary = score + per-skill breakdown, Next Step per the outcome. Weak points go here — the section planner reads this entry.

## Placement (setup mode)

Requires the skeleton to exist (runs right after `planning.md § Level 1` writes it). Adaptive, short, low-stakes — say so to the learner. 10–15 items sweeping difficulty: start ~2 sections in, step up on correct, down on wrong; stop when it oscillates. Mix recognition and production. Output: set the course's Current Section to the last section where they were *solid* (not the last they survived) and write a Level string to the Course row ("A1+", "TOPIK 1"…). No Eval Score is written; log as Type Evaluation with the placement rationale.

Grade generously on typos, strictly on grammar. Always end with two sentences on what the result means — never just a number.
