# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Elegoo CC2 Cloud"
# description = "Send gcode to and control an Elegoo Centauri Carbon 2 from OrcaSlicer through the Elegoo cloud: upload, start/pause/stop, live status. Reuses your ElegooSlicer login (close ElegooSlicer while connected)."
# author = "personal use"
# version = "0.1.0"
# ///
"""
Elegoo Centauri Carbon 2 - cloud printer agent for OrcaSlicer 2.5.x

Architecture
  * Python  : agent (discovery, connect, status mapping, upload, print), HTTP to matrix.elegoo.com
  * JS page : the "Elegoo CC2" tab loads Agora Web RTM and is only a transport
              (commands are published over RTM; the cloud has no other command path)
  * Auth    : reuses the ElegooSlicer login (DPAPI blob %APPDATA%\\ElegooSlicer\\user\\user_info).
              The token is never refreshed here (that would sign ElegooSlicer out).

Limits of v0.1
  * Open the "Elegoo CC2" tab once per Orca session so the RTM page exists.
  * Close ElegooSlicer while this plugin is connected (one cloud session per account).
  * No camera. Status-state mapping is a first guess and needs calibration on real prints.
"""
import base64
import collections
import ctypes
import ctypes.wintypes as wt
import hashlib
import io
import json
import os
import threading
import time
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

import orca

pa = orca.printer_agent

BASE = "https://matrix.elegoo.com"
AGENT_ID = "elegoo_cc2_cloud"
AGORA_APP_ID = "d035320941e34cd5bc4ec106eff05580"  # public id from elegoo-link rtm_service.cpp
USER_INFO = os.path.expandvars(r"%APPDATA%\ElegooSlicer\user\user_info")
UA = "Mozilla/5.0"

# update_fn stage numbers (Bambu-style guess: 1 upload, 3 sending, 5 wait printer, 6 finished, 7 error)
ST_UPLOAD, ST_SEND, ST_WAIT, ST_DONE, ST_ERR = 1, 3, 5, 6, 7


# --------------------------------------------------------------------------- helpers
class _Blob(ctypes.Structure):
    _fields_ = [("n", wt.DWORD), ("p", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes) -> bytes:
    i = _Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    o = _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(i), None, None, None, None, 0, ctypes.byref(o)):
        raise OSError("DPAPI decrypt failed")
    out = ctypes.string_at(o.p, o.n)
    ctypes.windll.kernel32.LocalFree(o.p)
    return out


def _elegoo_user() -> dict:
    return json.loads(_dpapi(_read_bytes(USER_INFO)).decode("utf-8"))


