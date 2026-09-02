from __future__ import annotations

import mmap
from pathlib import Path

from context import Context, Decision
from shellwords import SimpleCommand

RULES: dict[str, str] = {
    "find": "Deny find when fd is installed; use fd (escape hatch: FIND_OK=1 find …)",
    "grep": "Deny grep -r when ripgrep is installed; use rg (escape hatch: GREP_OK=1 grep -r …)",
}
KEYWORDS: tuple[str, ...] = ("find", "grep")
REQUIRES: dict[str, tuple[str, ...]] = {"find": ("fd", "fdfind"), "grep": ("rg",)}

GREPS = {"grep", "egrep", "fgrep"}
# grep short options whose argument may be glued on (-e r ≠ -r)
GREP_OPTS_WITH_ARG = "efmABCdD"

MARKER = "fd/rg cheat sheet, shown once per context"

SHEET = r"""── {MARKER} (`{FD} -h` / `rg -h` print the short help) ──
TRAPS — read first:
  Both skip .gitignore'd paths AND dot-files/dirs by default; find/grep don't.
    Want everything?  {FD} -u (= -H -I)    rg -uu (= --no-ignore --hidden; -uuu adds --binary, but only -a prints binary matches)
  {FD} takes a REGEX on the basename: `{FD} *.py` is a shell-glob trap → {FD} -e py or {FD} -g '*.py';  -p matches the full path
  rg's first non-flag arg is the PATTERN: `rg src/` searches for the text "src/" (exit 0, no warning) → rg PAT src/
  rg silently skips binary files (grep prints "Binary file matches"); -a searches them as text
  Under sudo, brew binaries are not on secure_path:  sudo "$(command -v {FD})" …

{FD} [flags] [regex] [path…]   smart-case; -g glob, -F literal, --exact whole name; `{FD} -- '-x'` for patterns starting with -
  find . -name '*.py'              →  {FD} -e py            (or {FD} -g '*.py')
  find . -iname '*foo*'            →  {FD} foo              (find . -name foo, exact → {FD} --exact foo)
  find . -type f|d|l               →  {FD} -t f | -t d | -t l    (-t x executables, -t e empty)
  find . -maxdepth 2 / -mindepth 1 →  {FD} -d 2 / --min-depth 1   (--exact-depth N)
  find . -path '*/node_modules' -prune -o …  →  {FD} -E node_modules   (glob, repeatable)
  find . -mtime -1 / -mmin -30     →  {FD} --changed-within 1d / 30min    (--changed-before 2weeks; mtime only)
  find . -size +10M                →  {FD} -S +10mi         (find's M is MiB = mi; m = MB; unit required: b k m g t ki mi gi ti)
  find . -user core                →  {FD} --owner core     (--owner :group, '!user' negates)
  find . -exec cmd {} \;           →  {FD} … -x cmd {}     ({/} name, {//} dir, {.} no ext; -x last; PARALLEL, no shell; -j1 = sequential)
  find . -exec cmd {} + / -delete  →  {FD} … -X cmd / -X rm
  find . -xdev                     →  {FD} --one-file-system;   -L follows symlinks;   -0 for xargs -0;   -a absolute paths
  no extension: {FD} '^[^.]+$' -t f;   OR by regex: {FD} '\.(py|js)$';   {FD} PAT --and PAT2;   --format '{//}' prints path fields only

rg [flags] PATTERN [path…]       case-sensitive by default; line numbers + per-file headings automatic; -w -x -F -v -o -A -B -C as in grep
  grep -rn foo .                   →  rg foo              (-N no line numbers, -H/-I force/suppress filenames)
  grep -ri foo                     →  rg -i foo           (-S smart-case, -s force sensitive)
  grep -r --include='*.py' foo     →  rg -t py foo   or   rg -g '*.py' foo      (rg --type-list; -g beats -t)
  grep -r --exclude-dir=vendor     →  rg -g '!vendor' foo       (include-globs for dirs need /**: -g 'src/**'; later globs win; -T py = not type)
  grep -rl / -rL / -rc             →  rg -l / --files-without-match / -c    (-c omits 0-count files: --include-zero; --count-matches = per match)
  grep -rE … / -rP …               →  rg …  / rg -P …     (-P for look-around and backreferences);  `rg -e -foo` for patterns starting with -
  grep -ro foo                     →  rg -o foo           (-r REPL rewrites output only, never files)
  grep -rz … / multi-line          →  rg -z … (decompressor must be on PATH; silent if missing) / rg -U 'a\nb' (`.` needs --multiline-dotall to cross lines)
  find . -type f | …               →  rg --files [path]   (candidate files under the same ignore rules; a real search additionally skips binaries)
  -m N max per file, --max-filesize 5M, --sort path for stable order

Combine: rg walks the tree itself, so `find … | xargs grep` is usually just `rg PAT [path]`.
  Filters rg lacks (size, mtime, owner, depth, file type) → {FD} … -X rg -n PAT   or   {FD} -0 … | xargs -0 rg PAT
"""

