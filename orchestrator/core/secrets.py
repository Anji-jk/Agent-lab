"""Hiding secrets in text that is shown or posted. Best effort; the sandbox is the real protection."""

from __future__ import annotations

import re

MASK = "[masked]"

_SECRET_PATTERNS = (
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/-]{16,}=*"),
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_KEY)[A-Z0-9_]*)"
    r"(\s*[=:]\s*)(['\"]?)([^\s'\"]{6,})"
)


def mask_secrets(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)} {MASK}" if m.lastindex else MASK, text)
    return _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{MASK}", text)
