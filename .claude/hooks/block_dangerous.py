# /// script
# requires-python = ">=3.10"
# ///
"""
PreToolUse Hook: Block dangerous commands before execution.
Deterministic safety — never rely on the LLM to avoid destructive ops.

Exit code 2 + JSON with decision:block stops the command.

Security Profile Aware:
- strict/standard: Blocks all dangerous patterns
- minimal: Only blocks catastrophic patterns (rm -rf /, fork bombs, etc.)

GuardFall hardening (SCOUT-20260715-1)
--------------------------------------
The Adversa AI "GuardFall" research (2026-06-30) showed pattern guards that
match the RAW command text are bypassed because bash rewrites the command via
expansion/quote-removal/substitution before executing it. A naive raw-substring
guard also OVER-blocks: `git commit -m "...rm -rf /..."`, `grep "rm -rf /"` are
data, not commands. So this hook:

  1. Canonicalizes with deterministic, FP-free transforms — $IFS expansion,
     quote removal, ANSI-C \\xHH/\\NNN escapes, cartesian brace expansion,
     echo/printf/rev/base64 substitution, and literal VAR= assignment.
  2. STRUCTURALLY MASKS each form — tokenizes into command segments and drops
     quoted multi-word DATA arguments (a dangerous string quoted as an argument
     to echo/grep/git-commit is not a command), while keeping everything for
     shell interpreters (bash -c "..." really does execute its argument).
  3. Matches a widened set of unambiguously destructive argv shapes and
     decoder/echo-to-shell pipelines across the masked forms.

This is a deterministic defense-in-depth backstop, NOT a sandbox. Runtime-only
obfuscation a pattern engine cannot resolve — parameter expansion (${x:-rm},
${!n}, ${A:0:2}), positional params ($1), unicode ANSI-C ($'\\u002f'), and
interpreter one-liners (perl/python -c decoding base64) — remains out of reach
of any pattern guard, exactly as the GuardFall research concludes. The real
defenses against a determined adversary who controls the command string are
architectural (don't feed untrusted input to a shell; sandbox/allowlist).
"""

import json
import re
import shlex
import sys

# Import hook_config for profile-aware behavior
try:
    from hook_config import (
        get_exit_code,
        get_hook_behavior,
        get_reduced_patterns,
        is_enabled,
    )
except ImportError:
    # Fallback if hook_config not available
    def get_hook_behavior(hook_name: str) -> str:
        return "block"

    def get_exit_code(hook_name: str, issue_found: bool = True) -> int:
        return 2 if issue_found else 0

    def get_reduced_patterns(hook_name: str) -> list[str] | None:
        return None

    def is_enabled(hook_name: str) -> bool:
        return True


HOOK_NAME = "block_dangerous"

# =============================================================================
# Destructive argv patterns (matched against structurally-masked canonical forms
# unless flagged raw-only). Every entry is unambiguously catastrophic.
# =============================================================================
_ROOTS = r"[/~*]"
_RM_REC = r"(-[a-z]*r[a-z]*|--recursive)"  # short flag w/ r, or --recursive
_FLAG = r"(-[a-z-]+\s+)*"

