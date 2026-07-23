# /// script
# requires-python = ">=3.10"
# ///
"""
PreToolUse Hook: Scan file content for secrets before writing.
Blocks writes that contain API keys, tokens, passwords, or private keys.

This is a DETERMINISTIC check — no LLM involved.
The agent cannot be convinced to skip this.

Security Profile Aware:
- strict/standard: Blocks on secret detection
- minimal: Warns only (relies on /gate checkpoint)
"""

import json
import math
import re
import sys

# Import hook_config for profile-aware behavior
try:
    from hook_config import get_exit_code, is_enabled
except ImportError:
    # Fallback if hook_config not available
    def get_exit_code(hook_name: str, issue_found: bool = True) -> int:
        return 2 if issue_found else 0

    def is_enabled(hook_name: str) -> bool:
        return True


# Import hook_utils for the symlink-escape guard (fundamental invariant,
# never gated behind is_enabled()/security profile).
try:
    from hook_utils import resolve_escapes_root
except ImportError:
    # Fallback if hook_utils not available: fail closed — we cannot verify
    # path safety, so treat every path as an unconfirmed escape.
    def resolve_escapes_root(file_path: str) -> tuple[bool, str]:
        return True, "hook_utils unavailable; cannot verify path safety"


HOOK_NAME = "secrets_scanner"

# Core patterns — active for all tiers (Community, Professional, Enterprise)
# 8 patterns covering the most critical credential types
CORE_PATTERNS = [
    # AWS Access Keys
    (r"AKIA[0-9A-Z]{16}", "AWS Access Key"),
    # Private keys (PEM format)
    (
        r"-----BEGIN\s+(RSA\s+|EC\s+|DSA\s+|OPENSSH\s+)?PRIVATE\s+KEY-----",
        "Private Key",
    ),
    # Database connection strings with credentials
    (
        r"(?i)(?:mysql|postgres|postgresql|mongodb|redis|mssql)://\w+:[^@\s]+@",
        "Database Connection String with Password",
    ),
    # GitHub classic tokens
    (r"gh[pousr]_[A-Za-z0-9_]{36,}", "GitHub Token (Classic)"),
    # JWT tokens (in assignments, not imports/comments)
    (
        r"(?i)(?:token|jwt|bearer)\s*[=:]\s*['\"]eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+",
        "JWT Token",
    ),
    # Hardcoded passwords
    (r"(?i)password\s*[=:]\s*['\"][^'\"]{8,}['\"]", "Hardcoded Password"),
    # Generic API keys
    (r"(?i)api[_\-]?key\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{20,}['\"]?", "API Key"),
    # Generic secret assignments
    (
        r"(?i)(?:secret|token|apikey|api_key|auth_token|access_token|private_key)\s*[=:]\s*['\"][A-Za-z0-9+/=_\-]{32,}['\"]",
        "Generic Secret Assignment",
    ),
]

# Extended patterns — active for all tiers alongside CORE_PATTERNS
# 15 additional service-specific patterns
EXTENDED_PATTERNS = [
    # AWS Session Tokens (STS temporary credentials)
    (r"ASIA[0-9A-Z]{12,16}", "AWS Session Token (STS)"),
    # AWS Secret Access Key
    (
        r"(?i)aws[_\-\.]?secret[_\-\.]?access[_\-\.]?key\s*[=:]\s*['\"]?[A-Za-z0-9/+=]{40}",
        "AWS Secret Key",
    ),
    # GCP Service Account JSON (detect private_key field)
    (
        r"['\"]private_key['\"]:\s*['\"]-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----",
        "GCP Service Account Private Key",
    ),
    # Generic API secrets
    (r"(?i)api[_\-]?secret\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{20,}['\"]?", "API Secret"),
    # GitHub fine-grained personal access tokens
    (r"github_pat_[A-Za-z0-9_]{50,}", "GitHub Fine-Grained Token"),
    # GitLab tokens
    (r"glpat-[A-Za-z0-9_\-]{20,}", "GitLab Token"),
    # Slack tokens
    (r"xox[baprs]-[0-9]{10,}-[A-Za-z0-9]+", "Slack Token"),
    # Slack webhooks
    (
        r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+",
        "Slack Webhook",
    ),
    # Stripe Secret Keys (live)
    (r"sk_live_[A-Za-z0-9]{24,}", "Stripe Secret Key"),
    # Stripe Restricted Keys (live)
    (r"rk_live_[A-Za-z0-9]{24,}", "Stripe Restricted Key"),
    # SendGrid API Keys
    (r"SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}", "SendGrid API Key"),
    # Twilio API Keys
    (r"SK[0-9a-fA-F]{32}", "Twilio API Key"),
    # Google API Keys
    (r"AIza[0-9A-Za-z_-]{35}", "Google API Key"),
    # Anthropic API Keys
    (r"sk-ant-[A-Za-z0-9_-]{40,}", "Anthropic API Key"),
    # OpenAI API Keys
    (r"sk-[A-Za-z0-9]{48,}", "OpenAI API Key"),
]


def _get_active_patterns() -> list[tuple[str, str]]:
    """Return all secret-detection patterns. Secret scanning is safety-critical
    and ships in full to every tier — it is never gated behind a license."""
    return CORE_PATTERNS + EXTENDED_PATTERNS


# File extensions to skip (binary, config that legitimately has patterns)
SKIP_EXTENSIONS = {
    ".lock",
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".map",
}

