---
name: language-tutor
description: Personal language tutor that teaches any language *in the learner's own language*, with curriculum, lessons, spaced-repetition vocabulary and all progress stored in the learner's Notion workspace. Use this skill whenever the user wants to learn, practice, study, or continue studying a foreign language — "teach me Korean", "continue my Spanish course", "start a new language", "review my vocab", "flashcards", "quiz me in Japanese", "next lesson" — even if they don't mention Notion, courses, or this skill by name.
---

# Language Tutor

A stateful tutor for any language pair. Skills hold the *method*; all *state* lives in the learner's Notion workspace under a page called **Language Tutor** (schema in `references/notion-schema.md`). Every session must start by loading state and end by saving it — a future session in a fresh chat has only Notion to go on.

## Session boot (always do this first)

1. If Notion tools aren't loaded yet, load them (on claude.ai: tool search for "notion").
2. Notion-search for the page "Language Tutor" and fetch it to collect the five database / data-source IDs by title (Courses, Sections, Lessons, Vocabulary, Session Log).
   - Page or databases missing → this is a first run: go to `references/setup.md`.
3. Fetch the relevant Course row **including its page body** (learner profile) — if several Active courses exist and the user didn't name one, ask which.
   - No course rows → `references/setup.md`.
4. Fetch the most recent Session Log entry for that course (its **Next Step** tells you where to resume).

Boot is exactly these reads. Don't crawl the workspace.

## Routing

| Intent | Read |
|---|---|
| New course, first run, "learn X" with no existing course, rebuild workspace | `references/setup.md` |
| Build/revise the skeleton, or current section is `Skeleton` / "plan the next section" | `references/planning.md` |
| "Prep/write my next lesson" — content only, no teaching | `references/lesson-builder.md` |
| "Lesson", "continue", "teach me", default resume action | `references/lesson.md` |
| "Vocab", "flashcards", "review words" | `references/vocab.md` |
| Section finished, "test me", placement | `references/evaluation.md` |

If the Next Step in the log names a module, prefer it. When a lesson session is requested but vocabulary reviews are due, mention the due count and offer a 5-minute review first.

## Shared rules (apply in every module)

**Instruction language.** Everything — explanations, feedback, quiz questions, artifact UI labels, Notion page content — is written in the course's Instruction Language. The target language appears only as the material being studied. Never assume the instruction language; it's on the Course row, and setup asks for it explicitly.

**Contrastive teaching.** Explain every grammar point relative to the languages in the learner profile: what maps directly ("this works like *en* in French"), what differs, what simply doesn't exist in their language (flag these hardest). Reuse the learner's interests (profile) for example sentences.

**Script & romanization.** For non-Latin scripts, show romanization alongside the script early on (in HTML artifacts use `<ruby>` annotations). The profile stores a weaning policy — reduce romanization as the learner progresses, never abruptly.

**Color conventions** (chat and artifacts, always with a legend):
verbs **red** · particles/postpositions **blue** · subjects **green** · objects **orange** · endings/conjugation **purple**.

**Notion discipline.** Use exactly the property names in `references/notion-schema.md`; never rename databases or properties. Boot = only the four reads in the boot sequence (search → page → course row → latest log), nothing more. Batch writes at session close, not one call per change. Dates are written via expanded props (`date:Prop:start`). **Every session ends with a Session Log entry** (Type, Summary, Next Step) — no exceptions, it's the only memory the next chat has.

**Session close.** Update statuses/scores touched → update Current Section / Current Lesson pointers → write the Session Log entry → tell the learner in one line what comes next.

## Artifact contract (assets/)

Three templates ship with this skill: `assets/lesson-sheet.html`, `assets/flashcards.html`, `assets/quiz.html`. To use one: read the file, replace the single `/*__CONFIG__*/` placeholder with a JSON object, and publish as an HTML artifact. CONFIG always contains:

```js
{
  courseName, targetLanguage, instructionLanguage, level,
  labels: { ... },        // every UI string, written in the instruction language
  colors: { verb:"#dc2626", particle:"#2563eb", subject:"#16a34a", object:"#ea580c", ending:"#9333ea" },
  notion: { vocabDataSource: "collection://…", parentPageUrl: "…" },   // when writeback applies
  payload: { ... }        // module-specific: lesson content | cards | quiz items
}
```

Asset paths are relative to this skill's folder. Strings inside `payload` may contain a small trusted HTML subset — `<span class="verb|particle|subject|object|ending">` and `<ruby>` annotations — which templates render only into designated content slots.

Templates may call the Anthropic API from their JS (no key needed): `POST https://api.anthropic.com/v1/messages`, model `claude-sonnet-4-6`, `max_tokens: 1000`. If a template file is missing, build an equivalent single-file HTML artifact from scratch honoring this contract (CONFIG shape, labels in the instruction language, color conventions, loading states, recap fallback). The artifact-Claude is stateless and blind — every request must carry its own context from CONFIG. For Notion writeback, pass `mcp_servers: [{type:"url", url:"https://mcp.notion.com/mcp", name:"notion"}]` and batch one update call at the end. Always: loading states on every call, one retry on failure, and a manual **recap screen** fallback the learner can paste back into chat if writeback fails.

## Pedagogy defaults

Sessions target 20–40 minutes. Lesson flow: warm-up recall → homework review → presentation (contrastive) → guided drills → free production → mini-quiz → wrap-up with homework. Correct errors by recasting (show the correct form, one-line why) rather than lecturing. Spiral constantly: every lesson and test recycles earlier material (~20%). Praise specifically, not generically.

## File map

- `references/notion-schema.md` — exact DDL, creation order, MCP write quirks. Read before any workspace init or schema repair.
- `references/setup.md` — workspace init (idempotent) + new-course interview + course row creation.
- `references/planning.md` — course skeleton (once) + expand each section into lesson stubs. Outlines only.
- `references/lesson-builder.md` — write one lesson's full content just-in-time; creates its vocab rows.
- `references/lesson.md` — deliver one lesson end-to-end.
- `references/vocab.md` — SRS review session; the SM-2-lite rules live here.
- `references/evaluation.md` — section gate tests + placement; thresholds live here.
