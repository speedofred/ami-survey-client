"""Install the AMI survey skill and MCP server, on any operating system.

    python3 scripts/install.py --user
    python3 scripts/install.py --codex

A published client submits to the hosted survey and has no other destination, so
there is nothing to point it at - it asks for your token and that is all. A
development checkout, which carries the server half, takes `--api-url` to submit
somewhere other than the local API it would otherwise start.

Written in Python rather than shell for one decisive reason: **the interpreter
running this script is the interpreter written into the configuration.** On
Windows that sidesteps the worst trap in the whole setup - `python3` there is
often a Microsoft Store stub that opens the Store instead of running anything,
so a shell script resolving `python3` configures an agent that can never start.

It also means no bash, no symlink permissions, and no POSIX path assumptions,
all of which a Windows install would otherwise trip over.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # .../ami-survey
SKILLS_DIR = ROOT / "skills"

# Imported rather than restated so the installer and the client can never
# disagree about where submissions go. `config` reads no files and starts
# nothing at import; it only resolves paths.
sys.path.insert(0, str(ROOT))
from ami_survey import config  # noqa: E402


def available_skills() -> list[Path]:
    """Every skill this project ships. Discovered, not listed, so adding one is
    a directory rather than an edit here and in the uninstaller."""
    return sorted(p for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file())


def _link_or_copy(src: Path, dest: Path) -> str:
    """Prefer a symlink so `git pull` updates the skill; copy when we cannot.

    Windows only permits symlinks with Developer Mode on or from an elevated
    prompt, and failing the whole install over that would be absurd. A copy
    works identically until the repository changes, at which point the installer
    has to be re-run - which the caller is told.
    """
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    elif dest.is_dir():
        shutil.rmtree(dest)

    try:
        dest.symlink_to(src, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError):
        shutil.copytree(src, dest)
        return "copy"


def _write_json_atomically(target: Path, mutate) -> None:
    """Read, modify and replace a JSON config without risking truncation.

    ~/.claude.json is written by Claude Code itself while it runs, so a plain
    read-modify-write can lose a concurrent change or leave a half-written file
    if interrupted.
    """
    config = {}
    backup = None
    if target.exists():
        config = json.loads(target.read_text(encoding="utf-8"))
        backup = target.with_suffix(target.suffix + ".ami-backup")
        shutil.copy2(target, backup)

    mutate(config)

    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".ami-install-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, target)  # atomic: readers see old or new, never partial
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    if backup:
        print(f"backup   -> {backup}")


def server_entry(api_url: str, api_token: str) -> dict:
    env = {"PYTHONPATH": str(ROOT)}
    # Written into the entry, never exported: a desktop agent is launched from
    # the Dock or Start menu and inherits nothing from your terminal.
    if not config.SERVER_HALF_PRESENT:
        # No AMI_API_URL: this client reads none. Writing one would suggest the
        # destination is a setting, and the first thing anyone does with a
        # setting is change it.
        env["AMI_API_TOKEN"] = api_token
    elif api_url:
        env |= {
            "AMI_API_URL": api_url,
            "AMI_API_TOKEN": api_token,
            "AMI_AUTOSTART_API": "0",
        }
    return {
        "command": sys.executable,
        "args": ["-m", "ami_survey.mcp_server"],
        "env": env,
    }


#: what the agent calls itself when the installer registers on its behalf
AGENT_NAMES = {"user": "claude-code", "codex": "codex", "project": "claude-code"}


def register(scope: str, label: str = "") -> str:
    """Obtain a submission token, with no human on the other end.

    The installer is the only place that knows a token is needed, so it is the
    only place that can reasonably get one. Anything else means telling somebody
    to go and read a different page in the middle of a setup they are already
    halfway through.
    """
    from ami_survey import client

    if not label:
        sys.stdout.flush()
        try:
            label = input(
                "\nA short name for this token, so it can be recognised later\n"
                "  (e.g. 'my laptop', 'the office mac'): "
            ).strip()
        except EOFError:
            label = ""
    if len(label) < 3:
        print("\nThat name is too short to be recognisable later.", file=sys.stderr)
        raise SystemExit(1)

    print(f"\nRegistering with {config.SURVEY_SERVICE_URL} ...")
    try:
        result = client.post("/tokens", {
            "label": label,
            "agent": {"name": AGENT_NAMES.get(scope, "unknown")},
        })
    except client.ApiCallFailed as exc:
        detail = exc.payload.get("error") if isinstance(exc.payload, dict) else exc.payload
        print(f"\nThe survey refused the registration: {detail}", file=sys.stderr)
        if exc.status == 404:
            print("This survey is not open for self-registration. Ask whoever "
                  "pointed you here for a token.", file=sys.stderr)
        elif exc.status == 429:
            print("Too many tokens have been issued recently. Wait a while, or "
                  "ask for one directly.", file=sys.stderr)
        raise SystemExit(1)
    except client.ApiUnavailable as exc:
        print(f"\nCould not reach the survey: {exc}", file=sys.stderr)
        raise SystemExit(1)

    token = result["token"]
    print("\n  " + token + "\n")
    print("That is your submission token. It is shown once and is not")
    print("recoverable - keep a copy if you want to reinstall without")
    print("registering again. It is being written into your agent's")
    print("configuration now, so you do not need to do anything with it.")
    print(f"\nLimit: {result['limits']['submissions']} submissions on this token.")
    return token


#: Where a previous run of this installer put a token. Claude Code's config is
#: written by us; Codex's is printed for the operator to paste, so it may or may
#: not be there. Both are read, because a token belongs to the person, not to
#: the runtime that happens to be sending it.
def _token_sources(project_root: Path) -> list[tuple[str, Path]]:
    home = Path.home()
    return [
        ("~/.claude.json", home / ".claude.json"),
        ("~/.codex/config.toml", home / ".codex" / "config.toml"),
        (".mcp.json", project_root / ".mcp.json"),
    ]


def _existing_token(project_root: Path) -> tuple[str, str]:
    """A token this machine already holds, and where it was found.

    Read with a regex rather than a parser on purpose: one of these files is
    TOML, `tomllib` only exists from 3.11, and this project supports 3.9. A
    parser is the right tool for reading a config; this is looking for one
    string in a file we wrote, and failing to find it costs nothing but a
    prompt the operator was going to see anyway.
    """
    pattern = re.compile(r'AMI_API_TOKEN"?\s*[:=]\s*"([^"\s]+)"')
    for label, path in _token_sources(project_root):
        try:
            found = pattern.search(path.read_text(errors="replace"))
        except OSError:
            continue
        if found:
            return found.group(1), label
    return "", ""


def install(scope: str, api_url: str, api_token: str) -> int:
    if sys.version_info < (3, 9):
        print(f"This needs Python 3.9 or newer. Running under {sys.version.split()[0]}.",
              file=sys.stderr)
        return 1

    home = Path.home()
    project_root = ROOT.parent
    skill_dir = {
        "user": home / ".claude" / "skills",
        "codex": home / ".codex" / "skills",
    }.get(scope, project_root / ".claude" / "skills")

    print("AMI survey installer")
    print(f"  package:  {ROOT}")
    print(f"  python:   {sys.executable} ({sys.version.split()[0]})")
    print(f"  platform: {sys.platform}")
    print(f"  scope:    {scope}")
    if not config.SERVER_HALF_PRESENT:
        print(f"  survey:   {config.SURVEY_SERVICE_URL} (the only destination)")
    elif api_url:
        print(f"  survey:   {api_url} (token supplied)")
    print()

    skill_dir.mkdir(parents=True, exist_ok=True)
    copied = False
    for src in available_skills():
        dest = skill_dir / src.name
        how = _link_or_copy(src, dest)
        copied = copied or how == "copy"
        print(f"skill    -> {dest} ({how})")
    if copied:
        print("            (your system does not allow symlinks here, so these are"
              " copies - re-run this installer after updating the repository)")

    if scope == "codex":
        # TOML inline table with bare keys, which is what Codex's own docs show.
        env = ", ".join(
            f"{k} = {json.dumps(v)}"
            for k, v in server_entry(api_url, api_token)["env"].items()
        )
        print(f"""
