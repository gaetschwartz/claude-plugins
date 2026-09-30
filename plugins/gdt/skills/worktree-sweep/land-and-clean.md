# Landing and cleanup

Run these from the session's own worktree with plain, single git commands. A worktree-isolated session refuses `git -C <other path>`, shell loops and globs around git, so issue one command per call (independent ones in parallel).

## Land

The fix workflow returns `branches: [{family, branch, commit, ...}]`. Never assume one commit per branch — a resumed or retried agent can end up stacked on another family's branch.

1. **Read the real topology.** One call per branch:
   `git rev-list --reverse <base>..<branch>` — oldest first. Two branches sharing commits (stacked) means those commits are picked once; dedupe by SHA.
2. **Order by conflict risk.** `git diff --numstat <base> <branch>` per branch; land the families touching the fewest files first and the broadest/UI-heavy ones last, so early picks are trivially clean and the hard merges happen against the most complete tree.
3. **Cherry-pick, several SHAs per call.** `git cherry-pick <sha> <sha> …`. On a conflict:
   - Read the conflicted file whole. Both sides are normally *compatible intents* (one family changed a signature, another changed the body, one dropped `async`, another hoisted a const): keep both intents in the merged text rather than picking a side.
   - `git add <file>` then `git -c core.editor=true cherry-pick --continue`, then resume the remaining SHAs.
   - "The previous cherry-pick is now empty": the commit is redundant after resolution — `git cherry-pick --skip`. If the sequencer still reports an in-progress pick once every wanted SHA is in, `git cherry-pick --abort` clears the leftover state without touching commits.
4. **Rebase onto `-B REF`** (only when given): `git rebase <ref>`. Git prints `skipped previously applied commit` for commits already upstream — normal. A commit that conflicts because upstream carries the same change (compare the hunks; identical intent) is superseded: `git rebase --skip`. Anything else, resolve as in step 3 with `git rebase --continue`.
5. **`-b TARGET`** (only when it names a branch other than the current one): after verification passes, `git push . HEAD:refs/heads/<target>` — a local fast-forward-only update. If git refuses because that branch is checked out in another worktree, stop and tell the user; leave the sweep branch as the deliverable.

## Cleanup

Skipped entirely with `-k`. Done by the session, never by an agent.

A family's worktree may be removed only when **all** hold:

| Check | How |
|---|---|
| its agent returned a result and succeeded | in the workflow result's `branches`, not in `failed` |
| its commits are landed | `git cherry <landing-branch> <family-branch>` prints no `+` lines (works after cherry-pick and rebase rewrote the SHAs) |
| its tree is clean | `git worktree remove <path>` *without* `--force` — git itself refuses on tracked changes or untracked files, so a refusal is the dirty signal |

For each family that passes: `git worktree remove <path>`, then `git branch -D <branch>` (`-D` because rebased/cherry-picked commits are not ancestors, so `-d` would refuse).

Everything that fails a check is **kept**, and the report says which check and why. In particular:

- failed or retried-and-failed agents leave dirty worktrees — those are evidence; list the path and offer `git worktree remove --force <path>` + `git branch -D <branch>` only on the user's say.
- a passing agent whose commits are not landed (e.g. a dropped family) stays until it is.

Never touch worktrees the sweep did not create: the session's own worktree and any others in `git worktree list` stay untouched. Sweep worktrees are recognisable by the branch names in the workflow result.
