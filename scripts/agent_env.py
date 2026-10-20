"""Environment handed to agent (Claude Code) processes.

An allowlist, not a denylist: a secret added to the environment later is
withheld automatically instead of leaking until someone remembers to block it.
"""

from __future__ import annotations

import os

AGENT_ENV_ALLOWLIST = frozenset({
    # process basics (Windows and POSIX)
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE",
    "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "USERNAME", "USER", "LOGNAME", "SHELL",
    "LANG", "LC_ALL", "LC_CTYPE", "TERM", "PYTHONUTF8", "PYTHONIOENCODING",
    # Claude Code authentication and configuration
    "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR",
    # corporate proxies and certificates
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE",
})


def build_agent_env(source: dict[str, str] | None = None) -> dict[str, str]:
    """Copy only allowlisted variables (case-insensitive, as on Windows)."""
    source = dict(os.environ) if source is None else source
    return {k: v for k, v in source.items() if k.upper() in AGENT_ENV_ALLOWLIST}
