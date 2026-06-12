---
name: knowledge
description: Obtain grounded, cited P4 / Tofino-SDE knowledge from the bundled corpus. Use to verify the corpus environment is ready and to load the operational manual for querying it (P4_14/16, TNA/PSA/PNA/V1Model, p4c/bf-p4c, P4Runtime/BF-RT, open-p4studio, bmv2/Tofino). Pass `bootstrap` to set up the env on a fresh checkout.
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/scripts/toolkit.sh:*)
---

!`"${CLAUDE_SKILL_DIR}/scripts/toolkit.sh" status --skill "$ARGUMENTS"`

If the block above shows a literal `` !`…` `` command instead of its output,
shell injection did not run here — run `${CLAUDE_SKILL_DIR}/scripts/toolkit.sh
status --skill $ARGUMENTS` with your Bash tool and follow the result.