DANGEROUS_PATTERNS = [
    # rm with a recursive flag and an absolute/home/wildcard target anywhere
    # after it (covers rm -rf /, -fr /, -r -f /, rm -rf foo /).
    r"rm\s+" + _FLAG + _RM_REC + r"\b[^\n]*\s" + _ROOTS + r"(\s|$|/)",
    r"rm\s+" + _FLAG + _RM_REC + r"\s+" + _FLAG + r"(--\s+)?" + _ROOTS,
    r"rm\s+[^\n]*--no-preserve-root\b",
    r"rm\s+" + _FLAG + _RM_REC + r"\s+" + _FLAG + r"\.(\s|$)",  # bare cwd (not ./x)
    r"rm\s+" + _FLAG + _RM_REC + r"\s+(--\s+)?[\"']?\$[A-Za-z_{(]",  # rm -r $VAR/$( )
    r"sudo\s+rm\b",
    r"chmod\s+(-R\s+|--recursive\s+)?0*777\b",
    r"chmod\s+(-R|--recursive)\s+0*[0-7]{3}\s+[/~](\s|$)",
    r"chown\s+(-R|--recursive)\s+\S+\s+[/~](\s|$)",
    r"mkfs(\.\w+|\s+-t\s+\w+)?\s+/?dev/",
    r"\bdd\b[^\n]*\bof=/dev/(sd|disk|nvme|rdisk|hd|vd|xvd|mmcblk|loop|md)",
    r">\s*/dev/(sd|disk|nvme|rdisk|hd|vd|xvd|mmcblk|loop|md)",
    r"(cat|cp)\b[^\n]*>\s*/dev/(sd|disk|nvme|rdisk|hd|vd|xvd|mmcblk)",
    r"\bshred\b[^\n]*(/dev/|\s[/~](\s|$))",
    r"\bwipefs\b\s+-a",
    r"mv\s+[/~]\S*\s+/dev/null\b",
    r"curl[^\n]*\|\s*(ba|z|c|k|da)?sh\b",
    r"wget[^\n]*\|\s*(ba|z|c|k|da)?sh\b",
    r"git\s+push\s+(-f|--force)\s+(origin\s+)?(main|master)\b",
    r"git\s+push\s+(origin\s+)?(main|master)\s+(-f|--force)\b",
    r"git\s+reset\s+--hard\b",
    r":\(\)\s*\{[^\n]*\|[^\n]*&\s*\}\s*;\s*:",
    r"echo\s+[^\n]*>\s*/etc/",
    r"\bnpm\s+publish\b(?!\s+--dry-run)",
]

# find rooted at a CATASTROPHIC location + a destructive action. Bare /, system
# dirs, $HOME only — so `find . -exec rm`, `find ~/.cache -delete`,
# `find ~ -name x -delete`, `find /tmp/x -delete` all pass.
_FIND_ROOT = (
    r"(/(etc|usr|bin|lib|var|boot|sys|dev|System|Library|home|root|opt|sbin|private)\b"
    r"|/(\s|$)|\$HOME(\s|$))"
)
DANGEROUS_PATTERNS.append(
    r"find\s+(-[a-z]+\s+)*"
    + _FIND_ROOT
    + r"[^\n]*(-delete\b|-exec(dir)?\s+(rm|shred|dd|truncate|unlink|mkfs)\b)"
)
DANGEROUS_PATTERNS.append(r"cd\s+/\s*&&[^\n]*find\s+\.[^\n]*-delete\b")

SENSITIVE_PATH_PATTERNS = [
    r"/etc/passwd",
    r"/etc/shadow",
    r"~/.ssh/",
    r"~/.aws/",
    r"\.env($|\s)",
    r"credentials\.json",
    r"\.pem$",
    r"\.key$",
]

_DECODER = (
    r"(base64\s+(-d|--decode|-D)|xxd\s+(-r|-p\s+-r|-r\s+-p)|openssl\s+enc[^\n]*-d"
    r"|base32\s+-d|uudecode|curl\b|wget\b)"
)
_SHELL = r"(ba|z|c|k|da)?sh\b"
OBFUSCATION_PATTERNS = [
    _DECODER + r"[^\n]*\|\s*(\w[^\n|]*\|\s*)*" + _SHELL,
    _SHELL + r"\s+-c\s+[\"']?[$`]\(?[^\n]*" + _DECODER,
    _SHELL + r"\s+<\(\s*" + _DECODER,
    r"(source|\.)\s+<\(\s*" + _DECODER,
    _SHELL + r"\s+<<<\s*[\"']?[$`]\(?[^\n]*" + _DECODER,
    r"\bxxd\s+-p\s+-r\s+<<<[^\n]*\|\s*" + _SHELL,
    # eval of a FETCH/DECODE substitution (executes remote/encoded code). Scoped
    # to curl/wget/base64/xxd so legit `eval $(pyenv init -)` is NOT blocked.
    r"eval\s+[\"']?[$`]\(?\s*(curl|wget|base64|xxd|base32|openssl\s+enc)\b",
]

