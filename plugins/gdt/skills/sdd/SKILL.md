---
name: sdd
description: Run the full autonomous spec-driven-development pipeline end to end — isolate a git worktree, write the spec, write the plan, adversarially review both, then implement. Use whenever the user invokes /sdd, or asks to "run SDD", "do spec-driven development", "take this feature from idea to shipped", or wants a feature/change built autonomously through the specs→plan→review→execute flow without babysitting each step. This is the orchestrator that chains superpowers' brainstorming, writing-plans, and subagent-driven-development into one hands-off run; prefer it over invoking those skills individually when the user wants the whole feature delivered, not just one phase.
---

# SDD — Autonomous Spec-Driven Development

Take a feature from a rough idea to implemented code in one continuous, autonomous
run. You orchestrate three superpowers skills (`brainstorming` → `writing-plans` →
`subagent-driven-development`) inside an isolated worktree, with an adversarial
review pass on the spec and plan before any code is written.

**Announce at start:** "Using sdd to run the spec→plan→review→ship pipeline."

The superpowers skills you drive are built human-in-the-loop. This skill deliberately
overrides that: **you** are the decision-maker who normally would be the human
partner. Your judgment stands in for theirs on everything except genuine
blockers. That autonomy contract is the whole point — read it first, because it
governs every phase below.

## The autonomy contract

You run the entire pipeline without stopping to check in. The user asked for a
finished feature, not a status meeting. Progress summaries and "should I
continue?" prompts waste their time — don't write them.

Classify anything that would normally make you pause into three buckets:

- **Small** (naming, file layout, which test to write first, a library choice with
  an obvious default) → decide yourself and move on. Don't surface it.
- **Medium** (an ambiguous requirement with 2-3 reasonable readings, a design
  trade-off, an unexpected constraint in the codebase) → **decide the direction
  yourself.** Pick the interpretation that best serves the user's stated intent,
  record the call in the spec/plan so it's reviewable, and keep going. When a
  medium call is genuinely close, get a second opinion from a blind Opus agent
  (below) rather than stopping.
- **MAJOR** (the request is fundamentally underspecified in a way no reasonable
  default resolves; two readings would produce entirely different products;
  destructive/irreversible action outside the worktree; a hard external blocker like
  missing credentials or a failing dependency you cannot work around) → **this is
  the only case where you stop and ask the human.** State the blocker, the options,
  and your recommendation in one message, then wait.

The bar for MAJOR is high. If you find yourself wanting to stop, first ask a blind
Opus agent whether the ambiguity is really unresolvable or whether a sensible
default exists. Most "blockers" are medium calls in disguise.

## Blind Opus agents — your second opinion

At any point in the pipeline, when you want an honest, uncontaminated judgment —
"is this design sound?", "am I over-engineering this?", "is this ambiguity a real
blocker or am I being timid?", "which of these two approaches is better?" — spawn a
short-lived **blind Opus agent**: model `opus`, given only the specific question
plus the minimum context to answer it, and explicitly told to be blunt and to
disagree if warranted. Blind means it does **not** inherit your reasoning or your
preferred answer — you want its independent take, not a rubber stamp.

These are cheap and disposable. Reach for them liberally, especially before
escalating anything to MAJOR, at design forks, and whenever a review finding feels
either wrong or worryingly right. They are how you stay autonomous without flying
blind.

## Model selection for spawned agents

**Every spawned agent runs on Opus** (`model: 'opus'`) — implementers, reviewers,
fixers, and blind second-opinion agents alike. Opus handles design review, plan
critique, and implementation well, and this pipeline's work is uniformly the kind of
multi-step reasoning it's for.

Never use Haiku for any agent in this pipeline. Don't drop to Sonnet either: even the
mechanical-looking tasks here sit downstream of a spec and plan you're accountable
for. When a task's reasoning is genuinely hard, give the agent a sharper brief and
more context rather than reaching for a different model.

## The pipeline

### Phase 1 — Isolate a worktree

