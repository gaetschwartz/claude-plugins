# Lesson — deliver one lesson

The core teaching session. The plan comes from `lesson-builder.md`; chat is the medium; the lesson-sheet artifact is the visual reference open alongside it.

## Procedure

1. **Load:** fetch the Current Lesson page. **If the body is still a planning stub** (no full plan) — the normal case — **run `lesson-builder.md` now**, then continue. Set Lesson Status → In Progress; its Section → In Progress if not already.
2. **Lesson sheet:** read `assets/lesson-sheet.html`, inject CONFIG (SKILL.md contract) with `payload` = this lesson's content: title, contrastive grammar notes, the glossed example blocks rendered as color-annotated sentences (`<ruby>` for script + romanization per the weaning policy), vocab table. All labels in the instruction language. Publish as an artifact — it's the learner's reference for the session.
3. **Teach in chat**, following the plan's structure but reacting to the learner:
   - **Warm-up:** the plan's recall prompts.
   - **Homework review:** ask for their homework, correct by recasting + one-line explanations. Skip if none was assigned.
   - **Presentation:** grammar contrastively; one comprehension-check question per point before moving on.
   - **Practice:** guided drills, both directions — learner answers before you reveal.
   - **Production:** the plan's task; correct by recasting. Note recurring errors.
   - **Mini-quiz** (5–8 items, in chat) → percentage Score.
   - **Wrap-up:** summarize, assign the plan's homework explicitly.
4. **Optional practice chat:** the lesson sheet embeds a small practice chat scoped by CONFIG to this lesson's grammar + vocab — point the learner to it for extra reps.
5. **Close (one batched write pass):**
   - Lesson: Score; Status → Done if Score ≥ 60, otherwise it stays In Progress.
   - This lesson's vocab rows: Status → Learning, `date:Next Review:start` = tomorrow, Interval 1, Ease 2.5, Reps 0, Lapses 0.
   - Score ≥ 60: advance Current Lesson to the next Planned lesson; if none remain in the section → suggest the section evaluation (`evaluation.md`), leave the pointer.
   - Score < 60: don't advance; Next Step = re-run this lesson with a fresh angle on the weak items (the body is already built — don't rebuild it, vary the delivery).
   - Session Log: Type Lesson, Summary (covered material + recurring errors + homework assigned), Next Step.
6. One-line sign-off: the homework, what's next, and how many vocab cards come due tomorrow.

If the learner runs out of time mid-lesson: jump to Close, keep Status In Progress, log exactly where you stopped in Next Step.