# Files that legitimately reference secret patterns (documentation, tests)
SKIP_PATTERNS_IN_PATH = [
    r"\.md$",  # Documentation
    r"SECURITY\.md$",  # This very file
    r"README",  # Readmes
    r"\.example$",  # Example configs
    r"\.sample$",  # Sample configs
    r"\.template$",  # Templates
]


def calculate_entropy(s: str) -> float:
    """Calculate Shannon entropy of a string."""
    if not s:
        return 0.0
    freq = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    length = len(s)
    return -sum((count / length) * math.log2(count / length) for count in freq.values())


def should_skip_file(file_path: str) -> bool:
    """Check if this file should be skipped."""
    import os

    _, ext = os.path.splitext(file_path)
    if ext.lower() in SKIP_EXTENSIONS:
        return True
    for pattern in SKIP_PATTERNS_IN_PATH:
        if re.search(pattern, file_path, re.IGNORECASE):
            return True
    return False


def is_private_key_file(file_path: str) -> bool:
    """Check if file extension indicates a private key file."""
    import os

    _, ext = os.path.splitext(file_path)
    # Common private key file extensions
    return ext.lower() in {".pem", ".key", ".p8", ".ppk", ".pks", ".priv"}


def scan_content(content: str, file_path: str) -> list[dict]:
    """Scan content for secret patterns."""
    findings = []

    # Check if writing to a private key file
    if is_private_key_file(file_path):
        # For private key files, flag it as sensitive
        findings.append(
            {
                "line": 1,
                "type": "Private Key File",
                "preview": f"Writing to private key file: {file_path}",
                "file": file_path,
            }
        )
        return findings

    active_patterns = _get_active_patterns()
    for line_num, line in enumerate(content.split("\n"), 1):
        # Skip comments and empty lines
        stripped = line.strip()
        if stripped.startswith(("#", "//", "/*", "*", "<!--")) or not stripped:
            continue

        # Skip lines that reference env vars (this is the CORRECT pattern)
        if "process.env." in line or "os.environ" in line or "os.getenv" in line:
            continue
        if "${" in line and "}" in line:  # Shell variable expansion
            continue

        for pattern, name in active_patterns:
            match = re.search(pattern, line)
            if match:
                matched_text = match.group(0)
                # Verify it's not a placeholder/example
                placeholders = [
                    "xxx",
                    "your_",
                    "example",
                    "changeme",
                    "replace",
                    "insert",
                    "todo",
                    "<",
                    ">",
                    "...",
                ]
                if any(p in matched_text.lower() for p in placeholders):
                    continue

                findings.append(
                    {
                        "line": line_num,
                        "type": name,
                        "preview": line.strip()[:80],
                        "file": file_path,
                    }
                )

    return findings


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})

    if tool_name not in ("Write", "Edit"):
        sys.exit(0)

    file_path = tool_input.get("file_path", "")

    # Symlink-escape guard: a fundamental invariant — always blocks
    # regardless of security profile. Deliberately checked BEFORE
    # is_enabled(HOOK_NAME): a user can set security.hooks.secrets_scanner
    # to "off" for any profile via forge-config.json, and this path-safety
    # net must not vanish when they do.
    escapes, escape_reason = resolve_escapes_root(file_path)
    if escapes:
        border = "═" * 62
        print(border, file=sys.stderr)
        print("  SYMLINK ESCAPE DETECTED — Write Blocked", file=sys.stderr)
        print(border, file=sys.stderr)
        print(f"  Path: {file_path}", file=sys.stderr)
        print(f"  {escape_reason}", file=sys.stderr)
        print(border, file=sys.stderr)

        print(
            json.dumps(
                {
                    "decision": "block",
                    "reason": f"BLOCKED: Symlink escape: {escape_reason}",
                }
            )
        )
        sys.exit(2)

    # Check if hook is enabled for current security profile
    if not is_enabled(HOOK_NAME):
        sys.exit(0)

    if should_skip_file(file_path):
        sys.exit(0)

    # For Write, scan the content; for Edit, scan new_string
    content = tool_input.get("content", "") or tool_input.get("new_string", "")

    if not content:
        sys.exit(0)

    findings = scan_content(content, file_path)

    if findings:
        # Human-readable output to stderr (for user visibility)
        border = "═" * 62
        print(border, file=sys.stderr)
        print("  SECRET DETECTED — Write Blocked", file=sys.stderr)
        print(border, file=sys.stderr)
        for f in findings:
            print(f"  Line {f['line']}: [{f['type']}]", file=sys.stderr)
            print(f"  {f['preview']}", file=sys.stderr)
            print(file=sys.stderr)
        print("Fix: Use environment variables instead.", file=sys.stderr)
        print(border, file=sys.stderr)

        # JSON output to stdout (backward compatible)
        report = "SECRET DETECTED — write blocked.\n\n"
        for f in findings:
            report += f"  Line {f['line']}: {f['type']}\n"
            report += f"    {f['preview']}\n\n"
        report += "Use environment variables instead of hardcoding secrets.\n"
        report += "Example: process.env.API_KEY or os.environ['API_KEY']"

        exit_code = get_exit_code(HOOK_NAME, issue_found=True)
        decision = "block" if exit_code == 2 else "warn"
        print(
            json.dumps(
                {
                    "decision": decision,
                    "reason": report,
                    "findings": findings,
                }
            )
        )
        sys.exit(exit_code)

    sys.exit(0)


if __name__ == "__main__":
    main()