# =============================================================================
# Deterministic, false-positive-free canonicalizations
# =============================================================================
_IFS_RE = re.compile(r"\$\{IFS[^}]*\}|\$IFS\b")
_EMPTY_QUOTES_RE = re.compile(r"''|\"\"")
_ANSI_C_RE = re.compile(r"\$'([^']*)'")
_SUBST_RE = re.compile(
    r"\$\(\s*(echo|printf|rev|base64)\s+([^)]*)\)"
    r"|`\s*(echo|printf|rev|base64)\s+([^`]*)`"
)
_HERESTR_RE = re.compile(
    r"\$\(\s*(rev|base64)\s+(?:-d\s+|--decode\s+)?<<<\s*([^)]*)\)"
    r"|`\s*(rev|base64)\s+(?:-d\s+|--decode\s+)?<<<\s*([^`]*)`"
)
_ASSIGN_RE = re.compile(r"(?:^|[;&|])\s*([A-Za-z_]\w*)=([^\s;&|]+)")
_SHELL_LIKE = {
    "sh",
    "bash",
    "zsh",
    "dash",
    "ksh",
    "csh",
    "tcsh",
    "eval",
    "exec",
    "source",
    ".",
    "xargs",
    "env",
    "nohup",
    "timeout",
    "watch",
    "sudo",
    "command",
    "nice",
    "setsid",
    "stdbuf",
    "script",
    "ssh",
    "perl",
    "python",
    "python3",
    "ruby",
    "node",
    "php",
    "awk",
}


def _strip_q(a: str) -> str:
    a = a.strip()
    return a[1:-1] if len(a) >= 2 and a[0] in "'\"" and a[-1] == a[0] else a


def _hexoct(s: str) -> str:
    s = re.sub(r"\\x([0-9A-Fa-f]{2})", lambda h: chr(int(h.group(1), 16)), s)
    s = re.sub(r"\\([0-7]{1,3})", lambda o: chr(int(o.group(1), 8)), s)
    return s.replace("\\n", " ").replace("\\t", " ").replace("\\r", " ")


def _decode_ansi_c(s: str) -> str:
    return _ANSI_C_RE.sub(lambda m: _hexoct(m.group(1)), s)


def _expand_braces(s: str) -> str:
    for _ in range(6):
        m = re.search(r"([^\s{}]*)\{([^{}]*,[^{}]*)\}([^\s{}]*)", s)
        if not m:
            break
        pre, body, post = m.group(1), m.group(2), m.group(3)
        s = (
            s[: m.start()]
            + " ".join(pre + p + post for p in body.split(","))
            + s[m.end() :]
        )
    return s


def _b64(text: str):
    import base64
    import binascii

    try:
        return base64.b64decode(text, validate=True).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return None


def _resolve_subst(s: str) -> str:
    def herestr(m):
        op = m.group(1) or m.group(3)
        blob = _strip_q(m.group(2) if m.group(2) is not None else m.group(4))
        if op == "rev":
            return blob[::-1]
        dec = _b64(blob)
        return dec if dec is not None else m.group(0)

    def sub(m):
        cmd = m.group(1) or m.group(3)
        arg = _strip_q((m.group(2) or m.group(4) or "").strip())
        if cmd in ("echo", "printf"):
            return _hexoct(arg)
        if cmd == "rev":
            return arg[::-1]
        dec = _b64(arg)
        return dec if dec is not None else m.group(0)

    prev = None
    for _ in range(4):
        if s == prev:
            break
        prev = s
        s = _HERESTR_RE.sub(herestr, s)
        s = _SUBST_RE.sub(sub, s)
    return s


def _resolve_vars(s: str) -> str:
    for k, v in dict(_ASSIGN_RE.findall(s)).items():
        v = _strip_q(v)
        s = re.sub(r"\$\{" + re.escape(k) + r"\}", v, s)
        s = re.sub(r"\$" + re.escape(k) + r"\b", v, s)
    return s


_TRANSFORMS = (
    lambda x: _IFS_RE.sub(" ", x),
    _decode_ansi_c,
    _expand_braces,
    _resolve_subst,
    _resolve_vars,
)