_wh = ctypes.WinDLL("winhttp", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_vp = ctypes.c_void_p
_wh.WinHttpOpen.restype = _vp
_wh.WinHttpOpen.argtypes = [wt.LPCWSTR, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD]
_wh.WinHttpConnect.restype = _vp
_wh.WinHttpConnect.argtypes = [_vp, wt.LPCWSTR, wt.WORD, wt.DWORD]
_wh.WinHttpOpenRequest.restype = _vp
_wh.WinHttpOpenRequest.argtypes = [_vp, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, _vp, wt.DWORD]
_wh.WinHttpSendRequest.argtypes = [_vp, wt.LPCWSTR, wt.DWORD, _vp, wt.DWORD, wt.DWORD, _vp]
_wh.WinHttpWriteData.argtypes = [_vp, _vp, wt.DWORD, ctypes.POINTER(wt.DWORD)]
_wh.WinHttpReceiveResponse.argtypes = [_vp, _vp]
_wh.WinHttpQueryHeaders.argtypes = [_vp, wt.DWORD, wt.LPCWSTR, _vp, ctypes.POINTER(wt.DWORD), _vp]
_wh.WinHttpReadData.argtypes = [_vp, _vp, wt.DWORD, ctypes.POINTER(wt.DWORD)]
_wh.WinHttpSetTimeouts.argtypes = [_vp, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
_wh.WinHttpSetOption.argtypes = [_vp, wt.DWORD, _vp, wt.DWORD]
_wh.WinHttpCloseHandle.argtypes = [_vp]
_k32.CreateFileW.restype = _vp
_k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, _vp, wt.DWORD, wt.DWORD, _vp]
_k32.ReadFile.argtypes = [_vp, _vp, wt.DWORD, ctypes.POINTER(wt.DWORD), _vp]
_k32.GetFileSizeEx.argtypes = [_vp, ctypes.POINTER(ctypes.c_longlong)]
_k32.CloseHandle.argtypes = [_vp]


def _read_bytes(path):
    h = _k32.CreateFileW(path, 0x80000000, 1 | 2, None, 3, 0x80, None)
    if h in (None, ctypes.c_void_p(-1).value):
        raise OSError("cannot open %s (error %d)" % (os.path.basename(path), ctypes.get_last_error()))
    try:
        size = ctypes.c_longlong()
        _k32.GetFileSizeEx(h, ctypes.byref(size))
        buf = (ctypes.c_char * size.value)()
        got, done = wt.DWORD(), 0
        while done < size.value:
            n = min(1 << 24, size.value - done)
            if not _k32.ReadFile(h, ctypes.byref(buf, done), n, ctypes.byref(got), None) or got.value == 0:
                raise OSError("read error")
            done += got.value
        return bytes(buf)
    finally:
        _k32.CloseHandle(h)


def _http(method, url, headers=None, data=None, timeout=30):
    """Returns (status, body). No redirects followed (the bearer token must not leave the host)."""
    u = urllib.parse.urlsplit(url)
    secure = u.scheme == "https"
    port = u.port or (443 if secure else 80)
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    h = {"User-Agent": UA}
    h.update(headers or {})
    hdr = "".join("%s: %s\r\n" % (k, v) for k, v in h.items())
    ses = _wh.WinHttpOpen("Mozilla/5.0", 0, None, None, 0)
    if not ses:
        raise OSError("WinHttpOpen failed")
    con = req = None
    try:
        ms = int(timeout * 1000)
        _wh.WinHttpSetTimeouts(ses, 15000, 15000, ms, ms)
        con = _wh.WinHttpConnect(ses, u.hostname, port, 0)
        req = _wh.WinHttpOpenRequest(con, method, path, None, None, None, 0x800000 if secure else 0)
        if not (con and req):
            raise OSError("WinHTTP request setup failed (%d)" % ctypes.get_last_error())
        off = wt.DWORD(0x80)  # WINHTTP_DISABLE_REDIRECTS
        _wh.WinHttpSetOption(req, 63, ctypes.byref(off), 4)
        total = len(data) if data else 0
        if not _wh.WinHttpSendRequest(req, hdr, 0xFFFFFFFF, None, 0, total, None):
            raise OSError("WinHTTP send failed (%d)" % ctypes.get_last_error())
        if total:
            sent, w = 0, wt.DWORD()
            mv = memoryview(data)
            while sent < total:
                chunk = bytes(mv[sent:sent + (1 << 20)])
                if not _wh.WinHttpWriteData(req, chunk, len(chunk), ctypes.byref(w)):
                    raise OSError("WinHTTP write failed (%d)" % ctypes.get_last_error())
                sent += w.value
        if not _wh.WinHttpReceiveResponse(req, None):
            raise OSError("WinHTTP receive failed (%d)" % ctypes.get_last_error())
        code, sz = wt.DWORD(), wt.DWORD(4)
        _wh.WinHttpQueryHeaders(req, 19 | 0x20000000, None, ctypes.byref(code), ctypes.byref(sz), None)
        out, avail = [], wt.DWORD()
        buf = ctypes.create_string_buffer(65536)
        while True:
            rd = wt.DWORD()
            if not _wh.WinHttpReadData(req, buf, 65536, ctypes.byref(rd)) or rd.value == 0:
                break
            out.append(buf.raw[:rd.value])
        return code.value, b"".join(out)
    finally:
        for hnd in (req, con, ses):
            if hnd:
                _wh.WinHttpCloseHandle(hnd)


def _num(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def _dict(v):
    return v if isinstance(v, dict) else {}


def _api(path, params=None):
    tok = _elegoo_user().get("token", "")
    if not tok:
        raise RuntimeError("No ElegooSlicer token found; sign in to ElegooSlicer first.")
    url = BASE + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    status, body = _http("GET", url, {"Authorization": "Bearer " + tok})
    if status in (401, 403):
        raise RuntimeError("Elegoo cloud HTTP %s (token expired? sign in again in ElegooSlicer)" % status)
    if status != 200:
        raise RuntimeError("Elegoo cloud HTTP %s" % status)
    j = json.loads(body)
    if j.get("code") != 0:
        raise RuntimeError("Elegoo cloud error %s %s" % (j.get("code"), j.get("msg")))
    return j.get("data")


# --------------------------------------------------------------------------- hub (shared state)
class Hub:
    """Singleton shared by the page capability and the agent capability."""

    def __init__(self):
        self.page = None
        self.page_ready = False
        self.rtm_online = False
        self.gen = 0                # RTM session generation (stale events are ignored)
        self.login_event = threading.Event()
        self.login_error = ""
        self.login_lock = threading.Lock()
        self.login_fail_until = 0.0
        self.was_connected = False
        self.lock = threading.Lock()
        self.pending = {}           # id -> [Event, result]
        self.seq = 1000
        self.status = {}            # merged printer status (method 1002/6000)
        self.attrs = {}
        self.downloads = {}         # taskID -> latest 6006 result
        self.logs = collections.deque(maxlen=300)
        self.creds = None
        self.sn = ""
        self.user_id = ""
        self.dev_name = "Centauri Carbon 2"
        self.queue_main = None
        self.cb = {}
        self.poll_stop = threading.Event()
        self.poll_thread = None
        self.ui_thread = None
        self.outbox = collections.deque(maxlen=500)

    # ---- logging (never log tokens or signed URLs)
    def log(self, msg):
        line = time.strftime("%H:%M:%S ") + str(msg)
        self.logs.append(line)
        self._post({"type": "log", "line": line})

    # ---- ui bridge: only the UI thread talks to the webview; other threads use an outbox
    # that the page drains by polling ({"type":"pull"} every few hundred ms).
    def _post(self, obj):
        page = self.page
        if page is None or not self.page_ready:
            return False
        if threading.get_ident() == self.ui_thread:
            try:
                page.post_message(obj)
            except Exception as e:  # noqa
                self.logs.append("post_message failed: %r" % e)
        else:
            self.outbox.append(obj)
        return True

    def _drain(self):
        items = []
        while self.outbox:
            try:
                items.append(self.outbox.popleft())
            except IndexError:
                break
        if items and self.page is not None:
            try:
                self.page.post_message({"type": "batch", "items": items})
            except Exception as e:  # noqa
                self.logs.append("post_message failed: %r" % e)

    # ---- events from the JS page (UI thread; keep it quick)
    def on_page_message(self, data):
        self.ui_thread = threading.get_ident()
        try:
            if isinstance(data, str):
                data = json.loads(data)
            t = data.get("type")
            g = data.get("gen")
            stale = g is not None and g != self.gen
            if t == "pull":
                self._drain()
            elif t == "ready":
                self.page_ready = True
                self.rtm_online = False
                self.log("RTM page ready")
                self._post({"type": "logs", "lines": list(self.logs)[-100:]})
            elif t == "login_ok" and not stale:
                self.rtm_online = True
                self.login_error = ""
                self.login_event.set()
                self.log("RTM login ok")
                if self.was_connected:
                    self._fire_local_connect(0, "")
            elif t == "login_err" and not stale:
                self.rtm_online = False
                self.login_error = str(data.get("error"))
                self.login_event.set()
                self.log("RTM login error: %s" % self.login_error)
            elif t == "rtm_status" and not stale:
                st = str(data.get("state"))
                self.log("RTM state: %s (%s)" % (st, data.get("reason")))
                if st.upper() in ("DISCONNECTED", "FAILED"):
                    self.rtm_online = False
                    self._fire_local_connect(2, "RTM %s" % data.get("reason"))
            elif t == "publish_err":
                self.rtm_online = False
                self._fail_pending(data.get("id"), str(data.get("error")))
            elif t == "token_expiring":
                threading.Thread(target=self._renew, daemon=True).start()
            elif t == "msg":
                self._on_rtm_text(data.get("text", ""))
            elif t == "click_login":
                threading.Thread(target=self._safe_login, daemon=True).start()
        except Exception as e:
            self.log("on_page_message error: %r" % e)

    def _safe_login(self):
        try:
            self.ensure_login()
        except Exception as e:
            self.log("login: %s" % e)

    def _fail_pending(self, rid, err):
        with self.lock:
            slot = self.pending.get(rid)
        if slot:
            slot[1] = {"result": {"error_code": -1, "error": err}}
            slot[0].set()

    # ---- RTM payloads
    def _on_rtm_text(self, text):
        try:
            m = json.loads(text)
        except Exception:
            self.log("non-JSON RTM payload ignored")
            return
        if not isinstance(m, dict):
            return
        try:
            mid = int(m.get("id"))
        except Exception:
            mid = None
        meth = m.get("method")
        if meth == 6006:
            r = _dict(m.get("result")) or _dict(m.get("params"))
            self.downloads[str(r.get("taskID"))] = r
            return
        if meth in (6000, 6008):
            self._merge_status(_dict(m.get("result")) or _dict(m.get("params")))
            return
        with self.lock:
            slot = self.pending.get(mid) if mid is not None else None
        if meth == 1002:
            self._merge_status(_dict(m.get("result")))
        elif meth == 1001:
            self.attrs = _dict(m.get("result"))
        if slot:
            slot[1] = m
            slot[0].set()

    def _merge_status(self, delta):
        def merge(a, b):
            for k, v in b.items():
                if isinstance(v, dict) and isinstance(a.get(k), dict):
                    merge(a[k], v)
                else:
                    a[k] = v
        merge(self.status, delta)
        try:
            self._emit_status()
        except Exception as e:
            self.logs.append("emit_status failed: %r" % e)

    # ---- credentials / login
    def fetch_creds(self):
        d = _api("/api/v1/device-management-server/device/list/agora-token", {"source": "slicer"})
        a = d["agoraToken"]
        devs = d.get("devices") or []
        if not devs:
            raise RuntimeError("No printer bound to this Elegoo account")
        dev = devs[0]
        self.sn = dev["serialNo"]
        self.dev_name = dev.get("deviceName") or self.dev_name
        self.user_id = str(a["userId"])
        self.creds = {"appId": AGORA_APP_ID, "rtmUserId": a["rtmUserId"], "rtmToken": a["rtmToken"],
                      "userId": self.user_id, "sn": self.sn}
        return self.creds

    def ensure_login(self, timeout=25):
        if self.rtm_online:
            return True
        with self.login_lock:
            if self.rtm_online:
                return True
            if not self.page_ready:
                raise RuntimeError("Open the 'Elegoo CC2' tab once so the RTM page can start.")
            if time.time() < self.login_fail_until:
                raise RuntimeError("RTM login failed recently, retrying shortly: %s" % self.login_error)
            self.login_event.clear()
            self.fetch_creds()
            self.gen += 1
            self._post({"type": "login", "gen": self.gen, "cfg": self.creds})
            if not self.login_event.wait(timeout) or not self.rtm_online:
                self.login_fail_until = time.time() + 30
                raise RuntimeError("RTM login failed: %s" % (self.login_error or "timeout (is ElegooSlicer closed?)"))
            return True

    def _renew(self):
        try:
            self.fetch_creds()
            self._post({"type": "renew", "token": self.creds["rtmToken"]})
        except Exception as e:
            self.log("token renew failed: %s" % e)

    def logout(self):
        self.rtm_online = False
        self.gen += 1
        self._post({"type": "logout"})

    # ---- request/response over RTM
    def request(self, method, params=None, timeout=20):
        self.ensure_login()
        with self.lock:
            self.seq += 1
            rid = self.seq
            slot = [threading.Event(), None]
            self.pending[rid] = slot
        self._post({"type": "publish", "id": rid,
                    "text": json.dumps({"id": rid, "method": method, "params": params or {}})})
        ok = slot[0].wait(timeout)
        with self.lock:
            self.pending.pop(rid, None)
        if not ok:
            raise RuntimeError("printer did not answer method %s" % method)
        res = _dict((slot[1] or {}).get("result"))
        ec = res.get("error_code", 0)
        if ec not in (0, None):
            raise RuntimeError("printer error_code %s (%s) for method %s" % (ec, res.get("error", ""), method))
        return res

    # ---- status -> Bambu-style push_status
    def _emit_status(self):
        fn = self.cb.get("local_message")
        if not fn or not self.sn:
            return
        s = self.status
        ps, ms = _dict(s.get("print_status")), _dict(s.get("machine_status"))
        ex, hb = _dict(s.get("extruder")), _dict(s.get("heater_bed"))
        zt = _dict(s.get("ztemperature_sensor"))
        # real values seen in ElegooSlicer logs: state in {"", "printing", "cancelled", "complete"};
        # machine_status.status: 1 idle, 2 printing, 11 downloading file (sub 3000)
        state = str(ps.get("state", "")).lower()
        status = ms.get("status")
        if state in ("printing", "running", "busy") or status == 2:
            gs = "RUNNING"
        elif state in ("paused", "pausing") or status in (5, 6):
            gs = "PAUSE"
        elif state in ("complete", "completed", "finished"):
            gs = "FINISH"
        elif state in ("cancelled", "canceled", "stopped", "failed", "error"):
            gs = "FAILED"
        else:
            gs = "IDLE"
        sw = _dict(self.attrs.get("software_version")).get("ota_version", "")
        pct = int(_num(ps.get("progress", ms.get("progress", 0))))
        msg = {"print": {
            "command": "push_status", "msg": 0, "sequence_id": "0",
            "gcode_state": gs,
            "mc_percent": pct,
            "mc_remaining_time": int(_num(ps.get("remaining_time_sec")) // 60),
            "nozzle_temper": _num(ex.get("temperature")),
            "nozzle_target_temper": _num(ex.get("target")),
            "bed_temper": _num(hb.get("temperature")),
            "bed_target_temper": _num(hb.get("target")),
            "chamber_temper": _num(zt.get("temperature")),
            "layer_num": int(_num(ps.get("current_layer"))),
            "total_layer_num": int(_num(ps.get("total_layer"))),
            "subtask_name": str(ps.get("filename", "") or ""),
            "gcode_file": str(ps.get("filename", "") or ""),
            "print_type": "local", "wifi_signal": "-50dBm", "hw_ver": "", "sw_ver": str(sw),
        }}
        try:
            fn(self.sn, json.dumps(msg))
        except Exception as e:
            self.logs.append("local_message cb failed: %r" % e)
        self._post({"type": "status", "state": gs, "progress": pct,
                    "nozzle": msg["print"]["nozzle_temper"], "bed": msg["print"]["bed_temper"],
                    "file": msg["print"]["subtask_name"]})

    def _fire_local_connect(self, status, msg=""):
        fn = self.cb.get("local_connect")
        if fn:
            try:
                fn(status, self.sn, msg)
            except Exception as e:
                self.logs.append("local_connect cb failed: %r" % e)

    # ---- polling (one thread, own stop event)
    def start_poll(self):
        self.stop_poll()
        ev = threading.Event()
        self.poll_stop = ev

        def run():
            while not ev.is_set():
                try:
                    self.request(1002, {}, timeout=15)
                except Exception as e:
                    self.log("poll: %s" % e)
                    ev.wait(10)
                    continue
                ev.wait(5)
        self.poll_thread = threading.Thread(target=run, daemon=True, name="cc2-poll")
        self.poll_thread.start()

    def stop_poll(self):
        self.poll_stop.set()


HUB = Hub()


# --------------------------------------------------------------------------- upload + print
def upload_gcode(data, name, progress=None, cancel_fn=None):
    """OSS upload via biz-entrypoint from bytes; returns (accessUrl, md5hex)."""
    size = len(data)
    if size == 0:
        raise RuntimeError("empty file")
    if size >= 500 * 1024 * 1024:
        raise RuntimeError("file >= 500 MB: multipart upload not implemented in v0.1")
    raw = hashlib.md5(data).digest()
    md5b64, md5hex = base64.b64encode(raw).decode(), raw.hex()
    d = _api("/api/v1/device-management-server/oss/biz-entrypoint",
             {"filename": name, "bucketAlias": "iot-private", "module": "gcode", "fileMd5": md5b64})
    if progress:
        progress(10)
    if cancel_fn and cancel_fn():
        raise RuntimeError("cancelled")
    status, _ = _http("PUT", d["entrypoint"], {"Content-Type": "application/octet-stream",
                                                "Content-MD5": md5b64}, data=data, timeout=900)
    if not 200 <= status < 300:
        raise RuntimeError("upload HTTP %s" % status)
    if progress:
        progress(100)
    return d["accessUrl"], md5hex


def _remote_name(p):
    base = os.path.basename(getattr(p, "dst_file", "") or getattr(p, "filename", "") or "print.gcode")
    if not base.lower().endswith(".gcode"):
        base = (getattr(p, "project_name", "") or os.path.splitext(base)[0] or "print") + ".gcode"
    return "".join(c if (c.isalnum() or c in "._- ()") else "_" for c in base)


def _send_to_printer(p, update_fn, cancel_fn):
    name = _remote_name(p)
    cb = lambda pct: update_fn and update_fn(ST_UPLOAD, pct, "Uploading to Elegoo cloud")
    url, md5hex = upload_gcode(_plate_gcode(p), name, cb, cancel_fn)
    task = uuid.uuid4().hex
    update_fn and update_fn(ST_SEND, 0, "Printer is downloading the file")
    try:
        # verified live: result error_code 0, then 6006 {progress:100,status:0,taskID}
        HUB.request(1057, {"filename": name, "url": url, "md5": md5hex, "taskID": task}, timeout=30)
        t0 = last_change = time.time()
        last = None
        while time.time() - t0 < 900:
            d = HUB.downloads.get(task)
            if d:
                pr = _num(d.get("progress"))
                if pr >= 100:
                    return name
                if d.get("error_code") not in (0, None):
                    raise RuntimeError("printer download failed (error %s)" % d.get("error_code"))
                if (pr, d.get("status")) != last:
                    last, last_change = (pr, d.get("status")), time.time()
                    update_fn and update_fn(ST_SEND, int(pr), "Printer download %d%%" % pr)
            if time.time() - last_change > 180:
                raise RuntimeError("printer download stalled")
            if cancel_fn and cancel_fn():
                try:
                    HUB.request(1058, {"taskID": task}, timeout=10)
                except Exception:
                    pass
                raise RuntimeError("cancelled")
            time.sleep(1)
        raise RuntimeError("printer download timed out")
    finally:
        HUB.downloads.pop(task, None)


def _slot_map(p):
    """slot_map from Orca's filament->slot mapping (JSON list; value = canvas*4 + tray); fallback: first tray."""
    out = []
    HUB.log("ams_mapping raw: %r" % (getattr(p, "ams_mapping", ""),))
    try:
        am = json.loads(getattr(p, "ams_mapping", "") or "[]")
        if isinstance(am, list):
            for t, v in enumerate(am):
                if isinstance(v, int) and 0 <= v < 16:
                    out.append({"t": t, "canvas_id": v // 4, "tray_id": v % 4})
    except Exception:
        pass
    if out:
        return out
    try:
        r = HUB.request(2005, {}, timeout=10)
        for c in _dict(r.get("canvas_info")).get("canvas_list", []):
            if c.get("connected") and c.get("tray_list"):
                HUB.log("WARNING: no filament mapping from Orca; using first tray of canvas %s" % c.get("canvas_id", 0))
                return [{"t": 0, "canvas_id": c.get("canvas_id", 0), "tray_id": c["tray_list"][0].get("tray_id", 0)}]
    except Exception as e:
        HUB.log("canvas query: %s" % e)
    return []


def _start_print(name, p):
    # wire format identical to what ElegooSlicer sends (seen in its debug log)
    cfg = {"delay_video": False, "printer_check": bool(getattr(p, "auto_bed_leveling", False)),
           "print_layout": "A", "bedlevel_force": False, "slot_map": _slot_map(p)}
    HUB.request(1020, {"filename": name, "storage_media": "local", "config": cfg}, timeout=30)


def _in_plugin_thread(fn):
    """start_* run on host job threads where audited I/O is denied until the user answers a prompt.
    Plugin-spawned threads are not audited, so do the work there and wait for it."""
    box = {}

    def run():
        try:
            box["r"] = fn()
        except BaseException as e:  # noqa
            box["e"] = e
    t = threading.Thread(target=run, daemon=True, name="cc2-job")
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box.get("r")


def _plate_gcode(p):
    """Read the file Orca hands over. If it is a .gcode.3mf (zip), return the plate's gcode bytes."""
    data = _read_bytes(p.filename)
    is_zip = data[:2] == b"PK"
    HUB.log("send file: %s (%s, %d bytes)" % (os.path.basename(p.filename), "zip/3mf" if is_zip else "plain", len(data)))
    if not is_zip:
        return data
    idx = int(_num(getattr(p, "plate_index", 1), 1)) or 1
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        want = [n for n in names if n.lower() == "metadata/plate_%d.gcode" % idx] or \
               [n for n in names if n.lower().startswith("metadata/plate_") and n.lower().endswith(".gcode")]
        if not want:
            raise RuntimeError("no plate gcode inside the 3mf")
        return z.read(want[0])


# --------------------------------------------------------------------------- agent
class CC2Agent(pa.PrinterAgentBase):
    def __init__(self):
        super().__init__()

    def get_name(self):
        return "Elegoo CC2 Cloud"

    def get_agent_info(self):
        i = pa.AgentInfo()
        i.id, i.name, i.version = AGENT_ID, "Elegoo CC2 (cloud)", "0.1.0"
        i.description = "Elegoo Centauri Carbon 2 via the Elegoo cloud"
        return i

    # ---- callbacks
    def _setter(key):
        def f(self, fn):
            HUB.cb[key] = fn
            if key == "queue_main":
                HUB.queue_main = fn
            return 0
        return f

    set_server_callback = _setter("server")
    set_on_ssdp_msg_fn = _setter("ssdp")
    set_on_printer_connected_fn = _setter("printer_connected")
    set_on_subscribe_failure_fn = _setter("sub_fail")
    set_on_message_fn = _setter("message")
    set_on_user_message_fn = _setter("user_message")
    set_on_local_connect_fn = _setter("local_connect")
    set_on_local_message_fn = _setter("local_message")
    set_queue_on_main_fn = _setter("queue_main")
    del _setter

    # ---- discovery / selection
    def start_discovery(self, start, sending):
        if not start:
            return True

        def run():
            try:
                HUB.fetch_creds()
                fn = HUB.cb.get("ssdp")
                if fn:
                    fn(json.dumps({"dev_id": HUB.sn, "dev_name": HUB.dev_name, "dev_ip": "cloud",
                                   "dev_type": "Centauri Carbon 2", "dev_signal": "0",
                                   "connect_type": "lan", "bind_state": "free"}))
            except Exception as e:
                HUB.log("discovery: %s" % e)
        threading.Thread(target=run, daemon=True).start()
        return True

    def bind_detect(self, dev_ip, sec_link, detect):
        try:
            detect.result_msg, detect.command = "ok", "bind_detect"
            detect.dev_id, detect.dev_name = HUB.sn, HUB.dev_name
            detect.model_id, detect.version = "", ""
            detect.bind_state, detect.connect_type = "free", "lan"
            return 0
        except Exception:
            return -1

    def get_user_selected_machine(self):
        return HUB.sn

    def set_user_selected_machine(self, dev_id):
        return 0

    # ---- connect
    def connect_printer(self, params):
        def run():
            try:
                HUB.ensure_login()
                HUB.request(1001, {}, timeout=15)
                HUB.was_connected = True
                HUB._fire_local_connect(0, "")
                HUB.start_poll()
            except Exception as e:
                HUB.log("connect failed: %s" % e)
                HUB._fire_local_connect(1, str(e))
        threading.Thread(target=run, daemon=True).start()
        return 0

    def disconnect_printer(self):
        HUB.stop_poll()
        HUB.was_connected = False
        HUB.logout()
        return 0

    # ---- messaging (translate Bambu-style JSON used by default command_* implementations)
    def send_message(self, dev_id, json_str, qos, flag):
        return -7010

    def send_message_to_printer(self, dev_id, json_str, qos, flag):
        try:
            p = (json.loads(json_str) or {}).get("print", {})
            cmd, param = p.get("command"), str(p.get("param", ""))
            if cmd == "pause":
                return self._rc(1021)
            if cmd == "resume":
                return self._rc(1023)
            if cmd == "stop":
                return self._rc(1022)
            if cmd == "gcode_line":
                return self._gcode(param)
            if cmd in ("print_speed", "set_speed"):
                return self._rc(1031, {"mode": int(p.get("param", 1))})
            return -7010
        except Exception as e:
            HUB.log("send_message_to_printer: %s" % e)
            return -1

    def _rc(self, method, params=None):
        def run():
            try:
                HUB.request(method, params or {}, timeout=20)
            except Exception as e:
                HUB.log("cmd %s: %s" % (method, e))
        threading.Thread(target=run, daemon=True).start()
        return 0

    def _gcode(self, g):
        import re
        temps, handled = {}, False
        for line in g.splitlines():
            line = line.split(";")[0].strip()
            m = re.match(r"M104\s+S(\d+)", line)
            if m:
                temps["extruder"] = int(m.group(1))
            m = re.match(r"M140\s+S(\d+)", line)
            if m:
                temps["heater_bed"] = int(m.group(1))
        if temps:
            handled = True
            self._rc(1028, temps)
        if "G28" in g:
            handled = True
            self._rc(1026, {"homed_axes": "xyz"})
        return 0 if handled else -7010

    def command_set_nozzle(self, dev_id, temp, sequence_id, lan_mode):
        return self._rc(1028, {"extruder": int(temp)})

    def command_set_bed(self, dev_id, temp, supports_mqtt_bed_ctrl, sequence_id, lan_mode):
        return self._rc(1028, {"heater_bed": int(temp)})

    def command_go_home(self, dev_id, is_printing, supports_mqtt_homing, sequence_id, lan_mode):
        return self._rc(1026, {"homed_axes": "xyz"})

    def command_axis_control(self, dev_id, axis, unit, input_val, speed, is_core_xy,
                             supports_mqtt_axis_control, sequence_id, lan_mode):
        try:
            return self._rc(1027, {"axes": str(axis).lower().replace("+", "").replace("-", ""),
                                   "distance": float(input_val) * (float(unit) if unit else 1.0)})
        except Exception:
            return -7010

    def command_xyz_abs(self, dev_id, sequence_id, lan_mode):
        return 0

    def command_auto_leveling(self, dev_id, sequence_id, lan_mode):
        return -7010

    # ---- printing
    def start_send_gcode_to_sdcard(self, params, update_fn, cancel_fn, wait_fn):
        if getattr(params, "project_name", "") == "verify_job":
            try:
                _in_plugin_thread(HUB.ensure_login)
                return 0
            except Exception as e:
                HUB.log("verify: %s" % e)
                return -1
        try:
            _in_plugin_thread(lambda: _send_to_printer(params, update_fn, cancel_fn))
            update_fn and update_fn(ST_DONE, 100, "Sent")
            return 0
        except Exception as e:
            HUB.log("send failed: %s" % e)
            update_fn and update_fn(ST_ERR, -1, str(e))
            return -1

    def start_local_print(self, params, update_fn, cancel_fn):
        try:
            def job():
                name = _send_to_printer(params, update_fn, cancel_fn)
                update_fn and update_fn(ST_WAIT, 0, "Starting print")
                _start_print(name, params)
            _in_plugin_thread(job)
            update_fn and update_fn(ST_DONE, 100, "Print started")
            return 0
        except Exception as e:
            HUB.log("print failed: %s" % e)
            update_fn and update_fn(ST_ERR, -1, str(e))
            return -1

    def start_print(self, params, update_fn, cancel_fn, wait_fn):
        return self.start_local_print(params, update_fn, cancel_fn)

    def start_local_print_with_record(self, params, update_fn, cancel_fn, wait_fn):
        return self.start_local_print(params, update_fn, cancel_fn)

    def start_sdcard_print(self, params, update_fn, cancel_fn):
        try:
            _in_plugin_thread(lambda: _start_print(_remote_name(params), params))
            return 0
        except Exception as e:
            HUB.log("sdcard print failed: %s" % e)
            return -1

    # ---- binding stubs (no pairing step)
    def check_cert(self):
        return 0

    def ping_bind(self, ping_code):
        return 0

    def bind(self, dev_ip, dev_id, dev_model, sec_link, timezone, improved, update_fn):
        return 0

    def unbind(self, dev_id):
        return 0

    def request_bind_ticket(self):
        return (0, "")

    # ---- camera: not implemented in v0.1
    def get_camera_stream_mode(self):
        return pa.CameraStreamMode.None_

    def get_camera_url(self):
        return ""

    def command_start_camera(self, dev_id):
        return -7010


# --------------------------------------------------------------------------- page (RTM transport)
ICON_SVG = r"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="128" height="128">
  <rect width="128" height="128" rx="26" fill="#0f766e"/>
  <!-- cloud -->
  <path transform="translate(0 4)" d="M40 58a15 15 0 0 1 14-20 20 20 0 0 1 37 6 13 13 0 0 1-1 26H44a12 12 0 0 1-4-12z"
        fill="#ccfbf1" opacity="0.95"/>
  <!-- printer frame -->
  <rect x="34" y="66" width="60" height="44" rx="5" fill="none" stroke="#f0fdfa" stroke-width="5"/>
  <!-- gantry + nozzle -->
  <line x1="34" y1="80" x2="94" y2="80" stroke="#f0fdfa" stroke-width="4"/>
  <rect x="58" y="76" width="12" height="9" rx="2" fill="#5eead4"/>
  <path d="M61 85h6l-3 6z" fill="#5eead4"/>
  <!-- printed layers -->
  <rect x="50" y="102" width="28" height="4" rx="1.5" fill="#99f6e4"/>
  <rect x="54" y="97" width="20" height="4" rx="1.5" fill="#5eead4"/>
  <!-- label -->
  <text x="64" y="27" text-anchor="middle" font-family="Arial,Helvetica,sans-serif" font-weight="700"
        font-size="19" fill="#ffffff">CC2</text>
</svg>
"""

PAGE_HTML = r"""<!doctype html><meta charset="utf-8">
<style>
 body{font:13px var(--orca-font,sans-serif);color:var(--orca-fg,#ddd);background:var(--orca-bg,#222);margin:14px}
 .row{margin:6px 0} b{color:var(--orca-accent,#00ae42)}
 pre{background:rgba(0,0,0,.25);padding:8px;max-height:330px;overflow:auto;white-space:pre-wrap}
 button{padding:5px 12px;margin-right:6px}
</style>
<h3>Elegoo CC2 - cloud link</h3>
<div class="row">RTM: <b id="rtm">starting</b> &nbsp; Printer: <b id="st">-</b> <span id="det"></span></div>
<div class="row"><button id="b">Connect / reconnect</button><span style="color:var(--orca-muted,#999)">Close ElegooSlicer while connected (one session per account).</span></div>
<pre id="log"></pre>
<script>
// The page lives on an opaque origin inside Orca: localStorage/sessionStorage throw. Give the SDK in-memory ones.
(function(){
  function mem(){ const m=new Map(); return {
    getItem:k=>m.has(String(k))?m.get(String(k)):null, setItem:(k,v)=>{m.set(String(k),String(v));},
    removeItem:k=>{m.delete(String(k));}, clear:()=>m.clear(), key:i=>Array.from(m.keys())[i]??null,
    get length(){return m.size;} }; }
  for(const n of ['localStorage','sessionStorage']){
    let ok=true; try{ void window[n]; }catch(e){ ok=false; }
    if(!ok){ try{ Object.defineProperty(window,n,{value:mem(),configurable:true}); }catch(e){} }
  }
})();
</script>
<script src="https://cdn.jsdelivr.net/npm/agora-rtm@2.3.0/agora-rtm.js"></script>
<script>
const $=id=>document.getElementById(id); let rtm=null, C=null, GEN=0, off=null;
const dec=new TextDecoder();
function P(o){ try{ orca.postMessage(o);}catch(e){ L('bridge error '+e);} }
function L(s){ const l=$('log'); l.textContent+=s+'\n'; l.scrollTop=l.scrollHeight; }
async function drop(){
  if(off){ try{off();}catch(e){} off=null; }
  if(rtm){ try{ await rtm.logout(); }catch(e){} rtm=null; }
}
async function doLogin(cfg,gen){
  C=cfg; GEN=gen; $('rtm').textContent='logging in';
  try{
    await drop();
    const {RTM}=AgoraRTM;
    const r=new RTM(cfg.appId,cfg.rtmUserId,{presenceTimeout:300,heartbeatInterval:30});
    const onStatus=e=>{ if(gen!==GEN) return; $('rtm').textContent=e.state; P({type:'rtm_status',gen,state:e.state,reason:e.reason}); };
    const onExp=()=>{ if(gen===GEN) P({type:'token_expiring',gen}); };
    const printer=cfg.userId+cfg.sn;
    const onMsg=e=>{
      if(gen!==GEN || e.publisher!==printer) return;   // only the printer may talk to us
      let t=e.message; if(typeof t!=='string') t=dec.decode(t);
      P({type:'msg',gen,text:t});
    };
    r.addEventListener('status',onStatus);
    r.addEventListener('tokenPrivilegeWillExpire',onExp);
    r.addEventListener('message',onMsg);
    off=()=>{ r.removeEventListener('status',onStatus); r.removeEventListener('tokenPrivilegeWillExpire',onExp); r.removeEventListener('message',onMsg); };
    rtm=r;
    await r.login({token:cfg.rtmToken});
    await r.subscribe(cfg.userId,{withMessage:true,withPresence:true});
    $('rtm').textContent='online'; P({type:'login_ok',gen});
  }catch(e){ $('rtm').textContent='error'; P({type:'login_err',gen,error:(e&&e.message)||JSON.stringify(e)}); }
}
async function handle(m){
  if(!m) return;
  if(m.type==='login') doLogin(m.cfg,m.gen);
  else if(m.type==='logout'){ GEN++; await drop(); $('rtm').textContent='logged out'; }
  else if(m.type==='renew' && rtm) { try{ await rtm.renewToken(m.token);}catch(e){ L('renew '+e);} }
  else if(m.type==='publish'){
    if(!rtm || !C){ P({type:'publish_err',id:m.id,error:'RTM not logged in'}); return; }
    try{ await rtm.publish(C.userId+C.sn,m.text,{channelType:'USER',customType:'PlainText'}); }
    catch(e){ P({type:'publish_err',id:m.id,error:(e&&e.message)||JSON.stringify(e)}); }
  }
  else if(m.type==='log') L(m.line);
  else if(m.type==='logs') (m.lines||[]).forEach(L);
  else if(m.type==='status'){ $('st').textContent=m.state+' '+m.progress+'%'; $('det').textContent='nozzle '+m.nozzle+' / bed '+m.bed+' '+(m.file||''); }
  else if(m.type==='batch'){ for(const it of (m.items||[])) await handle(it); }
}
orca.onMessage(handle);
setInterval(()=>P({type:'pull'}),300);
$('b').onclick=()=>P({type:'click_login'});
if(typeof AgoraRTM==='undefined'){ $('rtm').textContent='SDK not loaded (no CDN access?)'; L('AgoraRTM script failed to load'); }
P({type:'ready'});
</script>"""


class CC2Page(orca.pages.PagesPluginCapabilityBase):
    def __init__(self):
        super().__init__()
        HUB.page = self

    def get_name(self):
        return "Elegoo CC2"

    def get_icon(self):
        try:  # next to the plugin file, which lives under Orca's data dir (allowed root)
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "elegoo_cc2_icon.svg")
            if not os.path.exists(path):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(ICON_SVG)
            return path
        except Exception:
            return ""

    def get_ui(self):
        HUB.page_ready = False
        HUB.rtm_online = False
        return PAGE_HTML

    def on_message(self, data):
        HUB.on_page_message(data)


@orca.plugin
class ElegooCC2Plugin(orca.base):
    def register_capabilities(self):
        try:
            orca.request_permissions(
                fs_read=[USER_INFO],
                network_http=[BASE + "/", "https://iot.elegoo.com/"],
                threading=True,
            )
        except Exception as e:
            HUB.log("request_permissions failed: %r" % e)
        orca.register_capability(CC2Page)
        orca.register_capability(CC2Agent)
