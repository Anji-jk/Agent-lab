"""Why an agent failed or stopped: always a reason, worked out from what was recorded (M5.5).

An agent that does not succeed must never be reported as "no detail reported".
When the runner gave a reason (``error``) it is used. When it did not, the reason is
worked out from what the run left behind, in this order:

1. a timeout;
2. Claude's usage limit (``rate_limit``);
3. Claude's own final result when it was an error (``result`` event: its subtype
   and text, e.g. ran out of turns, hit its spend cap);
4. an API error Claude met (``api_retry`` events, an error on a reply);
5. how the process ended (exit code, killed by a signal);
6. the end of stderr.

Whatever is found, the reason also says what was seen last (the last event, the last
tool, how many events) and where the files are, so a person can look further. If
nothing at all was recorded, it says exactly that, and where to look.

Everything is read from ``events.jsonl`` and ``stderr.log`` in the agent's run folder
(their paths are kept in the result). Secrets are masked and long text is cut.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orchestrator.monitoring.event_feed import mask_secrets

TAIL_BYTES = 200_000   # the end of events.jsonl that is looked at
TEXT_LIMIT = 300
STDERR_LINES = 4

SUBTYPES = {
    "error_max_turns": "ran out of turns before it finished",
    "error_max_budget_usd": "reached its own spend cap before it finished",
    "error_during_execution": "hit an error while working",
    "error_max_structured_output_retries": "could not produce a report in the required form",
}
LIMIT_NAMES = {"seven_day": "weekly", "seven_day_opus": "weekly (Opus)",
               "seven_day_sonnet": "weekly (Sonnet)", "five_hour": "5-hour"}


def _clip(text: Any, limit: int = TEXT_LIMIT) -> str:
    text = mask_secrets(" ".join(str(text).split()))
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def when_text(iso: Any) -> str:
    """A reset time as people read it: 2026-10-26 11:00 UTC."""
    from datetime import datetime, timezone
    try:
        moment = datetime.fromisoformat(str(iso))
    except ValueError:
        return str(iso)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _iso(epoch: Any) -> str | None:
    from datetime import datetime, timezone
    if isinstance(epoch, (int, float)) and not isinstance(epoch, bool):
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")
    return None


def _read_tail(path: Any) -> list[str]:
    """The last lines of a file (the whole file when it is small); [] when unreadable."""
    if not isinstance(path, str) or not path:
        return []
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(size - TAIL_BYTES, 0))
            data = handle.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if size > TAIL_BYTES else lines  # the first line may be cut


class Seen:
    """What the events file shows."""

    def __init__(self, lines: list[str]):
        self.count = 0
        self.last_type: str | None = None
        self.last_tool: str | None = None
        self.result: dict[str, Any] | None = None   # the last result event
        self.api_error: str | None = None
        self.limit: dict[str, Any] | None = None    # a rate_limit_event that refused the request
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            self.count += 1
            kind = str(event.get("type", "unknown"))
            self.last_type = kind if event.get("subtype") is None else f"{kind}/{event['subtype']}"
            if kind == "result":
                self.result = event
            elif kind == "rate_limit_event":
                info = event.get("rate_limit_info")
                if isinstance(info, dict):
                    status = info.get("status")
                    if isinstance(status, str) and not status.startswith("allowed"):
                        resets = info.get("resetsAt")
                        self.limit = {"type": info.get("rateLimitType"), "status": status,
                                      "resets_at": _iso(resets)}
            elif kind == "system" and event.get("subtype") == "api_retry":
                status = event.get("error_status")
                self.api_error = (f"Claude's API kept failing ({event.get('error') or 'error'}"
                                  f"{f', HTTP {status}' if status else ''}; "
                                  f"retry {event.get('attempt', '?')} of {event.get('max_retries', '?')})")
            elif kind == "assistant":
                message = event.get("message")
                if isinstance(event.get("error"), str):
                    self.api_error = f"Claude's reply was an error ({event['error']})"
                for part in (message.get("content") if isinstance(message, dict) else None) or []:
                    if isinstance(part, dict) and part.get("type") == "tool_use":
                        self.last_tool = str(part.get("name"))


def _exit_text(code: Any) -> str | None:
    if not isinstance(code, int) or isinstance(code, bool) or code == 0:
        return None
    if code < 0:
        return f"the process was killed by signal {-code}"
    return f"the process exited with code {code}"


def failure_reason(outcome_name: str, error: str | None, result: dict[str, Any],
                   exit_code: Any = None) -> str:
    """Why an agent did not succeed. Never empty."""
    seen = Seen(_read_tail(result.get("events_file")))
    stderr = [line.strip() for line in _read_tail(result.get("stderr_file")) if line.strip()]
    exit_code = exit_code if exit_code is not None else result.get("exit_code")

    headline = _clip(error) if error else None
    if headline is None:
        limit = result.get("rate_limit") or seen.limit
        final = seen.result or {}
        text = result.get("result_text") or final.get("result")
        subtype = result.get("subtype") or final.get("subtype")
        if result.get("timed_out") or outcome_name == "timeout":
            seconds = result.get("duration_seconds")
            headline = "It ran out of time" + (f" after {seconds:.0f} s" if isinstance(seconds, (int, float)) else "")
        elif isinstance(limit, dict):
            name = LIMIT_NAMES.get(str(limit.get("type")), str(limit.get("type")))
            resets = f"; it resets at {when_text(limit['resets_at'])}" if limit.get("resets_at") else ""
            headline = f"Claude's {name} usage limit was reached{resets}"
        elif result.get("is_error") is True or final.get("is_error") is True:
            what = SUBTYPES.get(str(subtype))
            headline = ("Claude " + what) if what else "Claude reported an error"
            if text:
                headline += f': "{_clip(text)}"'
        elif seen.api_error:
            headline = seen.api_error
        elif _exit_text(exit_code):
            headline = f"Claude stopped without a result: {_exit_text(exit_code)}"
        elif stderr:
            headline = "Claude stopped without a result"
        else:
            headline = "The agent stopped without a result and left no error text"

    facts = []
    if not error and _exit_text(exit_code) and "exit" not in headline and "signal" not in headline:
        facts.append(_exit_text(exit_code))
    if stderr:
        facts.append("stderr ended with: " + _clip(" | ".join(stderr[-STDERR_LINES:])))
    if seen.count:
        last = f"last event {seen.last_type}" + (f", last tool {seen.last_tool}" if seen.last_tool else "")
        facts.append(f"{seen.count} events recorded, {last}")
    elif result.get("events_file"):
        facts.append("no events were recorded")
    if result.get("events_file") and (not error or facts):
        facts.append(f"events: {result['events_file']}")
    return headline.rstrip(".") + "." + (f" ({'; '.join(facts)}.)" if facts else "")