# Pocket TV

[中文](README.md)

Turn AI Passport into a little TV for your desk. Watch your own videos, revisit favorite clips, or hop through a live-channel playlist. Adjust the sound, switch programs, and hide the text for an unobstructed view.

**Keep a computer connected with a USB data cable and running the included player. The firmware cannot play on its own, and Bluetooth video is not supported.**

## Get started

1. Download and extract this repository using **Code → Download ZIP**. Install Python 3.10–3.13 on your computer.
2. Install the [v1.0.0 merged firmware](releases/v1.0.0/pocket-tv-full.bin). Build and flashing instructions are in the [Chinese README](README.md#固件).
3. Connect the device with a USB data cable. From the extracted folder, run `./cctv.sh` on macOS/Linux or `cctv.cmd` on Windows to try the included channels. The first launch downloads dependencies.
4. To watch a file, run `./play.sh /path/to/video.mp4` (Windows: `play.cmd`). You can also put files in `media/` and run the launcher without arguments.
5. Volume starts at zero. Use a direction button to turn it up. Keep the computer player running.

## Features and controls

- Local files and folders, including MP4, MOV and MKV; local playlists loop in order.
- Online videos, HLS live streams and M3U channel lists. The included sample has two CCTV+ channels; these are not CCTV-1 and CCTV-2.
- Tap a direction button to change volume by five; hold it to change the channel. Both directions reverse in landscape mode.
- Tap OK to pause or resume; hold OK to show or hide the text overlay. Volume and overlay visibility are remembered after reboot.
- Automatic video orientation and aspect-ratio preserving scaling. Text sits over a translucent background without reserving video space.
- Automatic USB reconnection after unplugging or restarting the device, restoring the channel and play/pause state. Local files restart from the beginning.
- Terminal controls: `+` / `-` for volume, `p` / `n` for channels, an empty line for pause/resume, `h` for text, and `q` to quit. Press Enter after each command.

The overlay shows volume as `vol80`, `ok pause` during playback, `ok play` when paused, and `hold to hide` while visible. Live stream availability depends on the provider and your connection. macOS hardware tests are documented in [verification](docs/verification.md); Windows launchers have not been tested on a physical Windows computer.

The cover is an AI illustration, not a device screenshot. Project code uses the MIT license; bundled components retain their own licenses and notices.
