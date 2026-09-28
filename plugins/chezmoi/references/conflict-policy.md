# chezmoi conflict policy

Shared by the `chezmoi:update` and `chezmoi:push` skills. It decides **which conflicts you
resolve yourself and which ones you put to the user as a decision.**

## The fact that drives every call here

**A chezmoi source repo is shared across machines.** Resolving a conflict by taking one
side does not just settle a merge — it rewrites what *every other machine* will receive on
its next `chezmoi update`. A destination-side conflict is local; a source-side conflict is
global. Weigh them differently.

This is also why a value conflict is often not a merge question. If `work-laptop` says
`email = ada@corp` and `laptop` says `email = ada@home`, neither side is wrong — the value
is machine-specific, and the real fix is to make it a template. That restructuring is a
design decision, so it goes to the user.

## Resolve it yourself — do not ask

These have one defensible answer. Fix them and say what you did in the summary.

| Shape | Resolution |
|---|---|
| Both sides appended distinct entries in different regions (aliases, exports, plugin lines) | Keep both, in a stable order |
| One side unchanged from the merge base | Take the changed side |
| Both sides made the same change | Take either; it is a no-op |
| Whitespace, ordering, or formatting only | Take incoming, preserve local semantic content |
| Conflict inside a generated or derived region (a lockfile, a `run_onchange_` digest comment, a cache) | Regenerate it from its source of truth rather than merging the text. If the manifest it derives from also conflicts, resolve that first. If the artefact is legitimately machine-specific, it should not be managed — `.chezmoiignore` it and say so. |
| Local destination drift that the incoming change already contains | Discard the local copy, take incoming |
| Local destination drift in a file whose source is unchanged upstream | Keep the local edit; capture it with `re-add` (or into the template) |
| Conflict in a file that should never have been managed on this machine | Add it to `.chezmoiignore`, note it in the summary |

## Put it to the user — a real decision

Escalate only these. Each one has more than one defensible answer, and picking wrong is
expensive or hard to reverse.

| Shape | Why it is a decision |
|---|---|
| Same setting, two deliberate values | Likely wants to become a template; that changes every machine |
| One side deleted the entry, the other modified it | Intent is genuinely ambiguous |
| Identity, credential, key, or host-specific secret differs | A wrong pick leaks a secret or breaks auth |
| Resolution would restructure the source | plain file → template, splitting into `.chezmoidata`, adding `.chezmoiignore` — affects all machines |
| A `run_once_` script changed | Its content hash gates execution; the change may re-run something destructive |
| Incoming change reverses a local edit that looks deliberate | The local edit may be an intentional machine-specific override |
| Merge would drop a change that exists only on one side | Data loss, however small |

## When two rows both apply

Cite the higher-stakes row and frame the options around it. Precedence, highest first:

1. identity, credential, key or secret
2. deleted on one side, modified on the other
3. a change to a `run_once_` script
4. a resolution that would restructure the source
5. the same setting with two deliberate values

A conflicting `IdentityFile` is both "two deliberate values" and "credential" — treat it as
credential, because that framing is what makes the consequences legible.

## Escalate only with the analysis finished

You must be able to state each option's consequence before you ask. If you cannot, you are
not ready to escalate — gather first: read the full body of a changed script, run
`chezmoi diff` or `chezmoi cat` on the entry, check `chezmoi git -- log -p` for why the
other machine made the change. Escalating with "I'm not sure what this does" pushes the
analysis back onto the user, which is the thing you are here to avoid.

If, after gathering, a recommendation is still genuinely balanced, say so in one line and
still name the options — a recommendation may be "either is fine, here is the trade".

## The shape of an escalation

When you escalate, you have already done the analysis. The user picks an outcome; they
never do the work. Every escalation contains, in this order:

1. **What conflicts** — the file, and the two values or hunks, quoted.
2. **Why it is a decision** — which row of the table above it hit.
3. **The options, named, with consequences** — typically two or three, each stating what
   happens on *this* machine and on *other* machines.
4. **Your recommendation**, with a one-line reason.

Quote the two conflicting **values**, not the raw `<<<<<<<` block — the user wants to see
`ada@corp.example` vs `ada@personal.example`, not merge syntax.

Use `AskUserQuestion` with those options. Then implement the chosen option yourself.

**Several escalations at once:** resolve every self-resolvable conflict first, so the user
sees only the genuine decisions. Then batch them into a single `AskUserQuestion` call (it
takes up to four questions), most consequential first, rather than interrupting repeatedly.
If more than four survive, ask the top four and say plainly that others are waiting.

A worked example:

> `dot_gitconfig` sets `email` two ways: `ada@corp.example` (from work-laptop) and
> `ada@personal.example` (local). Both look deliberate, so this is a machine-specific
> value rather than a merge.
> - **Make it a template** *(recommended)* — branch on hostname; both machines keep their
>   own address, and future machines pick one explicitly.
> - **Take the work value everywhere** — simplest, but this laptop starts signing commits
>   with the work address.
> - **Take the personal value everywhere** — same trade in reverse.

When the file is **already a template** and the conflict is inside it, the question is not
"should this be a template" but "which axis does it vary on":

> `dot_gitconfig.tmpl` conflicts inside the existing hostname branch: the remote added a
> third machine's address to the same `else` arm the local change edited.
> - **Key the branch on a `[data]` flag** *(recommended)* — e.g. `.isWork`, set once at
>   `init`; survives machine renames, and new machines choose explicitly.
> - **Add another hostname arm** — smallest change, but the branch grows per machine and
>   silently falls through to the default when a host is renamed.

## Never do these

- Never hand the conflict back: no "please resolve the conflict in X", no "let me know how
  you'd like to merge this", no leaving `<<<<<<<` markers in a file for the user to sort out.
- Never open an interactive merge tool (`chezmoi merge`, `vimdiff`) in a non-interactive
  session — it hangs. Resolve in the working tree instead.
- Never resolve a source-side conflict by discarding a hunk you have not read.
- Never run `chezmoi apply --force` to make a destination conflict disappear; it silently
  destroys the local edit.
- Never commit a resolution containing conflict markers. Grep for `^<<<<<<<` before committing.
