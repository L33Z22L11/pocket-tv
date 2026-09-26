# Pocket TV

[中文](README.md)

Use AI Passport as a little TV for your desk. Watch local videos or choose a live channel on your computer, then use the device buttons for volume, channel changes and pause.

**The computer player must stay running. Installing the firmware alone does not provide standalone playback.**

<img src="assets/cover.webp" alt="Pocket TV cover: AI illustration, not a device screenshot" width="270">

## Get started

1. Download and extract the [v1.1.0 package](https://github.com/L33Z22L11/pocket-tv/releases/tag/v1.1.0). Install Python 3.10–3.13 on your computer; the first launch needs internet access.
2. Install this version from the community page, or flash the [merged firmware](releases/v1.1.0/pocket-tv-full.bin) at `0x0`. Use the matching computer player.
3. Connect the device with a USB **data cable**. Run `./tv.sh` from the project directory on macOS/Linux, or `tv.cmd` on Windows. Dependencies install on first launch.
4. Search for a channel, select it and press Enter or click the green channel-switch button. Volume initially starts at zero; use the device buttons to raise it.
5. Keep the computer player open while watching. Press `Ctrl+Q` to stop playback and exit the channel manager.

Current version: **1.1.0**. The old v1.0.0 firmware is incompatible with the current computer player.

## Device controls

| Action | Result |
|---|---|
| Tap ↑ / ↓ | Landscape: volume down / up. Portrait: up / down. |
| Hold ↑ / ↓ | Previous / next channel in either orientation |
| Tap OK | Pause / resume |
| Hold OK | Show / hide Chinese help |

Volume is remembered. The volume bar and channel name disappear after a short time; paused, loading and buffering states have Chinese notices. Video orientation adjusts automatically, with no persistent HUD during normal playback.

## Channel manager

The default theme is Catppuccin Mocha. Browse channels on the left, see playback status in the middle, and device status and logs on the right. Buttons and footer hints are clickable.

- Search by name, filter by group or favorites. `F` toggles a favorite, `Ctrl+F` focuses search, and `F5` refreshes the list.
- Space pauses/resumes, `N` / `P` changes channels, `+` / `-` adjusts volume, and `H` toggles device help.
- The first-frame check only checks whether the selected source produces a picture, not sustained reliability.
- The default list comes from [vbskycn/iptv](https://github.com/vbskycn/iptv). Other presets and custom M3U files/URLs are supported. Failed refreshes can fall back to the last cached list.

## Optional Wi-Fi transport

USB playback does not require Wi-Fi setup.

1. Leave the device connected over USB. Press `W` or click the network item in the channel manager footer; playback stops for setup.
2. Select a **2.4 GHz** network found by the device, enter its password, and click Connect and save. Manual entry is available for hidden networks.
3. After the connected message and device address appear, select a channel again. On failure, check the password and retry or rescan. Use the same path to change networks.
4. The computer and device need a mutually reachable LAN. USB has priority; loss of the data connection tries Wi-Fi, and reconnecting returns to USB. **The device still needs power and the computer player must remain running.**

Captive portals and enterprise authentication are unsupported. Handover re-buffers and is not guaranteed seamless. Sustained wireless performance and more complex networks need further testing.

## Your own videos

```sh
./play.sh /path/to/movie.mp4
./play.sh /path/to/videos
./play.sh /path/to/channels.m3u
```

Use `play.cmd` on Windows. With no path, the player reads `media/`; multiple files loop in sequence. Network video URLs also work. Type `q` and press Enter to exit. The older `cctv.sh` / `cctv.cmd` sample contains two CCTV+ channels, not CCTV-1/2.

## Limits

- Public streams can fail, take time to start or rebuffer. This project does not provide TV content or guarantee channel availability.
- The player attempts reconnection after device restarts or connection loss. Local files restart from the beginning after USB-only reconnection.
- Bluetooth video and standalone playback are unsupported.
- Physical-device checks were performed on macOS; Windows launchers have not completed equivalent testing.
- Do not share `~/.config/pocket-tv/device.json`, which contains pairing credentials. Media transport is unencrypted and intended for a trusted LAN.

## Development

[Release notes](releases/v1.1.0/README.md) · [Build and implementation notes, Chinese](docs/technical-notes.md) · [Verification record](docs/verification.md)

Run `firmware/tools/validate.sh --static` for host and protocol checks. With ESP-IDF 5.5.3 active, build the merged image using:

```sh
IDF_COMPONENT_MANAGER=0 idf.py -C firmware build merge-bin -o pocket-tv-full.bin
```

Code is [MIT licensed](LICENSE). The [Fusion Pixel-derived font](assets/fonts/fusion-pixel/README.md) and bundled components retain their own licenses. The [cover](assets/README.md) is an AI illustration saved as WebP, not a device screenshot.
