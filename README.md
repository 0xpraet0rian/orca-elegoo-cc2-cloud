<p align="center">
  <img src="assets/icon-512.png" width="128" alt="Elegoo CC2 Cloud icon">
</p>

<h1 align="center">Elegoo CC2 Cloud</h1>

<p align="center">
  <b>OrcaSlicer plugin for the Elegoo Centauri Carbon 2, over the Elegoo cloud.</b><br>
  Send prints, control the printer and watch the camera from OrcaSlicer 2.5.x, without ElegooSlicer.
</p>

<p align="center">
  <img alt="OrcaSlicer" src="https://img.shields.io/badge/OrcaSlicer-2.5.x-4f8cff?style=flat-square">
  <img alt="Printer" src="https://img.shields.io/badge/printer-Centauri%20Carbon%202-313134?style=flat-square">
  <img alt="Python" src="https://img.shields.io/badge/python-3.12%2B-3776ab?style=flat-square">
  <img alt="Platform" src="https://img.shields.io/badge/platform-Windows-0078d4?style=flat-square">
  <img alt="Dependencies" src="https://img.shields.io/badge/dependencies-none-4cc38a?style=flat-square">
  <img alt="Status" src="https://img.shields.io/badge/status-v0.3.2%20experimental-f59e0b?style=flat-square">
</p>

---

## Features

| | |
|---|---|
| **Send from Orca** | Slice, press *Send*, and the gcode goes to your Elegoo cloud storage, then to the printer; *Print* also starts it |
| **Control tab** | Print (pause, resume, stop, speed), temperatures, motion with homing, fans, light, Canvas filament load and unload, files, print history |
| **Camera** | Live view from the printer. It starts by itself when the printer connects (switch off with *Auto-start*), and the picture can be resized or shown full screen |
| **Live status** | Progress, layers, temperatures, remaining time and the machine state, in the tab and in Orca |
| **Multi-colour** | Filament slots come from Orca's mapping and are sent as the printer's `slot_map`; files started from the tab get a slot picker per colour |
| **Auto-refresh** | Filament slots, files and history refresh after connecting, when a print ends, when the printer goes idle and every minute |
| **Own sign-in** | Signs in with Elegoo's own page in your browser; the session is stored encrypted (Windows DPAPI) and survives plugin reinstalls |
| **Themes** | *Simple dark* (default), *Simple white* and *0xpraet0rian* (navy and orange); the choice is saved |
| **No dependencies** | One Python file and the standard library; everything native goes through `ctypes` |

## Status

An early, personal-use build, developed against one printer. What is known:

| Area | State |
|---|---|
| Cloud HTTP: device list, Agora tokens, upload URL and upload | verified live |
| Commands over RTM: `1001`, `1002`, `1044`, `1057`, `1047`, `2005` | verified live |
| Camera: joining the RTC channel and playing the video | verified live (in a browser and from the tab) |
| Sign-in through the local pass-through, session capture and auto-connect | verified in Orca |
| Status and controls in the tab | live values shown; a temperature target and axis movement were seen taking effect |
| *Send* from Prepare (upload, printer download, start) | upload and download verified in isolation; **start from Orca not verified** |
| Light, fans, speed, filament load and unload, auto refill | parameters copied from ElegooSlicer's own page, **not verified on the printer** |
| Files and print history over the cloud API | endpoints taken from the public SDK, **not verified on the printer** |
| Pause state | read from the status table of the SDK; never seen on a real print |
| Filament slot decoding from `ams_mapping` | a guess (`canvas * 4 + tray`), logged on the first run |
| Files of 500 MB or more | not implemented |
| LAN mode | not supported |

## Install

1. In Orca: **Preferences → Developer → Experimental Features**, enable
   *Use printer agents instead of print hosts*.
2. **Plugins → Install local plugin**, choose
   [`plugin/elegoo_cc2_cloud_win_x86_64.py`](plugin/elegoo_cc2_cloud_win_x86_64.py) and accept the permission dialog.
