# Centauri Carbon 2: cloud protocol notes

What the plugin relies on. Marks: **V** verified live against a real CC2, **O** copied from the parameters
ElegooSlicer's own Device page sends, **L** seen in ElegooSlicer's logs, **S** read from the public `elegoo-link`
source and not checked, **?** a guess.

## 1. Account and HTTP API

Base URL `https://matrix.elegoo.com`. Every call sends `Authorization: Bearer <token>` and a normal `User-Agent`
(some hosts sit behind Cloudflare and answer `error code: 1010` to the default Python user agent). Answers are JSON
`{code, msg, data}`; `code 0` is success, `401`, `30010` and `30012` mean the session is no longer valid.

| Call | Purpose | |
|---|---|---|
| `GET /api/v1/account-center-server/account-auth/auth-check` | token is valid | V |
| `POST .../account-auth/token/refresh` `{refreshToken, clientId:"Slicer"}` | new access token | S |
| `POST .../account-auth/logout` | end the session | S |
| `GET /api/v1/device-management-server/device/list` | bound printers (`serialNo`, `deviceName`) | V |
| `GET .../device/list/agora-token?source=slicer` | RTM and RTC credentials | V |
| `GET .../oss/biz-entrypoint` | pre-signed upload URL | V |
| `GET .../local-file/page?serialNo=&pageNo=&pageSize=` | files on the printer, as the cloud knows them | S |
| `GET .../device/event-data/page?deviceCode=&pageNo=&pageSize=&eventKey=history_task&sort=create_time,desc` | print history | S |
| `DELETE .../device-data/event-data?ids=a,b&deviceCode=` | delete history entries | S |

Files: `filename`, `printTime`, `layer`, `size`, `createTime`, `totalFilamentUsed`, `colorMap` (a JSON string of
`{color, t, type}`). History: each item has an `id` and an `eventValue` JSON string with `task_name`, `begin_time`,
`end_time`, `task_status` (a number whose meaning is not documented).

### Upload

1. `GET oss/biz-entrypoint?filename=<n>&bucketAlias=iot-private&module=gcode&fileMd5=<base64 md5>` returns
   `entrypoint` (PUT target) and `accessUrl` (signed download URL for the printer). V
2. `PUT <entrypoint>` with the raw bytes, `Content-Type: application/octet-stream` and `Content-MD5: <base64 md5>`.
   Any 2xx is success. V
3. Files of 500 MB or more use a multipart flow in `elegoo-link` (not implemented here). S

## 2. Signing in

The Elegoo account has no password API for third-party clients. The official page
`https://account.elegoo.com/account/slicer-login?region=global&theme=<light|dark>&deviceId=<id>` is meant to run
inside a slicer window and hands the session to the host application:

- **Client detection**: the page reads `navigator.userAgent`. If it contains `ElegooSlicer` it behaves as a desktop
  client and talks to `window.nativeIpc` (`request(method, params, timeout)`, `sendEvent`, `onRequest`, `on`, `off`);
  in a plain browser it stays in "browser mode" and never reports the session.
- **Host requests from the page**: `report.getClientUserInfo`, `report.ready`, `report.notLogged`,
  `report.userInfo`, `report.loginFailed`, `report.websiteOpen {url}` (Google and Facebook sign-in open in the system
  browser), `report.refreshToken`, `report.skipLogin`.
- **`report.userInfo`** carries the session: `accessToken`, `refreshToken`, `expiresTime`, `userId`, `email`,
  `nickname` and more. The access token lived about seven days in tests. V
- OrcaSlicer's plugin windows ignore these messages (they only accept an `{channel:'orca', kind:'message', data}`
  envelope), so the plugin serves the page to the user's browser through a loopback server that overrides the user
  agent and provides `nativeIpc`.

## 3. Commands over Agora RTM

The cloud has no HTTP or MQTT command path. Commands are JSON messages over Agora RTM 2.x; the web SDK
`agora-rtm@2.3.0` works with it. V

| Step | Detail |
|---|---|
| App ID | the public id hard-coded in `elegoo-link` (`rtm_service.cpp`) |
| Client | `new RTM(appId, rtmUserId)`, with `rtmUserId` from the Agora-token call |
| Login | `login({ token: rtmToken })`. The token must go here, not into the constructor (that gives error `-10005`) |
| Subscribe | channel `String(userId)`, `withMessage: true`, `withPresence: true` |
| Publish | channel `userId + serialNo`, `channelType: 'USER'`, `customType: 'PlainText'` |
| Replies | arrive as `Uint8Array` (UTF-8 JSON); the publisher id is `userId + serialNo` |
| Errors | `-10027`: another client of the account is logged in; `-11033`: the printer is offline in the cloud |
| Limit | one session per account: a second login with the same RTM user excludes the first |

Request: `{"id": <int>, "method": <int>, "params": {...}}`
Reply: `{"id": <int>, "method": <int>, "result": {"error_code": 0, ...}}`

Replies that are too large (a long file list) are probably cut off by the RTM message size limit and arrive as
invalid JSON, so lists are read over HTTP or in small pages.

### Methods