mcp: add this to {home / '.codex' / 'config.toml'}
     (or add the server through the Codex interface):

[mcp_servers.ami-survey]
command = {json.dumps(sys.executable)}
args = ["-m", "ami_survey.mcp_server"]
env = {{ {env} }}

Then FULLY QUIT and reopen Codex - it reads this only at launch.""")
        return 0

    target = home / ".claude.json" if scope == "user" else project_root / ".mcp.json"
    _write_json_atomically(
        target,
        lambda cfg: cfg.setdefault("mcpServers", {}).__setitem__(
            "ami-survey", server_entry(api_url, api_token)
        ),
    )
    print(f"mcp      -> {target} (server 'ami-survey')")
    print()
    if not config.SERVER_HALF_PRESENT:
        print(f"Surveys go to {config.SURVEY_SERVICE_URL}. Nothing is kept here.")
        print()
    elif not api_url:
        print("API: the MCP server starts a local one on first use.")
        print(f"     To run it yourself:  {ROOT / 'bin' / 'ami-api'}")
        print()
    print("Now FULLY QUIT and reopen your agent - configuration is read only at")
    print('launch - then ask it: "Take the AMI survey regarding <workflow>"')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="Install the AMI survey skill and MCP server.",
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--user", action="store_const", const="user", dest="scope",
                       help="this user: ~/.claude.json and ~/.claude/skills")
    scope.add_argument("--codex", action="store_const", const="codex", dest="scope",
                       help="Codex: ~/.codex/skills, plus the config to paste")
    parser.add_argument("--api-url", default="",
                        help="development checkouts only: submit to a hosted "
                             "survey instead of the local API")
    parser.add_argument("--api-token", default="",
                        help="submission token; prompted for if omitted, which "
                             "keeps it out of your shell history")
    parser.add_argument("--register", action="store_true",
                        help="obtain a token from the survey instead of being "
                             "given one. Same as leaving the prompt blank.")
    parser.add_argument("--label", default="",
                        help="name for a token being registered, so it does not "
                             "have to be typed at a prompt")
    args = parser.parse_args(argv)

    api_url = args.api_url
    if not config.SERVER_HALF_PRESENT:
        # Accepted and ignored rather than rejected: the old command line is
        # written down in guides and in people's shell history, and failing it
        # would teach nothing that this does not.
        if api_url and api_url.rstrip("/") != config.SURVEY_SERVICE_URL:
            print(f"note: --api-url is ignored; this client submits to "
                  f"{config.SURVEY_SERVICE_URL} and nowhere else.\n", file=sys.stderr)
        api_url = config.SURVEY_SERVICE_URL

    scope = args.scope or "project"
    token = args.api_token
    held, where = ("", "")
    if api_url and not token and not args.register:
        # Installing for a second runtime should not mint a second identity.
        # The token is shown once and stored nowhere the operator can easily
        # find, so "paste it if you have one" was a question most people could
        # not answer - and pressing Enter, as the site tells them to, registered
        # another. Two tokens from one machine means two submission ceilings and
        # runs that do not group by the person who made them.
        held, where = _existing_token(ROOT.parent)

    if api_url and not token and args.register:
        token = register(scope, args.label)
    elif api_url and not token and held:
        token = held
        print(f"\nReusing the submission token already in {where}.")
        print("Pass --register for a second one, or --api-token to use another.")
    elif api_url and not token:
        # The whole explanation goes inside the getpass prompt on purpose.
        # getpass writes to the terminal directly while print goes to stdout, so
        # printing the context separately lets the two arrive out of order - the
        # question appearing above the explanation of what is being asked.
        try:
            token = getpass.getpass(
                f"\nSubmitting to {api_url}\n"
                "If you already have a submission token, paste it now.\n"
                "If you do not, just press Enter and one will be registered for you.\n"
                "\nToken (hidden, or Enter to register): "
            )
        except EOFError:
            token = ""
        if not token:
            token = register(scope, args.label)

    return install(scope, api_url, token)


if __name__ == "__main__":
    raise SystemExit(main())
