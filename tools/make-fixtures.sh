#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
mkdir -p tests/fixtures/hls
ffmpeg -v error -y -f lavfi -i testsrc2=size=640x360:rate=20 -f lavfi -i anullsrc=r=16000:cl=mono -t 4 -c:v libx264 -preset ultrafast -pix_fmt yuv420p -c:a aac -movflags +faststart tests/fixtures/landscape.mp4
ffmpeg -v error -y -f lavfi -i testsrc2=size=180x320:rate=20 -t 3 -c:v libx264 -preset ultrafast -pix_fmt yuv420p tests/fixtures/portrait.mp4
ffmpeg -v error -y -display_rotation 90 -i tests/fixtures/landscape.mp4 -c copy tests/fixtures/rotated.mp4
ffmpeg -v error -y -i tests/fixtures/landscape.mp4 -c copy -hls_time 1 -hls_list_size 0 tests/fixtures/hls/index.m3u8
