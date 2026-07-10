# Lesson builder — write one lesson's full content

Turns a planned stub into a complete, ready-to-teach lesson page. Runs **just-in-time** — normally invoked at the start of a lesson session (`lesson.md` step 1), or standalone when the learner asks to "prep the next lesson". Building this late is deliberate: the plan absorbs yesterday's errors, the homework that was actually assigned, and lapsing vocabulary.

## Read (beyond boot)

- The target Lesson row + its stub body (objective, grammar points, vocab theme, recycle notes).
- The **previous lesson's page**: what was taught, and the homework it assigned. First lesson of the course → skip this; the warm-up draws on the setup interview instead.
- Recent Session Log entries: recurring errors, weak points.
- Learner profile — interests drive the example sentences. Optionally lapsed words (Lapses ≥ 3) to weave in.

## Write the plan into the lesson page body

In the instruction language. Every lesson page gets these sections, in this order:

1. **Objective** — one learner-facing line.
2. **Warm-up** (~5 min) — 3–4 recall prompts from the previous lesson + recycled weak items, with expected answers.
3. **Homework review** — restate the assignment, model answers, notes on likely errors. Omit the section if none was assigned.
4. **Presentation** — per grammar point: contrastive explanation vs. the learner's languages, then 4–6 examples written as glossed blocks — target sentence · romanization · word-by-word gloss · translation — so the lesson sheet can color-annotate them deterministically.
5. **Vocabulary** — 8–15 items: word · romanization · meaning · example, themed to the lesson and the learner's interests.
6. **Practice** — guided drills *with answer key*: both translation directions, transformations, fill-ins.
7. **Production** — one open-ended task.
8. **Mini-quiz** — 5–8 items + answers.
9. **Wrap-up & homework** — two-line summary + a small assignment for next time (e.g. write 3–5 sentences using today's grammar).

## Also

- **Create the Vocabulary rows for §5 now:** Word, Romanization, Meaning, Example, Course + Lesson relations, Status = New, SRS fields empty. (Delivery activates them at lesson close — reviews never surface untaught words.)
- Lesson Status stays **Planned** — delivery flips it to In Progress.
- **Scope discipline:** use only this stub's grammar/vocab plus already-taught material. Nothing the learner hasn't seen, unless the stub says so.
- Standalone run (no teaching afterwards): log Type Planning, Next Step = "Run lesson: <name>". When invoked inside a lesson session, the lesson's own log entry covers it.
