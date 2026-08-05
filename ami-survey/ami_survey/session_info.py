"""Show which session the survey would measure, and the window it would use.

Run this before starting a benchmark run to confirm you are in a fresh session,
and after pasting the workflow prompt to confirm the window starts where you
expect. Every registered runtime is checked, so it answers the same question
whether the work is happening in Claude Code, in Codex, or in both at once.

    ami-session          the session the survey would measure
    ami-session --all    every session recorded for this directory
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from . import adapters


def _describe(module, path: Path, active: str) -> list[str]:
    entries = module._entries(path)
    calls = module.extract_calls(entries, path)
    window = module.suggest_window(entries, calls)
    meta = module.runtime_metadata(entries, path)
    prompts = module.user_prompt_times(entries)

    marker = f"  <-- {active}" if active else ""
    modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return [
        f"[{module.NAME}] session {meta.get('session_id')}{marker}",
        f"  log           {path}",
        f"  modified      {modified.isoformat(timespec='seconds').replace('+00:00', 'Z')}",
        f"  platform      {meta.get('platform')}",
        f"  human turns   {len(prompts)}",
        f"  agent calls   {len(calls)}",
        f"  window start  {window['start']}   ({window['start_basis']})",
        f"  window end    {window['end']}   ({window['end_basis']})",
        "",
    ]


def render(show_all: bool = False, cwd: str | None = None) -> str:
    cwd = cwd or str(Path.cwd())

    # (mtime, module, path) across every runtime, newest first - the same ordering
    # the adapter registry uses to decide which session is doing the asking.
    found: list[tuple[float, object, Path]] = []
    for module in adapters._modules():
        for path in module.sessions(cwd):
            try:
                found.append((path.stat().st_mtime, module, path))
            except OSError:
                continue
    found.sort(key=lambda f: f[0], reverse=True)

    if not found:
        return (
            f"No agent sessions recorded for {cwd}\n"
            f"(checked: {', '.join(adapters.available())})\n"
            "A runtime with no adapter can still be surveyed - it posts its own "
            "usage records with ami_record_calls."
        )

    # Which session gets measured depends on which runtime asks: each one names
    # itself in the environment of the survey server it launches, and then picks
    # its own newest session. Run from a plain terminal there is no such signal,
    # so the honest answer is one candidate per runtime rather than one winner.
    newest_per_runtime: dict[str, Path] = {}
    for _, module, path in found:
        newest_per_runtime.setdefault(module.NAME, path)

    shown = found if show_all else [
        f for f in found if newest_per_runtime.get(f[1].NAME) == f[2]
    ]

    out = [f"Sessions for {cwd}", ""]
    for _, module, path in shown:
        active = (
            f"{module.NAME} would measure this one"
            if newest_per_runtime.get(module.NAME) == path
            else ""
        )
        out += _describe(module, path, active=active)

    runtimes = sorted(newest_per_runtime)
    if len(found) > len(shown):
        out.append(f"{len(found)} sessions exist for this directory.")
        if not show_all:
            out.append("Pass --all to see them.")
        out.append("")
    if len(runtimes) > 1:
        out.append(
            f"Two runtimes have sessions here ({', '.join(runtimes)}). Whichever one "
            "asks for the survey measures its own newest session - it identifies "
            "itself from the environment of the survey server it launched, so a "
            "session belonging to the other runtime is not picked up by mistake."
        )
        out.append("")
    if len(found) > 1:
        out.append(
            "Still, do not run two sessions of the SAME runtime here at once during "
            "a benchmark: within one runtime the newest log wins."
        )
        out.append("")

    out.append(
        "A window starting at the first turn of a long, unrelated session means you "
        "are not in a fresh one. Start a new conversation, or have the agent pass an "
        "explicit workflow_start_time to ami_survey_begin."
    )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    print(render(show_all="--all" in argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