Create an isolated workspace so nothing touches the user's current branch. Use the
builtin **`EnterWorktree`** tool directly (this is the native worktree mechanism
`superpowers:using-git-worktrees` tells you to prefer — do not fall back to
`git worktree add` shell wrappers when the native tool exists).

First check whether you're *already* in an isolated worktree (compare git-dir vs
git-common-dir, guarding against submodules). If so, stay put. Otherwise
`EnterWorktree` for the feature.

All spec and plan artifacts live inside the repo (`docs/superpowers/specs/…` and
`docs/superpowers/plans/…`), so the worktree must exist before they're written —
that's why this is Phase 1.

### Phase 2 — Spec

Produce the spec by driving `superpowers:brainstorming`.

- **If a spec already exists** for this work (in the previous workspace or a prior
  session): don't redo it. If it isn't already inside the worktree, move it into
  `docs/superpowers/specs/` here, then proceed to Phase 3.
- **Otherwise** run brainstorming to write the design doc — but **autonomously**.
  brainstorming's `HARD-GATE` normally blocks on user approval; under this skill
  *you* provide that approval. Explore the codebase context, resolve the clarifying
  questions yourself using the autonomy contract (escalate only MAJOR ones),
  propose approaches, pick one, and write the spec to
  `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md`. Do the spec self-review
  brainstorming calls for. Use a blind Opus agent as your stand-in reviewer at the
  approval gate instead of stopping for the human.

### Phase 3 — Plan

Drive `superpowers:writing-plans` to turn the spec into a bite-sized implementation
plan at `docs/superpowers/plans/YYYY-MM-DD-<feature>.md`. Same autonomy: make the
decomposition and file-structure calls yourself; escalate only MAJOR gaps.

### Phase 4 — Adversarial review of spec + plan

Before a line of code is written, harden the spec and plan. Dispatch **at least 2
parallel review agents** (Opus by default; scale up — 3, 4, or more — when the spec
and plan are large or span multiple subsystems; a single agent can't hold an
exceptionally big plan in context well). Give each a distinct lens so they don't
overlap, e.g.:

- **Spec soundness** — does the design actually satisfy the user's intent? gaps,
  contradictions, unstated assumptions, scope creep.
- **Plan executability** — are tasks truly bite-sized, independently testable,
  correctly ordered? missing setup, hidden coupling, untestable steps.
- **(when large) Codebase fit / risk** — does this match existing patterns? what
  breaks? riskiest tasks and their blast radius.

Each agent returns concrete findings ranked by severity. When a lens hits genuinely
convoluted reasoning (a subtle concurrency invariant, a gnarly migration, a
non-obvious system interaction), narrow that agent's scope and hand it the relevant
code rather than asking it to hold the whole surface at once.

Then **you apply the fixes** to the spec and plan yourself — you hold the full
context and the authority to reconcile conflicting findings. Where reviewers
disagree or a fix is a close call, settle it with a blind Opus agent rather than
guessing or escalating.

### Phase 5 — Final review pass

Dispatch **one** Opus agent for a quick, fresh-eyes pass brushing over the revised
spec and plan together — a coherence check that the two are aligned, the fixes
landed cleanly, and nothing is left dangling. Apply anything it catches. This is a
lightweight gate, not another deep review.

### Phase 6 — Ship it

Implement the plan with `superpowers:subagent-driven-development` (fresh implementer
subagent per task, task review after each, broad final review at the end). It runs
continuously by design, which matches this skill's autonomy contract — do not pause
between tasks. Implementer/reviewer/fix subagents follow the same model rule: Opus
for every one of them, never Haiku or Sonnet. Let it drive through to the
finishing-a-development-branch step it hands off to.

## What "done" looks like

The plan is implemented in the worktree, its tests pass, and the final review is
clean — reached without a single unnecessary check-in. Report completion once, at
the end, with the branch/worktree location and a short summary of what shipped and
any medium-bucket decisions you made along the way (so they're auditable). Stop
before the end **only** for a MAJOR blocker.
