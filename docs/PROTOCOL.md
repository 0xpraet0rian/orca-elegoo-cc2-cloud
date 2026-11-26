# Centauri Carbon 2, cloud protocol notes

What the plugin relies on. **V** means verified live against a real CC2, **L** means seen in ElegooSlicer's own
logs, **S** means read from the public `elegoo-link` source and not checked, **?** means a guess.

## 1. Account and HTTP API

Base URL `https://matrix.elegoo.com`. Every call sends `Authorization: Bearer <token>` and a normal
`User-Agent` (some storage hosts sit behind Cloudflare and answer `error code: 1010` to the default
Python user agent).

| Call | Purpose | |
|---|---|---|
| `GET /api/v1/account-center-server/account-auth/auth-check` | token is valid | V |
| `GET /api/v1/device-management-server/device/list` | bound printers (`serialNo`, `deviceName`) | V |
| `GET /api/v1/device-management-server/device/list/agora-token?source=slicer` | RTM credentials | V |
| `GET /api/v1/device-management-server/oss/biz-entrypoint` | pre-signed upload URL | V |

Where the token comes from: ElegooSlicer stores its session in
`%APPDATA%\ElegooSlicer\user\user_info`, a Windows DPAPI blob. Its JSON holds `token`, `refreshToken`,
`userId` and `region`. The plugin only reads `token`.

### Upload

1. `GET oss/biz-entrypoint?filename=<n>&bucketAlias=iot-private&module=gcode&fileMd5=<base64 md5>` returns
   `entrypoint` (PUT target) and `accessUrl` (signed download URL for the printer). V
2. `PUT <entrypoint>` with the raw bytes, `Content-Type: application/octet-stream` and
   `Content-MD5: <base64 md5>`. Any 2xx is success. V
3. Files of 500 MB or more use a multipart flow in `elegoo-link` (not implemented here). S

## 2. Commands over Agora RTM

The cloud has no HTTP or MQTT command path. Commands are JSON messages over Agora RTM 2.x. The web SDK
`agora-rtm@2.3.0` works with it. V

| Step | Detail |
|---|---|
| App ID | the public id hard-coded in `elegoo-link` (`rtm_service.cpp`) |
| Client | `new RTM(appId, rtmUserId)`, with `rtmUserId` from the Agora-token call |
| Login | `login({ token: rtmToken })`. The token must go here, not into the constructor (that gives error `-10005`) |
| Subscribe | channel `String(userId)`, `withMessage: true`, `withPresence: true` |
| Publish | channel `userId + serialNo`, `channelType: 'USER'`, `customType: 'PlainText'` |
| Replies | arrive as `Uint8Array` (UTF-8 JSON); the publisher id is `userId + serialNo` |
| Limit | one session per account: a second login with the same RTM user kicks the first |

Request: `{"id": <int>, "method": <int>, "params": {...}}`
Reply: `{"id": <int>, "method": <int>, "result": {"error_code": 0, ...}}`

### Methods

| Method | Meaning | Params | |
|---|---|---|---|
| `1001` | attributes | none | V |
| `1002` | full status | none | V |
| `1020` | start print | `{filename, storage_media:"local", config:{delay_video, printer_check, print_layout:"A", bedlevel_force, slot_map:[{t, canvas_id, tray_id}]}}` | L |
| `1021` | pause | none | L |
| `1022` | stop | none | L |
| `1023` | resume | none | S |
| `1026` | home | `{homed_axes:"xyz"}` | S |
| `1027` | move | `{axes, distance}` | S |
| `1028` | set temperature | `{extruder?, heater_bed?}` | L |
| `1031` | print speed | `{mode:<int>}` | L |
| `1044` | file list | `{storage_media:"local", path:"/", page, page_size}` | V |
| `1047` | delete file | `{storage_media:"local", file_path:["<name>"]}` | V |
| `1057` | printer downloads a file | `{filename, url, md5, taskID}` | V |
| `1058` | cancel download | `{taskID}` | S |
| `2005` | canvas (multi-colour) status | none | V |
| `6000` / `6008` | pushed status / attributes | event | S |
| `6006` | download progress | event `{taskID, progress, status}` | V |

Download sequence seen live, for both a 45-byte and a 5 MB file: reply `error_code 0`, then `6006` with
`progress 100, status 0`, then `6006` with `progress 0, status 1`. The plugin treats `progress >= 100` as done.

### Status fields

`machine_status.status`: `1` idle, `2` printing, `11` downloading (with `sub_status 3000`). Other pairs seen in
real logs: `13/1061`, `17/3046`, `15/0`, `9/0`.
`print_status.state` has only been seen as `""`, `printing`, `cancelled` and `complete`. **A paused value has
never been observed.**

## 3. LAN side (not used by the plugin)

- Discovery: UDP broadcast to port `52700` with `{"id":0,"method":7000}`. The printer replies with `host_name`,
  `machine_model`, `sn`, `lan_status` and `token_status`.
- The printer runs an MQTT broker on `1883`. It accepts user `elegoo` with the access code as password.
  In cloud mode (LAN Only off) it did not answer `api_register`, so the LAN path is untested and may need
  *LAN Only* to be switched on.
- Topics documented by the community: `elegoo/<sn>/api_register`, `elegoo/<sn>/<client>/api_request`,
  `elegoo/<sn>/<client>/api_response`, `elegoo/<sn>/api_status`.

## 4. OrcaSlicer plugin facts used

- File name must end with the platform, for example `_win_x86_64.py`.
- Metadata is a PEP 723 block with a `[tool.orcaslicer.plugin]` table.
- Printer agents subclass `orca.printer_agent.PrinterAgentBase` and are enabled by the experimental setting
  *Use printer agents instead of print hosts*.
- Plugin-spawned threads are not audited, while host job threads are and are denied on first access, so all
  network and file work runs in threads the plugin starts.
- The webview must only be touched from the UI thread. The plugin queues messages and the page pulls them.