3. In the CC2 printer preset set **Printer Agent** to *Elegoo CC2 (cloud)*.
4. Open the **Elegoo CC2** tab and press **Sign in with Elegoo**. The sign-in page opens in your browser.
5. After signing in, the tab connects by itself (*Connect automatically*). Use *Send* from Prepare.

Notes:

- Orca requires the file name to end with the OS and architecture (`_win_x86_64`).
- **Close ElegooSlicer** while connected: the Elegoo account allows one cloud session at a time.
- Reinstalling a plugin makes Orca delete its folder. The session and settings are kept outside it, so you do not
  have to sign in again after an update.

## Using the tab

- **Account**: signed-in user, printer picker, *Connect via cloud*, *Log out*, and the exact reason when the
  connection fails (printer offline in the cloud, account used by another client, expired session).
- **Controls stay grey** until the printer has answered. Motion needs an idle printer with the axis homed.
- **Camera** card: *S / M / L / Fit / Full screen*, or drag the bottom-right corner of the picture.
- **Files**: *Print* opens a panel to map each colour of the file to a Canvas slot; *Delete* asks first.
- **Theme** selector in the top right.

## Privacy and data

- **Sign-in**: the page is served to your browser through a loopback server on `127.0.0.1` started by the plugin, so
  your password passes through the plugin's memory on its way to Elegoo. It is never stored or logged. Only the
  session Elegoo returns is kept, encrypted with Windows DPAPI.
- **Where things are stored**: `<Orca data dir>/plugin_data/elegoo_cc2_cloud/` holds the encrypted session and the
  settings file.
- **What leaves the PC**: requests to the Elegoo cloud (`account.elegoo.com`, `matrix.elegoo.com`, its storage and
  Agora) and the two Agora SDKs loaded from `cdn.jsdelivr.net` when the tab opens.
- A separate *Use ElegooSlicer session* button can import the token of an installed ElegooSlicer instead (token
  only, read once).

## How it works

```
 OrcaSlicer 2.5.x
 ┌──────────────────────────────────────────────┐
 │ Printer Agent (Python)                       │──HTTPS──▶ matrix.elegoo.com   account, lists, tokens
 │   upload · status · commands · account       │──HTTPS──▶ Elegoo cloud storage (gcode upload)
 │                                              │
 │ "Elegoo CC2" tab (HTML + JS)                 │
 │   Agora RTM  ◀── bridge ──▶ commands, status │──RTM────▶ Elegoo cloud ──▶ Centauri Carbon 2
 │   Agora RTC  ── camera video ────────────────│──RTC────▶ (printer camera)
 └──────────────────────────────────────────────┘
     your browser ◀── 127.0.0.1 pass-through ──▶ Elegoo sign-in page
```

The cloud only accepts commands over Agora RTM and has no Python client, so the plugin runs the Agora web SDKs in
an Orca page and passes JSON to and from Python. Orca's audit hook does not cover `ctypes`, and its message
bridge ignores the Elegoo sign-in page's messages, hence the Windows HTTP and socket code. Details and the message
tables are in [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Known limits

- One cloud session per account: ElegooSlicer and this plugin cannot be connected together.
- The Agora SDKs load from a CDN without an integrity hash.
- Print history shows the raw status code reported by the printer; its meaning is not documented anywhere I found.
- Orca's plugin API is new and its printer-agent support is marked work in progress.
- Windows only (DPAPI, WinHTTP and Winsock through `ctypes`).

## Repository layout

```
assets/    icon (SVG, PNG, WebP, JPEG)
docs/      protocol notes
plugin/    the installable plugin
```

## Credits and disclaimer

The protocol was worked out from the public [`elegoo-link`](https://github.com/elegooofficial/elegoo-link) sources
(Apache-2.0), the OrcaSlicer plugin documentation, and logs and traffic from the author's own printer and slicer.
Plugin by [0xpraet0rian](https://0xpraet0rian.github.io/). Personal project, not affiliated with or endorsed by
Elegoo or the OrcaSlicer project. Use at your own risk: it can start prints, move axes and heat the printer.
