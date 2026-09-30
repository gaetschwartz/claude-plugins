# Source: clippy

Rust workspaces. Sweeps `cargo clippy` lints and rustc lints (`dead_code`, `unused_*`, `deprecated`, …) — both arrive in the same JSON stream, so one capture covers both.

Detected by: `Cargo.toml` at the workspace root.

## Capture

```capture
cargo clippy --workspace --all-targets --message-format=json 2>/dev/null
```

Cargo replays cached diagnostics, so a capture can show stale or zero hits for a crate that was already built. To force fresh output for a crate, touch one of its source files. Avoid `cargo clean`.

Diagnostic code is `.message.code.code`: `clippy::foo` for clippy, bare `dead_code` for rustc. Messages without a code (build summaries, `aborting due to`) are not diagnostics and are ignored.

## Defaults

Used when the matching flag is not given. Each is detected in the workspace, and a step whose command cannot be found is skipped and reported.

| Step | Detection order |
|---|---|
| format | `just fmt` if the justfile has a `fmt` recipe, else `cargo fmt --all` |
| test | `cargo nextest run -p {units}` if `cargo-nextest` is installed, else `cargo test -p {units}` |
| verify | the capture itself: zero diagnostics for the swept rules, no rule count increases |

`{units}` is replaced by the crates the agent touched (`-p a -p b`).

## Suppression

```suppress
#[expect(lint, reason = "...")] — narrowest scope (the item or expression, never a module or crate), mandatory reason, #[expect] over #[allow]. A crate-wide #![allow] is a last resort and never for a family fix.
```

## Guidance for the agents

```notes
- Rules seldom overlap on the same line, but fixes can chain (e.g. single_match_else → if let → manual_let_else → question_mark): go straight to the final form in one step.
- result_large_err: the flagged function is not the culprit. The secondary span (query Q3) points at the oversized variant; box that variant's source ONCE in the error enum instead of touching each function, following the project's error-type conventions.
- Where a signature is forced by a trait, a derive/proc-macro or a framework, that is the legitimate case for a narrowly scoped #[expect] with a reason naming the structural constraint.
- Deleting dead code: confirm no references remain in cfg(test), benches, examples and other crates; if it is used only by tests, gate it with #[cfg(test)] instead.
- machine-applicable suggestions (query Q4) are safe to apply in bulk with a jq/python script; MaybeIncorrect ones need a look.
```

## Review focus

```focus
- Iterator-vs-index rewrites and loop-bound changes: same elements, same order, same step?
- Lock/guard scope changes: can the data change between the old and new read point?
- Cast rewrites: wrap and precision behaviour identical?
- Every #[expect]/#[allow]: is the reason true about the code it sits on; does the code actually still trigger the lint?
- Error-type changes: error chains and Display output intact?
```

## Grouping hints for the planner

Rules that belong in one family (fixed together, same idioms, same reviewers). Anything not here gets its own family or a small "misc" family; a rule with 100+ hits stays alone.

| Family | Typical rules |
|---|---|
| control-flow | manual_let_else, single_match(_else), match_like_matches_macro, redundant_guards, redundant_pattern_matching, collapsible_match, while_let_loop, if_then_some_else_none, needless_range_loop, semicolon_if_nothing_returned, manual_clamp |
| drop-guards | significant_drop_in_scrutinee, await_holding_lock, await_holding_refcell_ref |
| underscore-bindings | used_underscore_binding, used_underscore_items, no_effect_underscore_binding, redundant_locals |
| async-and-unused-params | unused_async, unused_self, needless_pass_by_ref_mut |
| signature-ergonomics | needless_pass_by_value, trivially_copy_pass_by_ref, ref_option(_ref), implicit_hasher, too_many_arguments |
| error-types | map_err_ignore, result_large_err, unnecessary_wraps |
| docs-and-must-use | missing_safety_doc, missing_errors_doc, return_self_not_must_use, must_use_candidate, double_must_use |
| string-collect-clone-idioms | format_push_string, format_collect, needless_collect, assigning_clones, default_trait_access, or_fun_call, field_reassign_with_default, unnecessary_debug_formatting |
| item-layout | items_after_statements, module_inception, type_complexity, type_repetition_in_bounds, elidable_lifetime_names, extra_unused_lifetimes, duplicated_attributes |
| trait-impl-hygiene | expl_impl_clone_on_copy, missing_fields_in_debug, new_without_default, into_iter_without_iter |
| structural | large_futures, exit |
| rustc-lints | dead_code, unused_*, unreachable_code, deprecated, recursion_depth_exceeding_limit |

## Queries

`{{FILE}}` is the capture path, `{{RULES}}` the family's full rule codes, space-separated and passed as literal arguments (zsh does not word-split a variable).

```queries
# Q1 - count per rule
cat {{FILE}} | jq -rn --args '
  [inputs | select(.reason=="compiler-message" and (.message.code.code | IN($ARGS.positional[]))) | .message.code.code]
  | group_by(.) | map({rule: .[0], n: length}) | sort_by(-.n)[]
  | "\(.n)\t\(.rule)"
' {{RULES}}

# Q2 - worklist: rule, file:line:col, source line (deduped across targets)
cat {{FILE}} | jq -rn --args '
  [inputs | select(.reason=="compiler-message" and (.message.code.code | IN($ARGS.positional[])))
   | .message as $m | $m.spans[] | select(.is_primary)
   | "\($m.code.code)\t\(.file_name):\(.line_start):\(.column_start)\t\(.text[0].text | gsub("^ +"; ""))"]
  | unique[]
' {{RULES}}

# Q3 - all spans incl. secondary/child spans and labels (* = primary)
cat {{FILE}} | jq -rn --args '
  [inputs | select(.reason=="compiler-message" and (.message.code.code | IN($ARGS.positional[])))
   | .message as $m | ([$m.spans[]] + [$m.children[].spans[]])[]
   | "\(if .is_primary then "*" else " " end) \($m.code.code)\t\(.file_name):\(.line_start):\(.column_start)\t\(.label // $m.message)"]
  | unique[]
' {{RULES}}

# Q4 - machine-applicable suggestions (replacement text per span)
cat {{FILE}} | jq -rn --args '
  [inputs | select(.reason=="compiler-message" and (.message.code.code | IN($ARGS.positional[])))
   | .message as $m | ([$m.spans[]] + [$m.children[].spans[]])[]
   | select(.suggested_replacement != null)
   | "\($m.code.code)\t\(.file_name):\(.line_start):\(.column_start)-\(.line_end):\(.column_end)\t\(.suggestion_applicability)\t\(.suggested_replacement | @json)"]
  | unique[]
' {{RULES}}

# Q5 - first two rustc-rendered diagnostics with clippy's help text (raise the limit if needed)
cat {{FILE}} | jq -rn --args 'limit(2; inputs | select(.reason=="compiler-message" and (.message.code.code | IN($ARGS.positional[]))) | .message.rendered)' {{RULES}}

# Workspace-wide counts per rule (BEFORE/AFTER regression check)
cat {{FILE}} | jq -rn '[inputs | select(.reason=="compiler-message" and .message.code != null) | .message.code.code] | group_by(.) | map({rule: .[0], n: length})[] | "\(.n)\t\(.rule)"'
```
