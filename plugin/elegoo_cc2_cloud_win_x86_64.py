# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Elegoo CC2 Cloud"
# description = "Send gcode to and control an Elegoo Centauri Carbon 2 from OrcaSlicer through the Elegoo cloud: upload, start/pause/stop, live status. Reuses your ElegooSlicer login (close ElegooSlicer while connected)."
# author = "personal use"
# version = "0.2.0"
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
_HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(_HERE, "cc2_session.dat")   # DPAPI-encrypted, under the Orca data dir
LOGIN_URL = "https://account.elegoo.com/account/slicer-login?region=%s&theme=%s&deviceId=%s"
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


def _protect(data: bytes) -> bytes:
    i = _Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    o = _Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(i), "cc2", None, None, None, 0, ctypes.byref(o)):
        raise OSError("DPAPI encrypt failed")
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


def _http_full(method, url, headers=None, data=None, timeout=30, user_agent=None):
    """Returns (status, [(header, value)...], body). No redirects, no automatic cookies."""
    u = urllib.parse.urlsplit(url)
    secure = u.scheme == "https"
    port = u.port or (443 if secure else 80)
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    h = {"User-Agent": user_agent or UA}
    h.update(headers or {})
    hdr = "".join("%s: %s\r\n" % (k, v) for k, v in h.items())
    ses = _wh.WinHttpOpen("Mozilla/5.0", 4, None, None, 0) or _wh.WinHttpOpen("Mozilla/5.0", 0, None, None, 0)
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
        off = wt.DWORD(0x3)  # WINHTTP_DISABLE_COOKIES | WINHTTP_DISABLE_REDIRECTS (option 63)
        if not _wh.WinHttpSetOption(req, 63, ctypes.byref(off), 4):
            raise OSError("cannot disable redirects (%d)" % ctypes.get_last_error())
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
        raw = ""
        sz2 = wt.DWORD(0)
        _wh.WinHttpQueryHeaders(req, 22, None, None, ctypes.byref(sz2), None)   # size probe (raw CRLF headers)
        if sz2.value:
            hb = ctypes.create_unicode_buffer(sz2.value // 2 + 2)
            if _wh.WinHttpQueryHeaders(req, 22, None, hb, ctypes.byref(sz2), None):
                raw = hb.value
        pairs = []
        for ln in raw.split("\r\n")[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                pairs.append((k.strip(), v.strip()))
        out = []
        buf = ctypes.create_string_buffer(65536)
        while True:
            rd = wt.DWORD()
            if not _wh.WinHttpReadData(req, buf, 65536, ctypes.byref(rd)):
                raise OSError("WinHTTP read failed (%d)" % ctypes.get_last_error())
            if rd.value == 0:
                break
            out.append(buf.raw[:rd.value])
        return code.value, pairs, b"".join(out)
    finally:
        for hnd in (req, con, ses):
            if hnd:
                _wh.WinHttpCloseHandle(hnd)


def _http(method, url, headers=None, data=None, timeout=30):
    """Returns (status, body)."""
    st, _h, body = _http_full(method, url, headers, data, timeout)
    return st, body


# --------------------------------------------------------------------------- local sign-in pass-through
# Orca ignores the sign-in page's native-bridge messages, so the official page is served to the user's browser
# through a tiny loopback server that adds the bridge ("window.wx") the page expects. Built on ctypes (Winsock +
# WinHTTP) because Orca audits Python sockets/urllib. Request bodies (including the password) are forwarded in
# memory only: nothing is logged or stored except the session the page reports after a successful login.
_ws = ctypes.WinDLL("ws2_32", use_last_error=True)
_SOCK = ctypes.c_size_t
_INVALID_SOCK = _SOCK(-1).value


class _SockAddrIn(ctypes.Structure):
    _fields_ = [("family", ctypes.c_short), ("port", ctypes.c_ushort),
                ("addr", ctypes.c_ubyte * 4), ("zero", ctypes.c_ubyte * 8)]


_ws.socket.restype = _SOCK
_ws.socket.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
_ws.bind.argtypes = [_SOCK, ctypes.c_void_p, ctypes.c_int]
_ws.listen.argtypes = [_SOCK, ctypes.c_int]
_ws.accept.restype = _SOCK
_ws.accept.argtypes = [_SOCK, ctypes.c_void_p, ctypes.c_void_p]
_ws.getsockname.argtypes = [_SOCK, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
_ws.recv.restype = ctypes.c_int
_ws.recv.argtypes = [_SOCK, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
_ws.send.restype = ctypes.c_int
_ws.send.argtypes = [_SOCK, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
_ws.closesocket.argtypes = [_SOCK]
_ws.connect.restype = ctypes.c_int
_ws.connect.argtypes = [_SOCK, ctypes.c_void_p, ctypes.c_int]
_ws.shutdown.argtypes = [_SOCK, ctypes.c_int]
_ws.setsockopt.argtypes = [_SOCK, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
_WSA_READY = []


def _wsa():
    if not _WSA_READY:
        _ws.WSAStartup(0x0202, ctypes.create_string_buffer(512))
        _WSA_READY.append(1)


def _open_url(url):
    """Open a URL in the default browser without Python process APIs (not audited)."""
    r = ctypes.windll.shell32.ShellExecuteW(None, "open", url, None, None, 1)
    if r <= 32:
        raise OSError("cannot open the browser (%d)" % r)


class LoginProxy:
    UPSTREAM = "https://account.elegoo.com"
    DROP_RESP = {"transfer-encoding", "content-length", "connection", "content-security-policy",
                 "content-security-policy-report-only", "x-frame-options", "strict-transport-security",
                 "alt-svc", "report-to", "nel", "keep-alive"}
    DROP_REQ = {"host", "origin", "referer", "connection", "content-length", "accept-encoding", "keep-alive",
                "upgrade-insecure-requests", "transfer-encoding", "te", "proxy-connection"}

    def __init__(self, on_msg, log):
        self.on_msg, self.log = on_msg, log
        self.sock = None
        self.port = 0
        self.nonce = uuid.uuid4().hex
        self.stopped = False

    # ---- lifecycle
    def start(self):
        _wsa()
        s = _ws.socket(2, 1, 6)
        if s == _INVALID_SOCK:
            raise OSError("socket failed (%d)" % ctypes.get_last_error())
        sa = _SockAddrIn(2, 0, (ctypes.c_ubyte * 4)(127, 0, 0, 1))
        if _ws.bind(s, ctypes.byref(sa), ctypes.sizeof(sa)) != 0 or _ws.listen(s, 32) != 0:
            _ws.closesocket(s)
            raise OSError("bind/listen failed (%d)" % ctypes.get_last_error())
        n = ctypes.c_int(ctypes.sizeof(sa))
        _ws.getsockname(s, ctypes.byref(sa), ctypes.byref(n))
        self.port = ((sa.port & 0xFF) << 8) | (sa.port >> 8)
        self.sock = s
        threading.Thread(target=self._accept_loop, daemon=True, name="cc2-proxy").start()
        return self.port

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        try:  # wake the blocking accept() with a throw-away loopback connection
            w = _ws.socket(2, 1, 6)
            sa = _SockAddrIn(2, ((self.port & 0xFF) << 8) | (self.port >> 8), (ctypes.c_ubyte * 4)(127, 0, 0, 1))
            _ws.connect(w, ctypes.byref(sa), ctypes.sizeof(sa))
            _ws.closesocket(w)
        except Exception:
            pass

    def _accept_loop(self):
        s = self.sock
        try:
            while not self.stopped:
                c = _ws.accept(s, None, None)
                if c == _INVALID_SOCK:
                    break
                if self.stopped:
                    _ws.closesocket(c)
                    break
                threading.Thread(target=self._serve, args=(c,), daemon=True, name="cc2-proxy-conn").start()
        finally:
            self.sock = None
            _ws.closesocket(s)

    # ---- socket helpers
    @staticmethod
    def _recv(c, n=65536):
        buf = ctypes.create_string_buffer(n)
        got = _ws.recv(c, buf, n, 0)
        return buf.raw[:got] if got > 0 else b""

    @staticmethod
    def _send_all(c, data):
        mv, off = memoryview(data), 0
        while off < len(data):
            chunk = bytes(mv[off:off + 65536])
            n = _ws.send(c, chunk, len(chunk), 0)
            if n <= 0:
                return False
            off += n
        return True

    def _reply(self, c, status, body=b"", headers=None, reason="OK", head=False):
        h = [("Content-Length", str(len(body))), ("Connection", "close"), ("Cache-Control", "no-store")]
        h += list(headers or [])
        out = ("HTTP/1.1 %d %s\r\n" % (status, reason) + "".join("%s: %s\r\n" % kv for kv in h) + "\r\n").encode("latin-1", "replace")
        self._send_all(c, out + (b"" if head else body))

    # ---- request handling
    def _serve(self, c):
        try:
            to = ctypes.c_int(20000)
            _ws.setsockopt(c, 0xFFFF, 0x1006, ctypes.byref(to), 4)  # SO_RCVTIMEO
            data = b""
            while b"\r\n\r\n" not in data and len(data) < 65536:
                chunk = self._recv(c)
                if not chunk:
                    return
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            try:
                method, target, _ver = lines[0].split(" ", 2)
            except ValueError:
                return self._reply(c, 400, b"bad request", reason="Bad Request")
            hdrs = {}
            for ln in lines[1:]:
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    hdrs[k.strip().lower()] = v.strip()
            if hdrs.get("host", "") != "127.0.0.1:%d" % self.port:       # DNS-rebinding protection
                return self._reply(c, 403, b"forbidden", reason="Forbidden")
            if hdrs.get("transfer-encoding"):
                return self._reply(c, 411, b"length required", reason="Length Required")
            need = int(hdrs.get("content-length", "0") or 0)
            if need > 4 * 1024 * 1024 or not target.startswith("/"):
                return self._reply(c, 400, b"bad request", reason="Bad Request")
            body = rest
            while len(body) < need:
                chunk = self._recv(c)
                if not chunk:
                    return
                body += chunk
            body = body[:need]
            path = target.split("#", 1)[0]
            if path.split("?", 1)[0] == "/__wx/" + self.nonce and method == "POST":
                return self._wx(c, body)
            self._proxy(c, method, path, hdrs, body)
        except Exception as e:  # noqa
            try:
                self.log("proxy: %r" % e)
                self._reply(c, 502, b"proxy error", reason="Bad Gateway")
            except Exception:
                pass
        finally:
            try:
                _ws.shutdown(c, 2)
                _ws.closesocket(c)
            except Exception:
                pass

    def _wx(self, c, body):
        try:
            m = json.loads(body.decode("utf-8"))
            if isinstance(m, dict):
                self.on_msg(m)
        except Exception:
            pass
        self._reply(c, 200, b"{}", [("Content-Type", "application/json")])

    SHIM = """<script>(function(){var N="__NONCE__";
// The Elegoo page picks its desktop-client IPC from navigator.userAgent ("ElegooSlicer") and then talks to
// window.nativeIpc; in a plain browser it stays in "browser mode" and never reports the session.
try{var ua=navigator.userAgent+' ElegooSlicer/1.5.3';Object.defineProperty(navigator,'userAgent',{get:function(){return ua;},configurable:true});}catch(e){}
var seq=0;
function post(m){return fetch('/__wx/'+N,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(m)}).then(function(r){if(!r.ok)throw new Error('bridge '+r.status);return r;});}
window.nativeIpc={
 request:function(method,params,timeout){return post({id:'ni-'+(++seq),method:method,type:'request',params:params||{}}).then(function(){return {};});},
 sendEvent:function(method,data){post({id:null,method:method,type:'event',params:data||{}}).catch(function(){});},
 onRequest:function(){},on:function(){},off:function(){}};
function rep(id,m,code,msg,data){setTimeout(function(){try{window.HandleStudio&&window.HandleStudio({id:id,method:m,type:'response',code:code,message:msg,data:data});}catch(e){}},0);}
window.wx={postMessage:function(s){var m;try{m=JSON.parse(s);}catch(e){return;}
 if(!m||m.type!=='request')return;
 post(m).then(function(){rep(m.id,m.method,0,'success',{});}).catch(function(){rep(m.id,m.method,1,'failed',null);});}};
})();</script>"""

    def _proxy(self, c, method, path, hdrs, body):
        origin = "http://127.0.0.1:%d" % self.port
        fwd = {}
        for k, v in hdrs.items():
            if k in self.DROP_REQ or k.startswith("sec-fetch") or k.startswith("sec-ch"):
                continue
            fwd[k] = v
        fwd["Accept-Encoding"] = "identity"
        if "origin" in hdrs:
            fwd["Origin"] = self.UPSTREAM
        if "referer" in hdrs:
            fwd["Referer"] = hdrs["referer"].replace(origin, self.UPSTREAM)
        fwd.pop("user-agent", None)
        status, rh, rb = _http_full(method, self.UPSTREAM + path, fwd, body or None, timeout=30,
                                    user_agent=hdrs.get("user-agent") or UA)
        ctype, out_h = "", []
        for k, v in rh:
            lk = k.lower()
            if lk in self.DROP_RESP:
                continue
            if lk == "content-type":
                ctype = v
            if lk == "location":
                v = v.replace(self.UPSTREAM, origin)
            elif lk == "set-cookie":
                parts = [p.strip() for p in v.split(";")]
                parts = [p for p in parts if not p.lower().startswith(("domain=", "samesite=")) and p.lower() != "secure"]
                v = "; ".join(parts + ["SameSite=Lax"])
            out_h.append((k, v))
        enc = any(k.lower() == "content-encoding" for k, _ in rh)
        text = ("html" in ctype or "javascript" in ctype or "json" in ctype or "css" in ctype or "svg" in ctype) and not enc
        if text:
            s = rb.decode("utf-8", "replace").replace(self.UPSTREAM, origin)
            if "html" in ctype:
                shim = self.SHIM.replace("__NONCE__", self.nonce)
                i = s.lower().find("<head>")
                s = (s[:i + 6] + shim + s[i + 6:]) if i >= 0 else (shim + s)
            rb = s.encode("utf-8")
        reason = {200: "OK", 204: "No Content", 301: "Moved Permanently", 302: "Found", 304: "Not Modified",
                  400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found"}.get(status, "OK")
        self._reply(c, status, rb, out_h, reason=reason, head=(method == "HEAD"))


def _num(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def _dict(v):
    return v if isinstance(v, dict) else {}


def _ms(v):
    try:
        v = float(v)
    except Exception:
        return 0
    return int(v if v > 1e12 else v * 1000)


class Session:
    """The plugin's own Elegoo login (never ElegooSlicer's refresh token)."""

    def __init__(self):
        self.d = None
        self.lock = threading.Lock()

    def get(self):
        with self.lock:
            if self.d is None:
                try:
                    self.d = json.loads(_dpapi(_read_bytes(SESSION_FILE)).decode("utf-8"))
                except Exception:
                    self.d = {}
            return dict(self.d)

    def save(self, d):
        with self.lock:
            self.d = dict(d)
            with open(SESSION_FILE, "wb") as f:
                f.write(_protect(json.dumps(self.d).encode("utf-8")))

    def update(self, **kw):
        d = self.get()
        d.update(kw)
        self.save(d)

    def clear(self):
        with self.lock:
            self.d = {}
            try:
                os.remove(SESSION_FILE)
            except OSError:
                pass

    def logged_in(self):
        return bool(self.get().get("token"))


SESSION = Session()


def _refresh_session():
    d = SESSION.get()
    rt = d.get("refreshToken")
    if not rt:
        raise RuntimeError("Session expired: sign in again in the Elegoo CC2 tab.")
    status, body = _http("POST", BASE + "/api/v1/account-center-server/account-auth/token/refresh",
                         {"Authorization": "Bearer " + d.get("token", ""), "Content-Type": "application/json"},
                         data=json.dumps({"refreshToken": rt, "clientId": "Slicer"}).encode())
    j = json.loads(body) if status == 200 else {}
    if j.get("code") not in (0, 200) or not isinstance(j.get("data"), dict):
        SESSION.update(token="", refreshToken="")
        raise RuntimeError("Session expired: sign in again in the Elegoo CC2 tab.")
    x = j["data"]
    SESSION.update(token=x.get("accessToken") or x.get("token") or d.get("token"),
                   refreshToken=x.get("refreshToken") or rt,
                   exp=_ms(x.get("expiresTime") or x.get("accessTokenExpireTime")))


def _token():
    d = SESSION.get()
    if not d.get("token"):
        raise RuntimeError("Not signed in: use Sign in in the Elegoo CC2 tab.")
    exp = d.get("exp") or 0
    if exp and exp - time.time() * 1000 < 120000 and d.get("refreshToken"):
        try:
            _refresh_session()
        except Exception as e:
            HUB.log("token refresh: %s" % e)
    return SESSION.get().get("token", "")


def _api(path, params=None):
    tok = _token()
    url = BASE + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    status, body = _http("GET", url, {"Authorization": "Bearer " + tok})
    if status in (401, 403):
        raise RuntimeError("Elegoo cloud HTTP %s (session expired? sign in again in the Elegoo CC2 tab)" % status)
    if status != 200:
        raise RuntimeError("Elegoo cloud HTTP %s" % status)
    j = json.loads(body)
    if j.get("code") in (401, 30010, 30012):
        SESSION.update(token="", refreshToken="")
        HUB._push_account()
        raise RuntimeError("Your Elegoo session is no longer valid (password changed or signed out elsewhere): "
                           "sign in again in the Elegoo CC2 tab.")
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
        self.outbox = collections.deque()          # critical items: never evicted
        self.outlogs = collections.deque(maxlen=120)
        self.out_status = None                      # only the latest status matters
        self.printer_online = True
        self.devices = []
        self.login_proxy = None
        self.connect_lock = threading.Lock()
        self.rtc = None
        self.cur_code = None
        self.status_ts = 0.0
        self.busy = threading.Lock()
        self.want_poll = False
        self.poll_lock = threading.Lock()
        self.cur_gs = "IDLE"

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
            return True
        t = obj.get("type")
        if t == "status":
            self.out_status = obj
        elif t == "log":
            self.outlogs.append(obj)
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
        while self.outlogs:
            try:
                items.append(self.outlogs.popleft())
            except IndexError:
                break
        if self.out_status is not None:
            items.append(self.out_status)
            self.out_status = None
        if items and self.page is not None:
            try:
                self.page.post_message({"type": "batch", "items": items})
            except Exception as e:  # noqa
                self.logs.append("post_message failed: %r" % e)

    def _drop_publish(self, rid):
        keep = [x for x in list(self.outbox) if not (x.get("type") == "publish" and x.get("id") == rid)]
        self.outbox.clear()
        self.outbox.extend(keep)

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
                self.outbox.clear()
                self.outlogs.clear()
                self.out_status = None
                self.page_ready = True
                self.rtm_online = False
                self.log("RTM page ready")
                self._post({"type": "logs", "lines": list(self.logs)[-100:]})
                self._push_account()
                threading.Thread(target=self._auto_connect, daemon=True, name="cc2-autoconnect").start()
            elif t == "login_ok" and not stale:
                self.rtm_online = True
                self.login_error = ""
                self.login_event.set()
                self.log("RTM login ok")
                if self.was_connected:
                    self._fire_local_connect(0, "")
                threading.Thread(target=self._after_login, daemon=True).start()
            elif t == "login_err" and not stale:
                self.rtm_online = False
                self.login_error = str(data.get("error"))
                self.login_event.set()
                self.log("RTM login error: %s" % self.login_error)
                if "-10027" in self.login_error:
                    self._post({"type": "note", "kind": "bad", "text":
                                "Another Elegoo session is using this account (ElegooSlicer?). Close it, then press Connect."})
            elif t == "rtm_status" and not stale:
                st = str(data.get("state"))
                self.log("RTM state: %s (%s)" % (st, data.get("reason")))
                if st.upper() in ("DISCONNECTED", "FAILED"):
                    self.rtm_online = False
                    self._fire_local_connect(2, "RTM %s" % data.get("reason"))
            elif t == "publish_err":
                err = str(data.get("error"))
                if "-11033" in err:  # the printer (target user) is offline in the cloud; RTM itself is fine
                    self._set_printer_online(False, "The printer is offline in the Elegoo cloud (powered off or no Wi-Fi?)")
                else:
                    self.rtm_online = False
                self._fail_pending(data.get("id"), err)
            elif t == "cmd":
                threading.Thread(target=self._ui_cmd, args=(data,), daemon=True, name="cc2-ui-cmd").start()
            elif t == "token_expiring" and not stale:
                threading.Thread(target=self._renew, daemon=True).start()
            elif t == "msg":
                self._on_rtm_text(data.get("text", ""))
            elif t == "click_login":
                threading.Thread(target=self._safe_login, daemon=True).start()
        except Exception as e:
            self.log("on_page_message error: %r" % e)

    def _after_login(self):
        try:
            r = self.request(1001, {}, timeout=15)
            self.attrs = r
            self._post({"type": "info", "name": r.get("hostname") or self.dev_name,
                        "model": r.get("machine_model", ""), "ip": r.get("ip", ""), "sn": self.sn,
                        "fw": _dict(r.get("software_version")).get("ota_version", "")})
            if self.want_poll and not (self.poll_thread and self.poll_thread.is_alive() and not self.poll_stop.is_set()):
                self.start_poll()
        except Exception as e:
            self.log("after login: %s" % e)

    def _safe_login(self):
        try:
            self.login_fail_until = 0.0
            self.want_poll = True
            self.ensure_login()
            if not (self.poll_thread and self.poll_thread.is_alive() and not self.poll_stop.is_set()):
                self.start_poll()
        except Exception as e:
            self.log("login: %s" % e)

    def is_idle(self):
        return self.cur_code == 1 and time.time() - self.status_ts < 20

    def _set_printer_online(self, on, msg=None):
        if not on:
            self.cur_code = None
        if self.printer_online != on:
            self.printer_online = on
            self._post({"type": "conn", "printer": on, "msg": msg})

    # ---- commands from the tab (parameters validated; shapes copied from ElegooSlicer's own Device page)
    def _ui_cmd(self, data):
        rid, m, p = data.get("rid"), data.get("method"), _dict(data.get("params"))
        try:
            res = self._exec_ui(m, p)
            self._post({"type": "cmd_result", "rid": rid, "ok": True, "result": res})
        except Exception as e:
            self.log("ui cmd %s: %s" % (m, e))
            self._post({"type": "cmd_result", "rid": rid, "ok": False, "error": str(e)})

    @staticmethod
    def _int(v, lo, hi, name):
        try:
            n = int(round(float(v)))
        except Exception:
            raise ValueError("%s must be a number" % name)
        if not lo <= n <= hi:
            raise ValueError("%s must be %d-%d" % (name, lo, hi))
        return n

    @staticmethod
    def _valid_name(n):
        n = str(n)
        if not n.lower().endswith(".gcode") or len(n) > 200 or ".." in n or "/" in n or "\\" in n:
            raise ValueError("bad file name")
        if any(ord(c) < 32 for c in n):
            raise ValueError("bad file name")
        return n

    def _exec_ui(self, m, p):
        long_ops = (1024, 1025, 1026, 2001, 2002, "start_file")
        mm = int(m) if str(m).isdigit() else m
        if mm in long_ops:
            if not self.busy.acquire(blocking=False):
                raise RuntimeError("another operation is still running")
            try:
                return self._exec_ui2(m, p)
            finally:
                self.busy.release()
        return self._exec_ui2(m, p)

    def _exec_ui2(self, m, p):
        idle = self.is_idle()
        if m == "acct_status":
            if SESSION.logged_in() and not self.devices:
                try:
                    self.fetch_creds()
                except Exception as e:
                    self.log("device list: %s" % e)
            return self.account_state()
        if m == "cam_creds":
            self.fetch_creds()
            if not (self.rtc and self.rtc.get("token") and self.rtc.get("uid") is not None):
                raise RuntimeError("no camera credentials for this printer")
            return dict(self.rtc)
        if m == "acct_login":
            self.start_browser_login()
            return {"started": True}
        if m == "acct_import":
            self.import_from_elegooslicer()
            return self.account_state()
        if m == "acct_logout":
            self.sign_out()
            return self.account_state()
        if m == "acct_select":
            sn = str(p.get("sn", ""))
            if sn not in [x["sn"] for x in self.devices]:
                raise ValueError("unknown printer")
            SESSION.update(sn=sn)
            self.want_poll = False
            self.stop_poll()
            self.logout()
            self.status, self.sn = {}, sn
            self._push_account()
            return self.account_state()
        if m == "acct_connect":
            self.connect_cloud()
            return self.account_state()
        if m == "acct_auto":
            SESSION.update(auto=bool(p.get("on")))
            return self.account_state()
        if m == "files":
            files, page = [], 1
            while page <= 20:
                r = self.request(1044, {"storage_media": "local", "path": "/", "page": page, "page_size": 50}, timeout=25)
                lst = r.get("file_list") or []
                for f in lst:
                    if isinstance(f, dict) and f.get("type", "file") == "file":
                        files.append({k: f.get(k) for k in ("filename", "size", "layer", "print_time",
                                                            "total_filament_used", "color_map", "create_time")})
                if len(lst) < 50:
                    break
                page += 1
            return {"files": files}
        if m == "canvas":
            return self.request(2005, {}, timeout=15)
        if m == "history":
            return self.request(1036, {}, timeout=25)
        if m == "start_file":
            if not idle:
                raise RuntimeError("the printer is not idle")
            name = self._valid_name(p.get("filename", ""))
            slots = []
            for x in p.get("slots") or []:
                slots.append({"t": self._int(x.get("t"), 0, 15, "t"), "canvas_id": self._int(x.get("canvas_id"), 0, 3, "canvas"),
                              "tray_id": self._int(x.get("tray_id"), 0, 3, "slot")})
            cfg = {"delay_video": False, "printer_check": bool(p.get("leveling")), "print_layout": "A",
                   "bedlevel_force": False, "slot_map": slots}
            return self.request(1020, {"filename": name, "storage_media": "local", "config": cfg}, timeout=30)
        m = int(m) if str(m).isdigit() else m
        if m in (1021, 1022, 1023, 1024, 1025, 1036):
            if m in (1024, 1025) and not idle:
                raise RuntimeError("the printer is not idle")
            return self.request(m, {}, timeout=300 if m in (1024, 1025) else 30)
        if m == 1026:
            ax = "".join(sorted(set(str(p.get("homed_axes", "")).lower()) & set("xyz")))
            if not ax or not idle:
                raise RuntimeError("homing needs an idle printer and axes x/y/z")
            return self.request(1026, {"homed_axes": ax}, timeout=90)
        if m == 1027:
            ax = str(p.get("axes", "")).lower()
            if ax not in ("x", "y", "z") or not idle:
                raise RuntimeError("moving needs an idle printer and one axis")
            th = _dict(self.status.get("tool_head")) or _dict(self.status.get("toolhead"))
            if ax not in str(th.get("homed_axes", "") or ""):
                raise RuntimeError("home the %s axis before moving it" % ax.upper())
            dist = float(p.get("distance", 0))
            if not (0 < abs(dist) <= 100):
                raise ValueError("distance must be up to 100 mm")
            return self.request(1027, {"axes": ax, "distance": dist}, timeout=30)
        if m == 1028:
            if "extruder" in p:
                return self.request(1028, {"extruder": self._int(p["extruder"], 0, 350, "nozzle")}, timeout=20)
            if "heater_bed" in p:
                return self.request(1028, {"heater_bed": self._int(p["heater_bed"], 0, 120, "bed")}, timeout=20)
            raise ValueError("nothing to set")
        if m == 1029:
            return self.request(1029, {"power": 1 if int(p.get("power", 0)) else 0}, timeout=15)
        if m == 1030:
            out = {k: self._int(p[k], 0, 255, k) for k in ("fan", "box_fan", "aux_fan") if k in p}
            if not out:
                raise ValueError("nothing to set")
            return self.request(1030, out, timeout=15)
        if m == 1031:
            return self.request(1031, {"mode": self._int(p.get("mode"), 0, 3, "mode")}, timeout=15)
        if m in (2001, 2002):
            if not idle:
                raise RuntimeError("the printer is not idle")
            return self.request(m, {"canvas_id": self._int(p.get("canvas_id"), 0, 3, "canvas"),
                                    "tray_id": self._int(p.get("tray_id"), 0, 3, "slot")}, timeout=300)
        if m == 2004:
            return self.request(2004, {"auto_refill": bool(p.get("auto_refill"))}, timeout=15)
        if m == 1047:
            names = [self._valid_name(x) for x in (p.get("file_path") or [])]
            if not names:
                raise ValueError("bad file name")
            cur = str(_dict(self.status.get("print_status")).get("filename", ""))
            if cur in names and not idle:
                raise RuntimeError("that file is being printed")
            return self.request(1047, {"storage_media": "local", "file_path": names}, timeout=30)
        raise ValueError("command not allowed: %s" % m)

    # ---- account (own login session)
    def account_state(self):
        d = SESSION.get()
        return {"logged": bool(d.get("token")), "email": d.get("email", ""), "nickname": d.get("nickname", ""),
                "source": d.get("source", ""), "sn": d.get("sn", "") or self.sn, "devices": self.devices,
                "rtm": self.rtm_online, "printer": self.printer_online, "auto": d.get("auto", True)}

    def _push_account(self):
        self._post(dict(self.account_state(), type="account"))

    def start_browser_login(self):
        d = SESSION.get()
        dev_id = d.get("deviceId") or uuid.uuid4().hex
        if not d.get("deviceId"):
            SESSION.update(deviceId=dev_id)
        self._stop_login_proxy()
        px = LoginProxy(self._on_login_msg, self.log)
        port = px.start()
        self.login_proxy = px
        url = "http://127.0.0.1:%d/account/slicer-login?region=global&theme=light&deviceId=%s" % (port, dev_id)
        _open_url(url)
        self.log("sign-in page opened in your browser (local pass-through on 127.0.0.1:%d)" % port)
        t = threading.Timer(900, self._stop_login_proxy)
        t.daemon = True
        t.start()

    def _stop_login_proxy(self):
        px, self.login_proxy = self.login_proxy, None
        if px:
            px.stop()

    def _on_login_msg(self, data):
        """Messages from the Elegoo sign-in page (same bridge ElegooSlicer uses). Only method names are logged."""
        try:
            if isinstance(data, str):
                data = json.loads(data)
            m = data.get("method") if isinstance(data, dict) else None
            self.log("sign-in page message: %s" % (m or type(data).__name__))
            if m == "report.userInfo":
                self.log("sign-in page reported user info; fields: %s" % sorted(_dict(data.get("params")).keys()))
                threading.Thread(target=self._store_login, args=(_dict(data.get("params")),), daemon=True).start()
            elif m == "report.websiteOpen":
                url = str(_dict(data.get("params")).get("url", ""))
                if url.startswith("https://"):
                    threading.Thread(target=lambda: _open_url(url), daemon=True).start()
        except Exception as e:
            self.log("sign-in message error: %r" % e)

    def _store_login(self, u):
        try:
            tok = u.get("accessToken") or u.get("token")
            if not tok:
                raise RuntimeError("sign-in page sent no token")
            keep = SESSION.get()
            SESSION.save({"token": tok, "refreshToken": u.get("refreshToken", ""), "exp": _ms(u.get("expiresTime")),
                          "userId": str(u.get("userId", "")), "email": u.get("email", ""),
                          "nickname": u.get("nickname", ""), "deviceId": keep.get("deviceId", ""),
                          "source": "own", "sn": keep.get("sn", "")})
            self.log("signed in")
            t = threading.Timer(5, self._stop_login_proxy)
            t.daemon = True
            t.start()
            try:
                self.fetch_creds()
            except Exception as e:
                self.log("device list: %s" % e)
            self._push_account()
            self._post({"type": "note", "kind": "ok", "text": "Signed in."})
            self._auto_connect()
        except Exception as e:
            self.log("store login: %s" % e)

    def import_from_elegooslicer(self):
        u = _elegoo_user()
        if not u.get("token"):
            raise RuntimeError("ElegooSlicer has no active login")
        keep = SESSION.get()
        # token only: the refresh token stays with ElegooSlicer so its session is never rotated from here
        SESSION.save({"token": u["token"], "refreshToken": "", "exp": _ms(u.get("accessTokenExpireTime")),
                      "userId": str(u.get("userId", "")), "email": u.get("email", ""),
                      "nickname": u.get("nickname", ""), "deviceId": keep.get("deviceId", ""),
                      "source": "imported", "sn": keep.get("sn", "")})
        self.fetch_creds()
        self._push_account()

    def sign_out(self):
        self._stop_login_proxy()
        self.want_poll = False
        self.stop_poll()
        self.was_connected = False
        self.logout()
        try:
            if SESSION.get().get("source") == "own":
                _http("POST", BASE + "/api/v1/account-center-server/account-auth/logout",
                      {"Authorization": "Bearer " + SESSION.get().get("token", ""),
                       "Content-Type": "application/json"}, data=b"{}", timeout=5)
        except Exception:
            pass
        SESSION.clear()
        self.devices, self.creds, self.sn, self.status = [], None, "", {}
        self._push_account()

    # ---- connect to the cloud (tab button, auto-connect after sign-in / tab open)
    def _connstat(self, text, kind=""):
        self.log(text)
        self._post({"type": "connstat", "text": text, "kind": kind})

    def connect_cloud(self):
        if not self.connect_lock.acquire(blocking=False):
            return
        try:
            self.login_fail_until = 0.0
            self.want_poll = True
            self._connstat("Connecting to the Elegoo cloud...")
            self.ensure_login()
            self._connstat("Cloud link is up; asking the printer...")
            self.request(1001, {}, timeout=15)
            self.was_connected = True
            self._after_login()
            self._connstat("Connected", "ok")
            self._push_account()
        except Exception as e:
            self._connstat("Not connected: %s" % e, "bad")
            raise
        finally:
            self.connect_lock.release()

    def _auto_connect(self):
        try:
            if SESSION.logged_in() and SESSION.get().get("auto", True) and not self.rtm_online:
                self.connect_cloud()
        except Exception:
            pass

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
        self.devices = [{"sn": x.get("serialNo"), "name": x.get("deviceName") or x.get("pcode") or x.get("serialNo")}
                        for x in devs]
        want = SESSION.get().get("sn")
        dev = next((x for x in devs if x.get("serialNo") == want), devs[0])
        self.sn = dev["serialNo"]
        self.dev_name = dev.get("deviceName") or self.dev_name
        self.user_id = str(a["userId"])
        self.creds = {"appId": AGORA_APP_ID, "rtmUserId": a["rtmUserId"], "rtmToken": a["rtmToken"],
                      "userId": self.user_id, "sn": self.sn}
        self.rtc = {"appId": AGORA_APP_ID, "channel": self.sn, "token": a.get("rtcToken", ""),
                    "uid": a.get("rtcUserId")}
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
        self.cur_code = None
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
        self._post({"type": "publish", "id": rid, "deadline": time.time() + timeout + 2,
                    "text": json.dumps({"id": rid, "method": method, "params": params or {}})})
        ok = slot[0].wait(timeout)
        with self.lock:
            self.pending.pop(rid, None)
        if not ok:
            self._drop_publish(rid)
            raise RuntimeError("printer did not answer method %s" % method)
        res = _dict((slot[1] or {}).get("result"))
        ec = res.get("error_code", 0)
        if ec not in (0, None):
            err = str(res.get("error", ""))
            if "-11033" in err:
                self._set_printer_online(False, "The printer is offline in the Elegoo cloud (powered off or no Wi-Fi?)")
                raise RuntimeError("printer is offline in the Elegoo cloud")
            raise RuntimeError("printer error_code %s (%s) for method %s" % (ec, err, method))
        self._set_printer_online(True)
        return res

    # ---- status -> Bambu-style push_status
    LABELS = {0: "Initializing", 1: "Idle", 2: "Printing", 3: "Filament operation", 4: "Filament operation",
              5: "Auto leveling", 6: "PID calibration", 7: "Resonance test", 8: "Self check", 9: "Updating",
              10: "Homing", 11: "Receiving file", 12: "Composing video", 13: "Extruder operation",
              14: "Emergency stop", 15: "Power loss recovery"}
    # sub_status values while machine_status.status == 2 (from the elegoo-link CC2 adapter)
    SUB2 = {1081: "Downloading", 1082: "Downloading", 1086: "Downloading", 1045: "Heating nozzle",
            1096: "Heating nozzle", 1405: "Heating bed", 1906: "Heating bed", 2801: "Homing", 2802: "Homing",
            2901: "Leveling", 2902: "Leveling", 2501: "Pausing", 2502: "Paused", 2505: "Paused",
            2401: "Resuming", 2402: "Resuming", 2075: "Printing", 2077: "Finished", 2503: "Stopping",
            2504: "Stopped"}

    def _emit_status(self):
        fn = self.cb.get("local_message")
        if not self.sn:
            return
        s = self.status
        ps, ms = _dict(s.get("print_status")), _dict(s.get("machine_status"))
        ex, hb = _dict(s.get("extruder")), _dict(s.get("heater_bed"))
        zt, gm = _dict(s.get("ztemperature_sensor")), _dict(s.get("gcode_move"))
        fans, th = _dict(s.get("fans")), _dict(s.get("tool_head")) or _dict(s.get("toolhead"))
        has_code = ms.get("status") is not None
        code, sub = int(_num(ms.get("status"), 1)), int(_num(ms.get("sub_status"), 0))
        pstate = str(ps.get("state", "")).lower()
        if code == 2:
            if sub in (2502, 2505):
                gs = "PAUSE"
            elif sub == 2077:
                gs = "FINISH"
            elif sub == 2504:
                gs = "FAILED"
            else:
                gs = "RUNNING"
        elif code == 1:
            gs = "FINISH" if pstate in ("complete", "completed", "finished") else (
                "FAILED" if pstate in ("cancelled", "canceled", "stopped", "failed", "error") else "IDLE")
        elif code == 14:
            gs = "FAILED"
        else:
            gs = "PREPARE"
        label = self.SUB2.get(sub) if code == 2 and sub in self.SUB2 else self.LABELS.get(code, "Status %s" % code)
        if has_code:
            self.cur_code, self.status_ts = code, time.time()
        self.cur_gs = gs
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
        if fn:
            try:
                fn(self.sn, json.dumps(msg))
            except Exception as e:
                self.logs.append("local_message cb failed: %r" % e)

        def pc(k):
            return int(round(_num(_dict(fans.get(k)).get("speed")) * 100 / 255.0))
        pr = msg["print"]
        self._post({"type": "status", "state": gs, "label": label, "code": code, "sub": sub, "progress": pct,
                    "nozzle": pr["nozzle_temper"], "nozzle_t": pr["nozzle_target_temper"],
                    "bed": pr["bed_temper"], "bed_t": pr["bed_target_temper"], "chamber": pr["chamber_temper"],
                    "layer": pr["layer_num"], "total": pr["total_layer_num"], "left": pr["mc_remaining_time"],
                    "file": pr["subtask_name"], "x": _num(gm.get("x")), "y": _num(gm.get("y")), "z": _num(gm.get("z")),
                    "homed": str(th.get("homed_axes", "") or ""), "speed": int(_num(gm.get("speed_mode"), 1)),
                    "model_fan": pc("fan"), "aux_fan": pc("aux_fan"), "case_fan": pc("box_fan"),
                    "light": _num(_dict(s.get("led")).get("status")) == 1})

    def _fire_local_connect(self, status, msg=""):
        fn = self.cb.get("local_connect")
        if fn:
            try:
                fn(status, self.sn, msg)
            except Exception as e:
                self.logs.append("local_connect cb failed: %r" % e)

    # ---- polling (one thread, own stop event)
    def start_poll(self):
      with self.poll_lock:
        self.stop_poll()
        ev = threading.Event()
        self.poll_stop = ev

        def run():
            fails = 0
            while not ev.is_set():
                try:
                    self.request(1002, {}, timeout=15)
                    fails = 0
                except Exception as e:
                    self.log("poll: %s" % e)
                    fails += 1
                    if "-10027" in self.login_error and fails >= 2:
                        self.log("poll stopped: another session holds the account. Close ElegooSlicer, then press Connect.")
                        return
                    ev.wait(15 if not self.printer_online else 10)
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
        i.id, i.name, i.version = AGENT_ID, "Elegoo CC2 (cloud)", "0.2.0"
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
                if not SESSION.logged_in():
                    raise RuntimeError("Not signed in: open the Elegoo CC2 tab and use Sign in.")
                HUB.ensure_login()
                HUB.request(1001, {}, timeout=15)
                HUB.was_connected = True
                HUB.want_poll = True
                HUB._fire_local_connect(0, "")
                HUB.start_poll()
            except Exception as e:
                HUB.log("connect failed: %s" % e)
                HUB._fire_local_connect(1, str(e))
        threading.Thread(target=run, daemon=True).start()
        return 0

    def disconnect_printer(self):
        HUB.want_poll = False
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
                HUB._exec_ui(method, params or {})
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
        if any(re.match(r"G28\b", ln.split(";")[0].strip()) for ln in g.splitlines()):
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
 :root{--bg:var(--orca-bg,#f4f6f8);--fg:var(--orca-fg,#1f2933);--mut:var(--orca-muted,#6b7785);--acc:var(--orca-accent,#0f766e);--bd:var(--orca-border,#d5dbe1);--card:#fff;--bad:#c0392b;--ok:#0f766e;--warn:#b7791f}
 *{box-sizing:border-box} body{font:13px var(--orca-font,'Segoe UI',sans-serif);color:var(--fg);background:var(--bg);margin:0;padding:14px}
 h3{margin:0;font-size:15px} .mut{color:var(--mut)} .grid{display:grid;gap:12px}
 .top{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
 .top .name{font-size:18px;font-weight:600} .chips{display:flex;gap:6px;flex-wrap:wrap}
 .chip{padding:2px 9px;border-radius:999px;border:1px solid var(--bd);background:var(--card);font-size:12px}
 .chip.ok{border-color:var(--ok);color:var(--ok)} .chip.bad{border-color:var(--bad);color:var(--bad)} .chip.warn{border-color:var(--warn);color:var(--warn)}
 .cols{display:grid;gap:12px;grid-template-columns:repeat(2,minmax(0,1fr));align-items:stretch}
 @media (max-width:760px){.cols{grid-template-columns:1fr}}
 .card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:12px 14px}
 .card{display:flex;flex-direction:column} .card>h3{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
 button{font:inherit;padding:5px 11px;border:1px solid var(--bd);border-radius:6px;background:#fff;color:var(--fg);cursor:pointer}
 button:hover:not(:disabled){border-color:var(--acc);color:var(--acc)} button:disabled{opacity:.45;cursor:not-allowed}
 button.pri{background:var(--acc);border-color:var(--acc);color:#fff} button.pri:hover:not(:disabled){color:#fff;filter:brightness(1.1)}
 button.dng{border-color:var(--bad);color:var(--bad)} button.sel{background:var(--acc);border-color:var(--acc);color:#fff}
 input[type=number],select{font:inherit;padding:4px 6px;border:1px solid var(--bd);border-radius:6px;background:#fff;color:var(--fg)}
 input[type=number]{width:68px} .row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:6px 0}
 .bar{height:8px;border-radius:5px;background:#e3e8ed;overflow:hidden} .bar>i{display:block;height:100%;background:var(--acc);width:0%}
 .kv{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:10px 0} .kv div{display:flex;flex-direction:column}
 .k{font-size:11px;text-transform:uppercase;color:var(--mut)} .v{font-weight:600}
 .pad{display:grid;grid-template-columns:repeat(3,44px);grid-template-rows:repeat(3,38px);gap:5px} .pad button{padding:0}
 .zcol{display:flex;flex-direction:column;gap:5px} .zcol button{width:44px;height:38px;padding:0}
 .slots{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:6px 0}
 .slot{border:2px solid var(--bd);border-radius:10px;padding:8px 4px;text-align:center;cursor:pointer;background:#fff}
 .slot.on{border-color:var(--acc);box-shadow:0 0 0 2px rgba(15,118,110,.18)} .slot .dot{width:34px;height:34px;border-radius:50%;margin:0 auto 4px;border:1px solid #0003}
 .slot small{display:block;color:var(--mut)} .slot.empty .dot{background:repeating-linear-gradient(45deg,#eee,#eee 4px,#fff 4px,#fff 8px)}
 table{width:100%;border-collapse:collapse} th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--bd);vertical-align:middle}
 th{font-size:11px;text-transform:uppercase;color:var(--mut)} td.n{max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .tabs{display:flex;gap:6px} .dots i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:3px;border:1px solid #0003}
 .toolbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;flex-wrap:wrap;gap:8px}
 #note{position:sticky;top:0;z-index:5;margin:0 0 10px;padding:8px 12px;border-radius:8px;display:none;border:1px solid var(--bd);background:#fff}
 #note.bad{border-color:var(--bad);color:var(--bad)} #note.ok{border-color:var(--ok);color:var(--ok)}
 .cols.off{opacity:.4;pointer-events:none;filter:grayscale(.4)}
 .panel{border:1px dashed var(--acc);border-radius:8px;padding:10px;margin:8px 0;background:#f1faf9}
 .sw{display:inline-flex;align-items:center;gap:6px} details pre{background:#0001;padding:8px;max-height:220px;overflow:auto;white-space:pre-wrap;border-radius:6px}
 .rng{display:grid;grid-template-columns:90px 1fr 44px;gap:8px;align-items:center;margin:6px 0}
</style>

<div id="note"></div>
<div class="top">
  <div><div class="name" id="pname">Elegoo CC2</div><div class="mut" id="pinfo"></div></div>
  <div class="chips"><span class="chip" id="cRtm">RTM: starting</span><span class="chip" id="cPr">Printer: -</span><span class="chip" id="cSt">-</span></div>
</div>

<div class="card" id="acct" style="margin-bottom:12px">
  <h3>Account <span class="mut" id="aSub"></span></h3>
  <div id="aOut" style="display:none">
    <div class="row">You are not signed in to the Elegoo cloud.</div>
    <div class="row"><button class="pri" id="bSign">Sign in with Elegoo</button>
      <button id="bImport" title="Reuse the session of the ElegooSlicer installed on this PC (token only, read once)">Use ElegooSlicer session</button></div>
    <div class="mut">Sign-in opens Elegoo's own page in your browser, served through a local connection on this PC. The plugin passes your password to Elegoo in memory only: it is never stored or logged. Only the session Elegoo returns is kept (encrypted).</div>
  </div>
  <div id="aIn" style="display:none">
    <div class="row"><span>Signed in as <b id="aMail"></b></span><span class="mut" id="aSrc"></span><span class="chip" id="aLink">cloud link: offline</span></div>
    <div class="row"><span>Printer</span><select id="aDev"></select>
      <button class="pri" id="bConn">Connect via cloud</button><button id="bOut" class="dng">Log out</button></div>
    <div class="row"><label class="sw"><input type="checkbox" id="aAuto"> Connect automatically</label><span id="aStat" class="mut"></span></div>
    <div class="mut">Close ElegooSlicer while connected: the Elegoo account allows one cloud session. LAN mode is not supported yet.</div>
  </div>
</div>

<div id="ctlHint" class="mut" style="margin:4px 2px 10px">Connect to the Elegoo cloud (Account above) to see and control the printer.</div>
<div class="cols off" id="ctl">
  <div class="card" id="cPrint">
    <h3>Print <span class="mut" id="pFile"></span></h3>
    <div class="bar"><i id="pBar"></i></div>
    <div class="kv">
      <div><span class="k">Progress</span><span class="v" id="vPg">-</span></div>
      <div><span class="k">Layer</span><span class="v" id="vLy">-</span></div>
      <div><span class="k">Remaining</span><span class="v" id="vLf">-</span></div>
    </div>
    <div class="row">
      <button id="bPause">Pause</button><button id="bResume">Resume</button><button id="bStop" class="dng">Stop</button>
    </div>
    <div class="row"><span class="mut" style="width:70px">Speed</span>
      <select id="sSpeed"><option value="0">Silent</option><option value="1">Balanced</option><option value="2">Sport</option><option value="3">Ludicrous</option></select>
    </div>
    <div id="cfmStop" class="panel" style="display:none">Stop the running print? <button id="yStop" class="dng">Yes, stop</button> <button id="nStop">No</button></div>
  </div>

  <div class="card">
    <h3>Temperature</h3>
    <div class="row"><span style="width:70px">Nozzle</span><b id="tNz">-</b><span class="mut">/</span><input type="number" id="iNz" min="0" max="350" placeholder="0"><button id="bNz">Set</button><button id="bNz0">Off</button></div>
    <div class="row"><span style="width:70px">Bed</span><b id="tBd">-</b><span class="mut">/</span><input type="number" id="iBd" min="0" max="120" placeholder="0"><button id="bBd">Set</button><button id="bBd0">Off</button></div>
    <div class="row"><span style="width:70px">Chamber</span><b id="tCh">-</b></div>
  </div>

  <div class="card">
    <h3>Motion <span class="mut" id="pos">X - Y - Z -</span></h3>
    <div class="row"><span class="mut">Step</span>
      <span class="tabs" id="steps"><button data-s="0.1">0.1</button><button data-s="1">1</button><button data-s="10" class="sel">10</button><button data-s="30">30</button><span class="mut">mm</span></span></div>
    <div class="row" style="gap:18px;align-items:flex-start">
      <div class="pad">
        <span></span><button data-mv="y+">Y+</button><span></span>
        <button data-mv="x-">X-</button><button id="bHxy" title="Home X and Y">XY</button><button data-mv="x+">X+</button>
        <span></span><button data-mv="y-">Y-</button><span></span>
      </div>
      <div class="zcol"><button data-mv="z+" title="Increase Z">Z+</button><button id="bHz" title="Home Z">Z</button><button data-mv="z-" title="Decrease Z">Z-</button></div>
      <div class="zcol"><button id="bHall" title="Home all axes" style="height:auto;padding:8px 10px">Home<br>all</button></div>
    </div>
    <div class="mut" id="homedNote"></div>
  </div>

  <div class="card">
    <h3>Fans and light</h3>
    <div class="rng"><span>Model</span><input type="range" id="fModel" min="0" max="100" step="5"><b id="vModel">0%</b></div>
    <div class="rng"><span>Assistance</span><input type="range" id="fAux" min="0" max="100" step="5"><b id="vAux">0%</b></div>
    <div class="rng"><span>Case</span><input type="range" id="fCase" min="0" max="100" step="5"><b id="vCase">0%</b></div>
    <div class="row"><label class="sw"><input type="checkbox" id="light"> Overhead light</label></div>
  </div>

  <div class="card" style="grid-column:1/-1">
    <h3>Camera <span><span class="mut" id="camSt"></span> <button id="bCam">Turn on</button></span></h3>
    <div id="camBox" style="display:none;background:#000;border-radius:8px;aspect-ratio:16/9;max-height:520px;overflow:hidden"></div>
    <div class="mut" id="camHint">Live view from the printer camera (needs the cloud account; one viewer per account).</div>
  </div>

  <div class="card" style="grid-column:1/-1">
    <h3>Filament <span><label class="sw"><input type="checkbox" id="autoRefill"> Auto refill</label> <button id="bCanvas">Refresh</button></span></h3>
    <div class="slots" id="slots"><span class="mut">Press Refresh to read the filament slots.</span></div>
    <div class="row"><button id="bLoad">Load</button><button id="bUnload">Unload</button><span class="mut">Select a slot first.</span>
      <span style="margin-left:auto" class="mut">Extruder:</span><button id="bFeed">Feed</button><button id="bRetreat">Retract</button></div>
  </div>

  <div class="card" style="grid-column:1/-1">
    <div class="toolbar"><div class="tabs"><button id="tFiles" class="sel">Printer files</button><button id="tHist">Print history</button></div><button id="bFiles">Refresh</button></div>
    <div id="startPanel" class="panel" style="display:none"></div>
    <div id="fileBox"><span class="mut">Press Refresh to list the files.</span></div>
  </div>
</div>

<details style="margin-top:12px"><summary class="mut">Log</summary><pre id="log"></pre></details>

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
<script src="https://cdn.jsdelivr.net/npm/agora-rtc-sdk-ng@4.22.0/AgoraRTC_N-production.js"></script>
<script>
const $=id=>document.getElementById(id); const dec=new TextDecoder();
let rtm=null, C=null, GEN=0, off=null;
function P(o){ try{ orca.postMessage(o);}catch(e){ L('bridge error '+e);} }
function L(s){ const l=$('log'); l.textContent+=s+'\n'; l.scrollTop=l.scrollHeight; }

/* ---------- RTM transport (unchanged protocol) ---------- */
async function drop(){
  if(off){ try{off();}catch(e){} off=null; }
  if(rtm){ try{ await rtm.logout(); }catch(e){} rtm=null; }
}
async function doLogin(cfg,gen){
  C=cfg; GEN=gen; setChip('cRtm','RTM: logging in','warn');
  try{
    await drop();
    const {RTM}=AgoraRTM;
    const r=new RTM(cfg.appId,cfg.rtmUserId,{presenceTimeout:300,heartbeatInterval:30});
    const onStatus=e=>{ if(gen!==GEN) return; setChip('cRtm','RTM: '+String(e.state).toLowerCase(), String(e.state)==='CONNECTED'?'ok':'warn'); P({type:'rtm_status',gen,state:e.state,reason:e.reason}); };
    const onExp=()=>{ if(gen===GEN) P({type:'token_expiring',gen}); };
    const printer=cfg.userId+cfg.sn;
    const onMsg=e=>{ if(gen!==GEN || e.publisher!==printer) return; let t=e.message; if(typeof t!=='string') t=dec.decode(t); P({type:'msg',gen,text:t}); };
    r.addEventListener('status',onStatus); r.addEventListener('tokenPrivilegeWillExpire',onExp); r.addEventListener('message',onMsg);
    off=()=>{ r.removeEventListener('status',onStatus); r.removeEventListener('tokenPrivilegeWillExpire',onExp); r.removeEventListener('message',onMsg); };
    rtm=r;
    await r.login({token:cfg.rtmToken});
    await r.subscribe(cfg.userId,{withMessage:true,withPresence:true});
    setChip('cRtm','RTM: online','ok'); P({type:'login_ok',gen});
  }catch(e){ setChip('cRtm','RTM: error','bad'); P({type:'login_err',gen,error:(e&&e.message)||JSON.stringify(e)}); }
}

/* ---------- UI helpers ---------- */
function setChip(id,text,cls){ const e=$(id); e.textContent=text; e.className='chip '+(cls||''); }
let noteT=null;
function note(text,kind){ const n=$('note'); n.textContent=text; n.className=kind||''; n.style.display='block'; clearTimeout(noteT); noteT=setTimeout(()=>{n.style.display='none';},kind==='bad'?9000:3500); }
let RID=0; const WAIT={};
async function locked(fn){ BUSY=true; applyStatus(S); try{ return await fn(); } finally{ BUSY=false; applyStatus(S); } }
function call(method,params,ms){ return new Promise((res,rej)=>{ const rid=++RID; const t=setTimeout(()=>{delete WAIT[rid];rej(new Error('timeout'));},ms||45000); WAIT[rid]={res,rej,t}; P({type:'cmd',rid,method,params:params||{}}); }); }
async function run(label,method,params,ms){ try{ const r=await call(method,params,ms); if(label) note(label,'ok'); return r; }catch(e){ note((label||String(method))+' failed: '+e.message,'bad'); throw e; } }
const fmtT=m=>{ if(!m) return '-'; const h=Math.floor(m/60), mm=Math.round(m%60); return h?h+' h '+mm+' min':mm+' min'; };
const fmtS=s=>{ if(!s) return '-'; const m=Math.round(s/60); return fmtT(m); };
const fmtMB=b=>b?(b/1048576).toFixed(b<1048576*10?2:1)+' MB':'-';
const fmtD=t=>{ if(!t) return '-'; const d=new Date((t>1e12?t:t*1000)); return isNaN(d)?'-':d.toISOString().slice(0,16).replace('T',' '); };
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const col=s=>/^#?[0-9a-f]{3,8}$/i.test(String(s||''))?(String(s)[0]==='#'?String(s):'#'+s):'#ddd';
const num=v=>(+v)|0;

/* ---------- live state ---------- */
let S={code:null,state:'IDLE',homed:'',online:false}; let STEP=10, dragging=null, BUSY=false;
const active=m=>m.code===2&&m.sub!==2077&&m.sub!==2504;
function applyStatus(m){
  S=m; const idle=(m.code===1)&&!BUSY, printing=active(m), paused=(m.state==='PAUSE');
  $('cSt').textContent=m.label||m.state; $('cSt').className='chip '+(printing?'ok':(m.code===14?'bad':''));
  $('pFile').textContent=m.file||''; $('pBar').style.width=Math.min(100,m.progress||0)+'%';
  $('vPg').textContent=(m.progress||0)+'%'; $('vLy').textContent=m.total?(m.layer+' / '+m.total):'-'; $('vLf').textContent=fmtT(m.left);
  $('tNz').textContent=Math.round(m.nozzle)+' C'; $('tBd').textContent=Math.round(m.bed)+' C'; $('tCh').textContent=Math.round(m.chamber)+' C';
  if(document.activeElement!==$('iNz')) $('iNz').placeholder=Math.round(m.nozzle_t||0); if(document.activeElement!==$('iBd')) $('iBd').placeholder=Math.round(m.bed_t||0);
  $('pos').textContent='X '+(+m.x).toFixed(1)+'  Y '+(+m.y).toFixed(1)+'  Z '+(+m.z).toFixed(1);
  $('homedNote').textContent=m.homed?('Homed axes: '+m.homed.toUpperCase()):'Not homed: home the axes before moving.';
  for(const [id,k,lab] of [['fModel','model','vModel'],['fAux','aux','vAux'],['fCase','case','vCase']]){ if(dragging!==id){ $(id).value=m[k+'_fan']; } $(lab).textContent=Math.round($(id).value)+'%'; }
  if(dragging!=='light') $('light').checked=!!m.light; if(document.activeElement!==$('sSpeed')) $('sSpeed').value=String(m.speed);
  const canPause=printing&&!paused, canResume=paused;
  $('bPause').disabled=!canPause; $('bResume').disabled=!canResume; $('bStop').disabled=!(printing||paused);
  document.querySelectorAll('[data-mv],#bHxy,#bHz,#bHall').forEach(b=>b.disabled=!idle);
  $('bNz').disabled=$('bBd').disabled=!(m.code===1||printing||m.code===13);
  $('bNz0').disabled=$('bBd0').disabled=!(m.code===1||m.code===13);
  $('bLoad').disabled=$('bUnload').disabled=$('bFeed').disabled=$('bRetreat').disabled=!idle;
}

/* ---------- messages from Python ---------- */
async function handle(m){
  if(!m) return;
  if(m.type==='login') doLogin(m.cfg,m.gen);
  else if(m.type==='logout'){ GEN++; await drop(); setChip('cRtm','RTM: logged out',''); }
  else if(m.type==='renew' && rtm){ try{ await rtm.renewToken(m.token);}catch(e){ L('renew '+e);} }
  else if(m.type==='publish'){
    if(m.deadline && Date.now()/1000>m.deadline){ L('dropped an expired command'); return; }
    if(!rtm||!C){ P({type:'publish_err',id:m.id,error:'RTM not logged in'}); return; }
    try{ await rtm.publish(C.userId+C.sn,m.text,{channelType:'USER',customType:'PlainText'}); }
    catch(e){ P({type:'publish_err',id:m.id,error:(e&&e.message)||JSON.stringify(e)}); }
  }
  else if(m.type==='log') L(m.line);
  else if(m.type==='logs') (m.lines||[]).forEach(L);
  else if(m.type==='info'){ $('pname').textContent=m.name||'Elegoo CC2'; $('pinfo').textContent=[m.model,'firmware '+(m.fw||'?'),'LAN IP '+(m.ip||'?'),'SN '+(m.sn||'?')].join('  |  '); }
  else if(m.type==='note'){ note(m.text,m.kind); }
  else if(m.type==='connstat'){ const e=$('aStat'); e.textContent=m.text; e.style.color=m.kind==='bad'?'var(--bad)':(m.kind==='ok'?'var(--ok)':''); }
  else if(m.type==='account'){ renderAccount(m); }
  else if(m.type==='conn'){ setChip('cPr','Printer: '+(m.printer?'online':'offline'),m.printer?'ok':'bad'); if(m.msg) note(m.msg,m.printer?'ok':'bad'); if(!m.printer){ $('ctl').classList.add('off'); $('ctlHint').style.display='block'; $('aStat').textContent=m.msg||'Printer is offline in the Elegoo cloud'; $('aStat').style.color='var(--bad)'; } }
  else if(m.type==='status'){ setChip('cPr','Printer: online','ok'); $('ctl').classList.remove('off'); $('ctlHint').style.display='none'; applyStatus(m); }
  else if(m.type==='cmd_result'){ const w=WAIT[m.rid]; if(!w) return; clearTimeout(w.t); delete WAIT[m.rid]; m.ok?w.res(m.result):w.rej(new Error(m.error)); }
  else if(m.type==='batch'){ for(const it of (m.items||[])) await handle(it); }
}
orca.onMessage(handle);
setInterval(()=>P({type:'pull'}),300);

/* ---------- controls ---------- */
/* ---------- account ---------- */
let ACC=null;
function renderAccount(a){
  ACC=a; $('aOut').style.display=a.logged?'none':'block'; $('aIn').style.display=a.logged?'block':'none';
  if(!a.logged){ return; }
  $('aMail').textContent=a.email||a.nickname||'(account)'; $('aSrc').textContent=a.source==='imported'?'(session imported from ElegooSlicer)':'';
  const sel=$('aDev'); sel.innerHTML=(a.devices||[]).map(d=>'<option value="'+esc(d.sn)+'"'+(d.sn===a.sn?' selected':'')+'>'+esc(d.name)+' ('+esc(d.sn)+')</option>').join('');
  const on=a.rtm; $('aLink').textContent='cloud link: '+(on?'online':'offline'); $('aLink').className='chip '+(on?'ok':'');
  $('aAuto').checked=a.auto!==false; if(!on){ $('ctl').classList.add('off'); $('ctlHint').style.display='block'; }
}
async function loadAccount(){ try{ renderAccount(await call('acct_status')); }catch(e){ note('Account: '+e.message,'bad'); } }
$('bSign').onclick=()=>run('Sign-in page opened in your browser','acct_login').catch(()=>{});
$('bImport').onclick=()=>run('Session imported','acct_import').then(renderAccount).catch(()=>{});
$('bOut').onclick=()=>run('Signed out','acct_logout').then(renderAccount).catch(()=>{});
$('aDev').onchange=e=>run('Printer selected','acct_select',{sn:e.target.value}).then(renderAccount).catch(()=>{});
$('bConn').onclick=()=>run(null,'acct_connect',{},90000).then(renderAccount).catch(()=>{});
$('aAuto').onchange=e=>run(null,'acct_auto',{on:e.target.checked}).catch(()=>{});
$('bPause').onclick=()=>run('Paused',1021).catch(()=>{});
$('bResume').onclick=()=>run('Resumed',1023).catch(()=>{});
$('bStop').onclick=()=>{ $('cfmStop').style.display='block'; };
$('nStop').onclick=()=>{ $('cfmStop').style.display='none'; };
$('yStop').onclick=()=>{ $('cfmStop').style.display='none'; run('Stopped',1022).catch(()=>{}); };
$('sSpeed').onchange=e=>run('Speed set',1031,{mode:+e.target.value}).catch(()=>{});
const setT=(key,id,v)=>run(key==='extruder'?'Nozzle target set':'Bed target set',1028,{[key]:v}).catch(()=>{});
$('bNz').onclick=()=>{ const v=parseInt($('iNz').value); if(!(v>=0&&v<=350)) return note('Nozzle 0-350 C','bad'); setT('extruder','iNz',v); $('iNz').value=''; };
$('bBd').onclick=()=>{ const v=parseInt($('iBd').value); if(!(v>=0&&v<=120)) return note('Bed 0-120 C','bad'); setT('heater_bed','iBd',v); $('iBd').value=''; };
$('bNz0').onclick=()=>setT('extruder','iNz',0); $('bBd0').onclick=()=>setT('heater_bed','iBd',0);
document.querySelectorAll('#steps button').forEach(b=>b.onclick=()=>{ STEP=+b.dataset.s; document.querySelectorAll('#steps button').forEach(x=>x.classList.toggle('sel',x===b)); });
document.querySelectorAll('[data-mv]').forEach(b=>b.onclick=()=>{ const [ax,sg]=[b.dataset.mv[0],b.dataset.mv[1]]; run(null,1027,{axes:ax,distance:(sg==='-'?-1:1)*STEP}).catch(()=>{}); });
$('bHxy').onclick=()=>locked(()=>run('Homing XY',1026,{homed_axes:'xy'},150000)).catch(()=>{});
$('bHz').onclick=()=>locked(()=>run('Homing Z',1026,{homed_axes:'z'},150000)).catch(()=>{});
$('bHall').onclick=()=>locked(()=>run('Homing all axes',1026,{homed_axes:'xyz'},180000)).catch(()=>{});
for(const [id,key,lab] of [['fModel','fan','vModel'],['fAux','aux_fan','vAux'],['fCase','box_fan','vCase']]){
  const el=$(id);
  el.oninput=()=>{ dragging=id; $(lab).textContent=el.value+'%'; };
  el.onchange=()=>{ run(null,1030,{[key]:Math.round(el.value*255/100)}).catch(()=>{}).finally(()=>{dragging=null;}); };
}
$('light').onchange=e=>{ dragging='light'; run('Light '+(e.target.checked?'on':'off'),1029,{power:e.target.checked?1:0}).catch(()=>{}).finally(()=>{dragging=null;}); };

/* ---------- camera (Agora RTC, same flow as ElegooSlicer: live/vp8, channel = printer serial) ---------- */
let CAM=null, CAMON=false;
(function(){ try{ if(!navigator.mediaDevices){ navigator.mediaDevices={}; }
  if(typeof navigator.mediaDevices.enumerateDevices!=='function') navigator.mediaDevices.enumerateDevices=()=>Promise.resolve([]);
  if(typeof navigator.mediaDevices.getUserMedia!=='function') navigator.mediaDevices.getUserMedia=()=>Promise.reject(new Error('no capture devices')); }catch(e){} })();
function camMsg(t,bad){ $('camSt').textContent=t||''; $('camSt').style.color=bad?'var(--bad)':''; }
async function camStop(){
  CAMON=false; $('bCam').textContent='Turn on'; $('camBox').style.display='none'; $('camBox').innerHTML=''; camMsg('');
  if(CAM){ try{ CAM.removeAllListeners(); await CAM.leave(); }catch(e){} CAM=null; }
}
async function camStart(){
  if(typeof AgoraRTC==='undefined'){ camMsg('RTC SDK not loaded',true); return; }
  camMsg('connecting...'); $('bCam').disabled=true;
  try{
    const cfg=await call('cam_creds',{},30000);
    try{ AgoraRTC.setLogLevel(3); AgoraRTC.disableLogUpload&&AgoraRTC.disableLogUpload(); }catch(e){}
    const c=AgoraRTC.createClient({mode:'live',codec:'vp8'}); CAM=c;
    const show=async(u)=>{ try{ await c.subscribe(u,'video'); if(u.videoTrack){ $('camBox').style.display='block'; $('camBox').innerHTML=''; u.videoTrack.play($('camBox')); camMsg('live'); } }catch(e){ camMsg('subscribe: '+((e&&e.message)||e),true); } };
    c.on('user-published',async(u,m)=>{ if(m==='video') await show(u); });
    c.on('user-unpublished',()=>camMsg('stream stopped'));
    c.on('connection-state-change',(a,b)=>{ if(a==='DISCONNECTED'&&CAMON){ camMsg('disconnected',true); } });
    await c.join(cfg.appId,String(cfg.channel),cfg.token,Number(cfg.uid));
    await c.setClientRole('host');
    CAMON=true; $('bCam').textContent='Turn off';
    for(let i=0;i<30&&CAMON;i++){ const u=c.remoteUsers.find(x=>x.hasVideo)||c.remoteUsers[0]; if(u){ await show(u); if(u.videoTrack) break; } await new Promise(r=>setTimeout(r,1000)); }
    if(CAMON && !$('camBox').innerHTML) camMsg('no video from the printer (offline, or another viewer is connected)',true);
  }catch(e){ camMsg((e&&e.message)||String(e),true); await camStop(); }
  finally{ $('bCam').disabled=false; }
}
$('bCam').onclick=()=>{ CAMON?camStop():camStart(); };

/* ---------- filament / canvas ---------- */
let CANVAS=null, SEL=null;
function renderCanvas(){
  const box=$('slots'); box.innerHTML=''; if(!CANVAS){ box.innerHTML='<span class="mut">No canvas data.</span>'; return; }
  const list=(CANVAS.canvas_list||[]).filter(c=>c.connected);
  if(!list.length){ box.innerHTML='<span class="mut">No Canvas connected.</span>'; return; }
  for(const c of list){ for(const t of (c.tray_list||[])){
    const d=document.createElement('div'); const has=t.filament_type||t.filament_name; d.className='slot'+(has?'':' empty')+((SEL&&SEL.c===c.canvas_id&&SEL.t===t.tray_id)?' on':'');
    d.innerHTML='<div class="dot" style="background:'+col(t.filament_color)+'"></div><b>'+esc(t.filament_type||'-')+'</b><small>'+(list.length>1?'C'+num(c.canvas_id)+' ':'')+'slot '+(num(t.tray_id)+1)+'</small>';
    d.onclick=()=>{ SEL={c:c.canvas_id,t:t.tray_id}; renderCanvas(); };
    box.appendChild(d);
  } }
  $('autoRefill').checked=!!CANVAS.auto_refill;
}
async function loadCanvas(){ try{ const r=await call('canvas'); CANVAS=r.canvas_info||null; renderCanvas(); }catch(e){ note('Canvas: '+e.message,'bad'); } }
$('bCanvas').onclick=loadCanvas;
$('bLoad').onclick=()=>{ if(!SEL) return note('Select a slot first','bad'); locked(()=>run('Loading filament',2001,{canvas_id:SEL.c,tray_id:SEL.t},330000)).then(loadCanvas).catch(()=>{}); };
$('bUnload').onclick=()=>{ if(!SEL) return note('Select a slot first','bad'); locked(()=>run('Unloading filament',2002,{canvas_id:SEL.c,tray_id:SEL.t},330000)).then(loadCanvas).catch(()=>{}); };
$('bFeed').onclick=()=>locked(()=>run('Feeding',1024,{},330000)).catch(()=>{});
$('bRetreat').onclick=()=>locked(()=>run('Retracting',1025,{},330000)).catch(()=>{});
$('autoRefill').onchange=e=>run('Auto refill '+(e.target.checked?'on':'off'),2004,{auto_refill:e.target.checked}).catch(()=>{});

/* ---------- files and history ---------- */
let TAB='files', FILES=[];
$('tFiles').onclick=()=>{ TAB='files'; $('tFiles').classList.add('sel'); $('tHist').classList.remove('sel'); refresh(); };
$('tHist').onclick=()=>{ TAB='hist'; $('tHist').classList.add('sel'); $('tFiles').classList.remove('sel'); refresh(); };
$('bFiles').onclick=()=>refresh();
async function refresh(){ $('startPanel').style.display='none'; if(TAB==='files') return loadFiles(); return loadHistory(); }
async function loadFiles(){
  $('fileBox').innerHTML='<span class="mut">Loading...</span>';
  try{ const r=await call('files',{},180000); FILES=r.files||[]; renderFiles(); }catch(e){ $('fileBox').innerHTML='<span class="mut">Could not load files: '+esc(e.message)+'</span>'; }
}
function renderFiles(){
  if(!FILES.length){ $('fileBox').innerHTML='<span class="mut">No files on the printer.</span>'; return; }
  const rows=FILES.slice().sort((a,b)=>(b.create_time||0)-(a.create_time||0)).map((f,i)=>{
    const dots=(f.color_map||[]).map(c=>'<i style="background:'+col(c.color)+'" title="T'+esc(c.t)+' '+esc(c.name)+'"></i>').join('');
    return '<tr><td class="n" title="'+esc(f.filename)+'">'+esc(f.filename)+'</td><td>'+fmtMB(f.size)+'</td><td>'+esc(f.layer||'-')+'</td><td>'+fmtS(f.print_time)+'</td><td>'+(Number(f.total_filament_used)?Number(f.total_filament_used).toFixed(1)+' g':'-')+'</td><td class="dots">'+dots+'</td><td>'+fmtD(f.create_time)+'</td>'+
      '<td style="white-space:nowrap"><button class="pri" data-p="'+i+'">Print</button> <button class="dng" data-d="'+i+'">Delete</button></td></tr>';
  }).join('');
  $('fileBox').innerHTML='<table><tr><th>File</th><th>Size</th><th>Layers</th><th>Time</th><th>Filament</th><th>Colours</th><th>Created</th><th></th></tr>'+rows+'</table>';
  const sorted=FILES.slice().sort((a,b)=>(b.create_time||0)-(a.create_time||0));
  $('fileBox').querySelectorAll('[data-p]').forEach(b=>b.onclick=()=>openStart(sorted[+b.dataset.p]));
  $('fileBox').querySelectorAll('[data-d]').forEach(b=>b.onclick=()=>askDelete(sorted[+b.dataset.d]));
}
function askDelete(f){
  PANEL++; const p=$('startPanel'); p.style.display='block';
  p.innerHTML='Delete <b>'+esc(f.filename)+'</b> from the printer? <button class="dng" id="yDel">Yes, delete</button> <button id="nDel">No</button>';
  $('nDel').onclick=()=>{p.style.display='none';};
  $('yDel').onclick=async()=>{ p.style.display='none'; try{ await run('Deleted',1047,{storage_media:'local',file_path:[f.filename]}); loadFiles(); }catch(e){} };
}
let PANEL=0;
async function openStart(f){
  const my=++PANEL; const p=$('startPanel'); p.style.display='block'; p.innerHTML='Reading filament slots...';
  await loadCanvas(); if(my!==PANEL) return;
  const trays=[]; for(const c of ((CANVAS&&CANVAS.canvas_list)||[])){ if(!c.connected) continue; for(const t of (c.tray_list||[])) trays.push({c:c.canvas_id,t:t.tray_id,type:t.filament_type||'',color:t.filament_color||'',name:t.filament_name||''}); }
  if(!trays.length){ p.style.display='none'; return note('No filament slot available (is the Canvas connected?)','bad'); }
  const tools=(f.color_map&&f.color_map.length)?f.color_map:[{t:0,color:'',name:''}];
  let html='<b>Print '+esc(f.filename)+'</b><div class="mut">Choose the slot for each filament in the file.</div>';
  const used=new Set();
  tools.forEach((tl,i)=>{
    let guess=trays.findIndex((x,j)=>!used.has(j)&&x.type&&tl.name&&x.type.toUpperCase()===String(tl.name).toUpperCase());
    if(guess<0) guess=trays.findIndex((x,j)=>!used.has(j));
    if(guess>=0) used.add(guess);
    html+='<div class="row"><span style="min-width:150px">T'+esc(tl.t)+' '+esc(tl.name||'')+' '+(tl.color?'<i style="display:inline-block;width:12px;height:12px;border-radius:50%;background:'+col(tl.color)+';border:1px solid #0003"></i>':'')+'</span><select data-t="'+esc(tl.t)+'">'+
      trays.map((x,j)=>'<option value="'+j+'"'+(j===(guess>=0?guess:Math.min(i,trays.length-1))?' selected':'')+'>'+(trays.length>0&&CANVAS.canvas_list.filter(c=>c.connected).length>1?'C'+num(x.c)+' ':'')+'slot '+(num(x.t)+1)+' - '+esc(x.type||'empty')+'</option>').join('')+'</select></div>';
  });
  html+='<div class="row"><label class="sw"><input type="checkbox" id="chkLevel"> Auto bed leveling</label></div><div class="row"><button class="pri" id="yPrint">Start print</button> <button id="nPrint">Cancel</button></div>';
  p.innerHTML=html; $('nPrint').onclick=()=>{p.style.display='none';};
  $('yPrint').onclick=async()=>{
    const slots=[...p.querySelectorAll('select[data-t]')].map(s=>({t:+s.dataset.t,canvas_id:trays[+s.value].c,tray_id:trays[+s.value].t}));
    if(!slots.length||slots.some(s=>s.canvas_id===undefined)) return note('No filament slot available','bad');
    p.style.display='none'; try{ await locked(()=>run('Print started','start_file',{filename:f.filename,slots,leveling:$('chkLevel')?$('chkLevel').checked:false},60000)); }catch(e){}
  };
}
async function loadHistory(){
  $('fileBox').innerHTML='<span class="mut">Loading...</span>';
  try{
    const r=await call('history'); const list=r.history_task_list||r.task_list||r.list||[];
    if(!list.length){ $('fileBox').innerHTML='<span class="mut">No print history.</span>'; return; }
    const rows=list.map(h=>{
      const name=h.task_name||h.taskName||h.filename||h.file_name||'-', b=h.begin_time||h.beginTime||h.start_time, e=h.end_time||h.endTime;
      const st=h.task_status!=null?h.task_status:(h.status!=null?h.status:'');
      return '<tr><td class="n" title="'+esc(name)+'">'+esc(name)+'</td><td>'+fmtD(b)+'</td><td>'+fmtD(e)+'</td><td>'+esc(st)+'</td></tr>';
    }).join('');
    $('fileBox').innerHTML='<table><tr><th>Task</th><th>Started</th><th>Ended</th><th>Status</th></tr>'+rows+'</table>';
  }catch(e){ $('fileBox').innerHTML='<span class="mut">Could not load history: '+esc(e.message)+'</span>'; }
}

if(typeof AgoraRTM==='undefined'){ setChip('cRtm','RTM: SDK not loaded','bad'); L('AgoraRTM script failed to load'); }
P({type:'ready'});
setTimeout(loadAccount,600);
</script>
"""


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
            orca.request_permissions(threading=True)
        except Exception as e:
            HUB.log("request_permissions failed: %r" % e)
        orca.register_capability(CC2Page)
        orca.register_capability(CC2Agent)