TERSE = """{FD} [regex | -g GLOB] [path]   (-e ext, -t f|d, -d depth, -E exclude, -u = ignored+hidden)
   pattern is a REGEX on the basename — `{FD} *.py` is a shell trap, use -e py; -p to match the path
rg PATTERN [path]              (-t py / -g '*.py', -l, -i, -w, -F, -C3, -uu = ignored+hidden)
   first arg is the PATTERN — `rg src/` searches for the text "src/"; pass the dir as the 2nd arg
(The full cheat sheet was shown earlier in this context; `{FD} -h` / `rg -h` print the short help.)"""


def grep_is_recursive(args: list[str]) -> bool:
    it = iter(args)
    for a in it:
        if a == "--":
            return False
        if a in ("--recursive", "--dereference-recursive", "--directories=recurse"):
            return True
        if a == "--directories":
            return next(it, "") == "recurse"
        if a.startswith("--") or not a.startswith("-") or a == "-":
            continue
        for i, c in enumerate(a[1:]):
            if c in "rR":
                return True
            if c in GREP_OPTS_WITH_ARG:
                if c == "d" and (a[i + 2:] or next(it, "")) == "recurse":
                    return True
                break
    return False


def sheet_already_shown(transcript_path: str | None) -> bool:
    if not transcript_path:
        return False
    try:
        with Path(transcript_path).open("rb") as f:
            if f.seek(0, 2) == 0:
                return False
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
                return m.find(MARKER.encode()) != -1
    except (OSError, ValueError):
        return False


def installed(ctx: Context, rule_id: str) -> str:
    if rule_id not in ctx.enabled:
        return ""
    return next((path for tool in REQUIRES[rule_id] if (path := ctx.which(tool))), "")


def opted_out(cmd: SimpleCommand, var: str) -> bool:
    return any(a.startswith(var + "=") and a.split("=", 1)[1] not in ("", "0") for a in cmd.assigns)


def check(ctx: Context) -> Decision | None:
    if ctx.cmds is None:
        return None

    fd_bin = installed(ctx, "find")
    rg_bin = installed(ctx, "grep")
    if not (fd_bin or rg_bin):
        return None

    found: set[str] = set()
    for cmd in ctx.cmds:
        if cmd.name == "find" and fd_bin and not opted_out(cmd, "FIND_OK"):
            found.add("find")
        if cmd.name in GREPS and rg_bin and grep_is_recursive(cmd.args) and not opted_out(cmd, "GREP_OK"):
            found.add("grep")
    if not found:
        return None

    fd = Path(fd_bin).name or "fd"
    lines = ["Blocked by the shell-guard hook."]
    if "find" in found:
        lines.append(f"`find` → use `{fd}` ({fd_bin}).")
    if "grep" in found:
        lines.append(f"`grep -r` → use `rg` ({rg_bin}).")
    lines.append(
        "They are much faster and respect .gitignore. If the replacement genuinely cannot express this\n"
        "(find: -perm, -newer REF, -atime/-ctime, -printf beyond paths, -inum, -samefile, -links, -ok,\n"
        "OR across different predicate kinds), re-run the exact same command prefixed with\n"
        "FIND_OK=1 (find) or GREP_OK=1 (grep), e.g. `FIND_OK=1 find …`."
    )
    lines.append("")
    template = TERSE if sheet_already_shown(ctx.transcript_path) else SHEET
    lines.append(template.replace("{MARKER}", MARKER).replace("{FD}", fd))
    return "deny", "\n".join(lines)