def _canonical_forms(command: str) -> list[str]:
    seeds = [command]
    for t in _TRANSFORMS:
        for s in list(seeds):
            r = t(s)
            if r != s and r not in seeds:
                seeds.append(r)
    composite = command
    for t in _TRANSFORMS:
        composite = t(composite)
    if composite not in seeds:
        seeds.append(composite)

    forms: list[str] = []
    for s in seeds:
        # Deliberately NO plain `" ".join(shlex.split(s))` form — it strips
        # quotes globally, un-quoting legit data args and defeating
        # _structural_mask. Quote removal is resolved per token in the mask.
        for f in (s, _EMPTY_QUOTES_RE.sub("", s)):
            if f and f not in forms:
                forms.append(f)
    return forms


# Patterns that span pipes/operators/shell-metacharacters — masking would split
# them apart. Match against RAW forms; they are not verb-substring-prone.
_RAW_ONLY = (r":\(\)", r"\|\s*(ba|z|c|k|da)?sh\b", r"cd\s+/\s*&&")


def _is_raw_pattern(pattern: str) -> bool:
    return any(marker in pattern for marker in _RAW_ONLY)


def _structural_mask(form: str) -> str:
    """Drop quoted multi-word DATA args so a dangerous string passed to
    echo/grep/git-commit is not matched; keep everything for shell interpreters
    (their arguments may be code)."""
    out = []
    for seg in re.split(r"\|\||&&|[;|&]", form):
        seg = seg.strip()
        if not seg:
            continue
        try:
            toks = shlex.split(seg, comments=False, posix=True)
        except ValueError:
            out.append(seg)
            continue
        if not toks:
            continue
        cmd = toks[0].lower().rsplit("/", 1)[-1]
        if cmd in _SHELL_LIKE:
            out.append(seg)
        else:
            out.append(" ".join(t for t in toks if " " not in t and "\t" not in t))
    return " ; ".join(out)


def _block(reason: str, pattern: str, block_type: str):
    return (
        {"decision": "block", "reason": f"BLOCKED: {reason}: {pattern}"},
        pattern,
        block_type,
    )


def check_command(command: str, reduced_patterns=None):
    """Check a command against dangerous patterns across canonical forms.

    Returns (result_dict, matched_pattern, block_type) or None if safe — the
    same 3-tuple contract as the original hook.

    reduced_patterns: minimal-profile catastrophic patterns; when given, only
    those are checked (still across canonical forms).
    """
    raw_forms = _canonical_forms(command)
    masked_forms = [m for m in (_structural_mask(f) for f in raw_forms) if m]
    active = reduced_patterns if reduced_patterns is not None else DANGEROUS_PATTERNS

    for pattern in active:
        targets = raw_forms if _is_raw_pattern(pattern) else masked_forms
        for form in targets:
            if re.search(pattern, form, re.IGNORECASE):
                return _block("Dangerous pattern detected", pattern, "dangerous")

    if reduced_patterns is None:
        for pattern in SENSITIVE_PATH_PATTERNS:
            for form in masked_forms:
                if re.search(pattern, form, re.IGNORECASE):
                    return _block("Sensitive file access", pattern, "sensitive")
        for pattern in OBFUSCATION_PATTERNS:
            for form in raw_forms:
                if re.search(pattern, form, re.IGNORECASE):
                    return _block(
                        "Obfuscated-execution pattern detected", pattern, "dangerous"
                    )
        # echo/printf "X" | sh  — X is executed as code, so check X itself.
        m = re.search(
            r"(?:echo|printf)\s+(.+?)\s*\|\s*(?:\w+\s+[^|]*\|\s*)*(ba|z|c|k|da)?sh\b",
            command,
        )
        if m:
            inner = _strip_q(m.group(1).strip())
            if inner and inner != command:
                inner_result = check_command(inner, reduced_patterns)
                if inner_result:
                    return inner_result

    return None


# =============================================================================
# Presentation + entrypoint
# =============================================================================
def truncate_command(command: str, max_length: int = 60) -> str:
    if len(command) <= max_length:
        return command
    return command[: max_length - 3] + "..."


