#!/usr/bin/env bash
set -euo pipefail
firmware_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$firmware_root"
mode="${1:---all}"
if [[ "$mode" != --all && "$mode" != --static && "$mode" != --firmware ]]; then
    echo 'Usage: tools/validate.sh [--all|--static|--firmware]' >&2
    exit 2
fi
if [[ "$mode" != --firmware ]]; then
    test_binary="$(mktemp /tmp/pocket-tv-test.XXXXXX)"
    trap 'rm -f "$test_binary"' EXIT
    cc -std=c11 -Wall -Wextra -Werror -Imain tests/test_player.c main/player.c -o "$test_binary"
    "$test_binary"
    python3 tests/test_verify_firmware.py
    ../.venv/bin/python -m unittest discover -s ../tests -v
fi
if [[ "$mode" != --static ]]; then
    export IDF_COMPONENT_MANAGER=0
    idf.py build merge-bin -o pocket-tv-full.bin
    python3 tools/verify_firmware.py build
fi
