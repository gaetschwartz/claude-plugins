# Setup — workspace init & new course

Two jobs, both idempotent: (A) make sure the Notion structure exists, (B) create a new course. Run A before B every time; A alone also repairs a broken workspace.

## A. Workspace init (verify-or-create)

1. Notion-search for a page titled **Language Tutor**; fetch it if found.
2. Check which of the five databases exist under it (match by title: Courses, Sections, Lessons, Vocabulary, Session Log). Collect their data-source IDs.
3. Create whatever is missing using the exact DDL in `notion-schema.md`, **in this order** (relations need their target to exist): Courses → Sections → Lessons → Vocabulary → Session Log → then `ADD COLUMN "Current Section" / "Current Lesson"` relations on Courses if absent.
4. If the parent page itself is missing, ask the user where to put it (default: top-level private page), create it with a short description of what each database is for, then create all five databases.
5. Never delete or rename anything that exists. If a database exists with a wrong/missing property, add the property; report anything you can't fix.

## B. New course

1. **Guard:** if an Active course for the same target language exists, ask: resume it, or archive (Status → Completed, note in body) and start fresh. Never silently duplicate.
2. **Interview** the learner — in the language they're writing to you in, but confirm each answer explicitly:
   - Target language
   - **Instruction language** (the language the whole course will be taught in — don't infer it, ask)
   - Other languages they know (fuels contrastive teaching)
   - Current level: true beginner / knows some basics / more (self-assessment only at this point — real placement runs after the skeleton exists, see step 4)
   - Goals: conversation, travel, media, work, an exam (TOPIK/JLPT/HSK/DELE/…)
   - Weekly time budget and preferred session length
   - Interests/hobbies (for personalized examples)
   - For non-Latin scripts: romanization preference and how fast to wean off it
3. **Create the Course row:** Name = target language name written in the instruction language; Target Language; Instruction Language; Level (initial estimate, e.g. "A0" or placement result); Status = Active; `date:Started:start` = today. Page body = learner profile in the instruction language: goals, known languages, interests, script/romanization policy, time budget, anything else notable.
4. **Hand off:** read `planning.md § Level 1` and build the skeleton now if the learner has time. For non-beginners, run `evaluation.md § Placement` right after the skeleton is written — it sets Current Section and Level. Otherwise stop here.
5. **Log:** Session Log entry — Type Setup, Summary of choices made, Next Step = "Build curriculum skeleton" (or the first lesson if the skeleton was built).

Tone note: this is the learner's first contact with the course. Keep the interview light — one or two questions per message, not a form.