| Method | Meaning | Params | |
|---|---|---|---|
| `1001` | attributes | none | V |
| `1002` | full status | none | V |
| `1020` | start print | `{filename, storage_media:"local", config:{delay_video, printer_check, print_layout:"A", bedlevel_force, slot_map:[{t, canvas_id, tray_id}]}}` | L |
| `1021` / `1022` / `1023` | pause / stop / resume | none | L / L / O |
| `1024` / `1025` | feed / retract the extruder | none | O |
| `1026` | home | `{homed_axes:"xyz"}` (any subset) | O |
| `1027` | move | `{axes:"x"\|"y"\|"z", distance:<mm, signed>}`; the page requires the axis to be homed | O |
| `1028` | target temperature | `{extruder:<C>}` or `{heater_bed:<C>}` | O |
| `1029` | light | `{power:0\|1}` | O |
| `1030` | fans | `{fan, box_fan, aux_fan}` as 0-255 (percent x 255 / 100); `fan` model, `box_fan` case, `aux_fan` assistance | O |
| `1031` | print speed | `{mode:0..3}`: Silent, Balanced, Sport, Ludicrous | O |
| `1044` | file list | `{storage_media:"local", path:"/", page, page_size}`; keep pages small | V |
| `1047` | delete file | `{storage_media:"local", file_path:["<name>"]}` | V |
| `1057` | printer downloads a file | `{filename, url, md5, taskID}` | V |
| `1058` | cancel download | `{taskID}` | S |
| `2001` / `2002` | load / unload Canvas filament | `{canvas_id, tray_id}` | O |
| `2004` | auto refill | `{auto_refill:<bool>}` | O |
| `2005` | Canvas status | none | V |
| `6006` | download progress (event) | `{taskID, progress, status}` | V |

Download sequence seen live, for a 45-byte and a 5 MB file: reply `error_code 0`, then `6006` with
`progress 100, status 0`, then `6006` with `progress 0, status 1`. The plugin treats `progress >= 100` as done.

### Status fields

`machine_status.status` (`sub_status` refines it), from the status table of the SDK:

| status | meaning | status | meaning |
|---|---|---|---|
| 0 | initializing | 8 | self check |
| 1 | idle | 9 | updating |
| 2 | printing | 10 | homing |
| 3, 4 | filament operation | 11 | receiving a file (`sub_status 3000`) |
| 5 | auto leveling | 12 | composing video |
| 6 | PID calibration | 13 | extruder operation |
| 7 | resonance test | 14 / 15 | emergency stop / power-loss recovery |

While `status` is 2: `sub_status` `2075` printing, `2501` pausing, `2502` and `2505` paused, `2401` and `2402`
resuming, `2503` stopping, `2504` stopped, `2077` finished, `1045` heating the nozzle, `1405` heating the bed,
`2801` homing, `2901` leveling, `1081`, `1082` and `1086` downloading. Pausing was never seen on a real print.

Other fields: `print_status` (`filename`, `state`, `progress`, layers, durations; `state` was only seen as `""`,
`printing`, `cancelled`, `complete`), `extruder` and `heater_bed` (`temperature`, `target`), `ztemperature_sensor`,
`fans.{fan,aux_fan,box_fan}.speed` (0-255), `gcode_move` (`x`, `y`, `z`, `speed_mode`), `tool_head.homed_axes`,
`led.status`.

## 4. Camera

The camera uses Agora RTC (not RTM), as in ElegooSlicer's Device page. V

- SDK: `agora-rtc-sdk-ng@4.22.0`; `createClient({mode:"live", codec:"vp8"})`.
- `join(appId, channel = printer serial number, rtcToken, uid = rtcUserId)`, then `setClientRole("host")`. The token
  and uid come from the same Agora-token call as the RTM credentials.
- The printer publishes a video track as a remote user once someone joins. Subscribe once per user, then
  `videoTrack.play(element, {fit:"contain"})`. Subscribing twice gives an `INVALID_OPERATION`.
- No separate start command was needed. One viewer per account.

## 5. LAN side (not used by the plugin)

- Discovery: UDP broadcast to port `52700` with `{"id":0,"method":7000}`. The printer replies with `host_name`,
  `machine_model`, `sn`, `lan_status` and `token_status`.
- The printer runs an MQTT broker on `1883`. It accepts user `elegoo` with the access code as password. In cloud
  mode it did not answer `api_register`, so the LAN path is untested.
- Topics documented by the community: `elegoo/<sn>/api_register`, `elegoo/<sn>/<client>/api_request`,
  `elegoo/<sn>/<client>/api_response`, `elegoo/<sn>/api_status`.

## 6. OrcaSlicer plugin facts used

- The file name must end with the platform, for example `_win_x86_64.py`; metadata is a PEP 723 block.
- Printer agents subclass `orca.printer_agent.PrinterAgentBase` and are enabled by the experimental setting
  *Use printer agents instead of print hosts*.
- Reinstalling a plugin deletes its whole folder, so persistent data lives in `<data_dir>/plugin_data/`.
- Orca's audit hook denies Python network and socket calls that were not granted, even from threads the plugin
  starts, and it compares full URLs exactly (signed URLs differ on every call). `ctypes` is not audited, so HTTP
  goes through WinHTTP and the sign-in server through Winsock.
- Plugin pages get `window.orca`, whose messages are wrapped in `{channel:'orca', kind:'message', data}`.
- The webview must only be touched from the UI thread. The plugin queues messages and the page pulls them.
- A plugin page has an opaque origin: `localStorage` throws, so the Agora SDK gets an in-memory shim.
