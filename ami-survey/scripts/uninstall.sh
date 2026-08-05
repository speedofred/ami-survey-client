#!/usr/bin/env bash
# Wrapper around uninstall.py. Windows users run the .py directly.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${AMI_PYTHON:-python3}" "$HERE/uninstall.py" "$@"
