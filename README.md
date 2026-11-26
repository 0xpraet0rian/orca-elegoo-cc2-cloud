<p align="center">
  <img src="assets/icon-512.png" width="128" alt="Elegoo CC2 Cloud icon">
</p>

<h1 align="center">Elegoo CC2 Cloud</h1>

<p align="center">
  <b>OrcaSlicer plugin for the Elegoo Centauri Carbon 2, over the Elegoo cloud.</b><br>
  Send, start and watch prints from OrcaSlicer 2.5.x, without ElegooSlicer.
</p>

<p align="center">
  <img alt="OrcaSlicer" src="https://img.shields.io/badge/OrcaSlicer-2.5.x-0f766e?style=flat-square">
  <img alt="Printer" src="https://img.shields.io/badge/printer-Centauri%20Carbon%202-134e4a?style=flat-square">
  <img alt="Python" src="https://img.shields.io/badge/python-3.12%2B-3776ab?style=flat-square">
  <img alt="Dependencies" src="https://img.shields.io/badge/dependencies-none-5eead4?style=flat-square">
  <img alt="Status" src="https://img.shields.io/badge/status-v0.1%20experimental-f59e0b?style=flat-square">
</p>

---

## Features

| | |
|---|---|
| **Upload** | Slice in Orca, press *Send*, and the gcode goes to your Elegoo cloud storage and then to the printer |
| **Print control** | Start, pause and stop; set nozzle and bed temperature; home axes |
| **Live status** | Progress, layers, temperatures and remaining time shown in Orca |
| **Multi-colour** | Filament slots are taken from Orca's mapping and sent as the printer's `slot_map` |
| **Own sign-in** | Signs in to your Elegoo account with Elegoo's own page, in your browser, through a local pass-through; the session is stored encrypted (Windows DPAPI) |
| **Control tab** | Print, temperature, motion, fans, light, Canvas filament, files, history and camera in one tab |
| **Theme** | Simple dark (default), Simple white, or the 0xpraet0rian style (navy and orange); saved between sessions |
| **Auto-refresh** | Filament slots, files and print history refresh themselves after connecting, when a print ends and every minute |
| **Camera** | Live view starts by itself when the printer connects (switch off with Auto-start); the picture window is resizable |
| **No dependencies** | One Python file; the standard library only |

## Status

This is an early, personal-use build. What has been checked against a real printer, and what has not:

| Area | State |
|---|---|
| Cloud HTTP: login check, device list, Agora token | verified |
| Commands over RTM: `1001`, `1002`, `1044`, `1057`, `1047` | verified |
| File upload to cloud storage and printer download | verified |
| Start-print message format (`1020`) | confirmed from ElegooSlicer's own log, not yet sent by the plugin |
| Running inside OrcaSlicer 2.5.2 | **not yet verified** |
| Pause state shown in Orca | guess, never observed |
| Filament slot decoding from `ams_mapping` | guess, logged on first run |
| Camera | not implemented |
| Files of 500 MB or more | not implemented |

## Install

1. In Orca: **Preferences → Developer → Experimental Features**, enable
   *Use printer agents instead of print hosts*.
2. **Plugins → Install local plugin**, choose
   [`plugin/elegoo_cc2_cloud_win_x86_64.py`](plugin/elegoo_cc2_cloud_win_x86_64.py)
   and accept the permission dialog.
3. In the CC2 printer preset, set **Printer Agent** to *Elegoo CC2 (cloud)*.
4. **Close ElegooSlicer.** The Elegoo account allows one cloud session at a time.
5. Open the **Elegoo CC2** tab once per Orca session. It shows `RTM: online` when the link is up.
6. Use *Send* from the Prepare tab.

> Orca requires the file name to end with the OS and architecture (`_win_x86_64`).
> Sign in from the **Elegoo CC2** tab. The sign-in page opens in your browser and is served through a loopback
> server on 127.0.0.1 started by the plugin, so your password passes through the plugin's memory on its way to
> Elegoo; it is never stored or logged, and only the session Elegoo returns is kept (encrypted with Windows DPAPI).
> The plugin is Windows-only. A separate *Use ElegooSlicer session* button can import the token of an installed
> ElegooSlicer instead.

## How it works

```
 OrcaSlicer 2.5.x
 ┌─────────────────────────────────────────────┐
 │ Printer Agent (Python)                      │
 │   discovery · status mapping · upload       │──HTTPS──▶ matrix.elegoo.com   (account, device list, tokens)
 │                                             │──HTTPS──▶ Elegoo OSS storage  (gcode upload)
 │ "Elegoo CC2" tab (HTML + JS)                │
 │   Agora RTM web SDK  ◀── message bridge ──▶ │
 └──────────────┬──────────────────────────────┘
                │ RTM (commands and replies)
                ▼
          Elegoo cloud ───▶ Centauri Carbon 2
```

The cloud only accepts commands over Agora RTM, and there is no Python RTM client, so the plugin runs the
RTM web SDK inside an Orca page and passes JSON to and from it. Details and message tables are in
[docs/PROTOCOL.md](docs/PROTOCOL.md).

## Known limits

- One cloud session per account: ElegooSlicer and this plugin cannot be connected together.
- The tab must exist: open **Elegoo CC2** once after starting Orca.
- The Agora SDK is loaded from a CDN in the tab, without an integrity hash yet.
- The ElegooSlicer token is never refreshed by the plugin, because that would sign ElegooSlicer out.
  When it expires, open ElegooSlicer once to log in again.

## Repository layout

```
assets/    icon (SVG, PNG, WebP, JPEG)
docs/      protocol notes
plugin/    the installable plugin
```

## Credits and disclaimer

The protocol was worked out from the public [`elegoo-link`](https://github.com/elegooofficial/elegoo-link)
sources (Apache-2.0), the OrcaSlicer plugin documentation, and traffic and logs from the author's own printer
and slicer. Plugin by [0xpraet0rian](https://0xpraet0rian.github.io/). Personal project, not affiliated with or endorsed by Elegoo or the OrcaSlicer project.
Use at your own risk: it can start prints and heat the printer.
