#!/usr/bin/env bash
# Thin wrapper around install.py, kept so existing instructions and muscle
# memory still work. The real installer is Python, because it must also run on
# Windows - where there is no bash, symlinks need extra permissions, and the
# `python3` on PATH is often a Store stub that opens the Microsoft Store.
#
#   ./scripts/install.sh --user  --api-url https://survey.example.com
#   ./scripts/install.sh --codex
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${AMI_PYTHON:-python3}" "$HERE/install.py" "$@"
