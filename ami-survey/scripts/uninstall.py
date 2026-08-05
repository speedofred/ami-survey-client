"""Remove everything this project put on your computer.

    python3 scripts/uninstall.py            # remove the skill and agent settings
    python3 scripts/uninstall.py --purge    # also delete surveys stored locally

Takes no arguments in the normal case: it looks in every place the installer
writes, removes what it finds, and reports each one. Anything it did not put
there is left alone and said so, because an uninstaller that deletes a file it
does not recognise is worse than one that leaves something behind.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # .../ami-survey
SERVER_KEY = "ami-survey"


def installed_skill_names() -> list[str]:
    """Names of the skills this project ships, so only those are removed."""
    skills = ROOT / "skills"
    if not skills.is_dir():
        return ["ami-survey"]
    return sorted(p.name for p in skills.iterdir() if (p / "SKILL.md").is_file())


def _is_ours(path: Path) -> bool:
    """Only remove a skill directory this project actually installed."""
    if path.is_symlink():
        try:
            return "ami-survey" in str(path.resolve())  # our repo directory
        except OSError:
            return True  # a broken link of ours is still ours to clear
    return path.is_dir() and (path / "SKILL.md").is_file()


def remove_skill(path: Path, report: list[str]) -> None:
    if not (path.exists() or path.is_symlink()):
        return
    if not _is_ours(path):
        report.append(f"  left alone   {path}\n               (not something this installer created)")
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)
    report.append(f"  removed      {path}")


def remove_server(target: Path, report: list[str]) -> None:
    """Drop our entry from a JSON config, leaving every other server intact."""
    if not target.exists():
        return
    try:
        config = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        report.append(f"  could not read {target}: {exc}")
        return

    servers = config.get("mcpServers") or {}
    if SERVER_KEY not in servers:
        return

    backup = target.with_suffix(target.suffix + ".ami-backup")
    shutil.copy2(target, backup)
    del servers[SERVER_KEY]
    if not servers:
        config.pop("mcpServers", None)

    handle, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".ami-uninstall-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    remaining = len(servers)
    report.append(
        f"  removed      '{SERVER_KEY}' from {target}"
        f" ({remaining} other server{'s' if remaining != 1 else ''} left untouched)"
    )
    report.append(f"  backup       {backup}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="uninstall.py",
        description="Remove the AMI survey skill and agent settings.",
    )
    parser.add_argument(
        "--purge", action="store_true",
        help="also delete surveys stored on this computer (data/). Irreversible.",
    )
    args = parser.parse_args(argv)

    home = Path.home()
    project_root = ROOT.parent
    report: list[str] = []

    print("Removing the AMI survey\n")

    for parent in (
        home / ".claude" / "skills",
        home / ".codex" / "skills",
        project_root / ".claude" / "skills",
    ):
        for name in installed_skill_names():
            remove_skill(parent / name, report)

    for config in (home / ".claude.json", project_root / ".mcp.json"):
        remove_server(config, report)

    data = ROOT / "data"
    if data.is_dir():
        responses = list((data / "responses").glob("*.json")) if (data / "responses").is_dir() else []
        if args.purge:
            shutil.rmtree(data)
            report.append(f"  deleted      {data} ({len(responses)} stored survey(s))")
        else:
            report.append(
                f"  kept         {data} ({len(responses)} survey(s) stored on this computer)"
                "\n               re-run with --purge to delete them"
            )

    codex_config = home / ".codex" / "config.toml"
    print("\n".join(report) if report else "  nothing found to remove")
    print()
    if codex_config.exists():
        print(f"One manual step: open {codex_config} and delete the")
        print("[mcp_servers.ami-survey] block. It is not edited automatically because")
        print("that file can hold credentials for other things.")
        print()
    print("Then fully quit and reopen your agent.")
    print()
    print("The downloaded folder itself is still here, and is safe to delete:")
    print(f"  {project_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