def print_block_box(command: str, pattern: str, block_type: str = "dangerous") -> None:
    truncated = truncate_command(command)
    if block_type == "sensitive":
        title = "SENSITIVE FILE ACCESS BLOCKED"
        description = "Matched sensitive path pattern"
    else:
        title = "DANGEROUS COMMAND BLOCKED"
        description = "Matched forbidden pattern"

    content_width = max(60, len(truncated) + 4, len(pattern) + len(description) + 4)
    top_border = "═" * (content_width + 2)
    print(f"\n╔{top_border}╗", file=sys.stderr)
    print(f"║ {title:^{content_width}} ║", file=sys.stderr)
    print(f"╠{top_border}╣", file=sys.stderr)
    print(f"║ Command: {truncated:<{content_width - 9}} ║", file=sys.stderr)
    print(
        f"║ {description}: {pattern:<{content_width - len(description) - 3}} ║",
        file=sys.stderr,
    )
    print(f"╠{top_border}╣", file=sys.stderr)
    print(
        f"║ {'TIP: Use /gate for legitimate dangerous operations':<{content_width}} ║",
        file=sys.stderr,
    )
    print(f"╚{top_border}╝\n", file=sys.stderr)


def main():
    try:
        if not is_enabled(HOOK_NAME):
            sys.exit(0)

        input_data = json.loads(sys.stdin.read())
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {})

        if tool_name != "Bash":
            sys.exit(0)

        command = tool_input.get("command", "")

        # Minimal profile: only catastrophic patterns (still across canonical forms).
        reduced_patterns = get_reduced_patterns(HOOK_NAME)
        result = check_command(command, reduced_patterns=reduced_patterns)

        if result:
            result_dict, pattern, block_type = result
            print_block_box(command, pattern, block_type)
            print(json.dumps(result_dict))
            sys.exit(get_exit_code(HOOK_NAME, issue_found=True))

        # network_egress_guard has no settings.json entry of its own (that
        # file is human-protected) — it runs piggybacked on this hook's
        # existing Bash PreToolUse registration instead. See SECURITY.md §
        # Network Egress Guard. Imported here (deferred), NOT at module
        # level: network_egress_guard imports _canonical_forms/
        # _structural_mask back FROM this module, so a top-level `import
        # network_egress_guard` is circular — whichever module a caller
        # imports first ends up mid-execution when the other tries to pull
        # names from it, and the resulting ImportError silently falls back
        # to identity canonicalization (verified: this actually happened —
        # it broke the false-positive guard on data arguments like `git
        # commit -m "...curl https://... bug"`). Deferring the import to
        # call time guarantees this module has already finished executing
        # (main() only runs after that), so the cycle always resolves clean.
        try:
            import network_egress_guard
        except ImportError:  # pragma: no cover - fallback if module unavailable
            network_egress_guard = None

        if network_egress_guard is not None and is_enabled("network_egress_guard"):
            egress_result = network_egress_guard.check_command(command)
            if egress_result:
                egress_dict, hosts = egress_result
                network_egress_guard.print_block_box(command, hosts)
                print(json.dumps(egress_dict))
                sys.exit(get_exit_code("network_egress_guard", issue_found=True))

        # slopsquat_check also has no settings.json entry of its own (that file
        # is human-protected) — it piggybacks on this hook's Bash registration
        # too, checking pip/npm install commands for hallucinated/slopsquatted
        # packages BEFORE the install runs. Unlike network_egress_guard it does
        # NOT import anything back from this module, so there is no import
        # cycle; it is still imported deferred here for parity and to keep its
        # (network-touching) import cost off block_dangerous's own import path.
        try:
            import slopsquat_check
        except ImportError:  # pragma: no cover - fallback if module unavailable
            slopsquat_check = None

        if slopsquat_check is not None and is_enabled("slopsquat_check"):
            slop_result = slopsquat_check.check_command(command)
            if slop_result:
                decision, slop_dict, slop_findings = slop_result
                slopsquat_check.print_report(command, slop_findings)
                if decision == "block":
                    print(json.dumps(slop_dict))
                    sys.exit(get_exit_code("slopsquat_check", issue_found=True))
                # warn-only: surface to the user without blocking the install
                sys.exit(1)

        sys.exit(0)

    except json.JSONDecodeError:
        sys.exit(0)
    except Exception as e:
        print(f"Hook error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
