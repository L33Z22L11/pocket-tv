#!/bin/sh
set -eu
cd "$(dirname "$0")"
exec ./play.sh config/cctv.m3u "$@"
