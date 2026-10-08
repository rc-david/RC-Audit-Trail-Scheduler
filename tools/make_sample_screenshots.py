#!/usr/bin/env python3
"""
Builds the FICTIONAL sample data and README screenshots. Documentation tooling only:
rc_audit_trail.py never uses any of this, and nothing here touches the RingCentral API.

What it does
  1. Generates docs/sample_events_FICTIONAL.json: made-up people, extensions and documentation-range
     IP addresses (203.0.113.x), in the same shape the real Audit Trail API returns.
  2. Builds viewer pages from that data using the real write_viewer() and viewer_template.html,
     then applies a UI state to each (a filter selected, settings panel open, dark mode, ...).
  3. Captures each page to docs/screenshots/*.png by driving a private headless Edge/Chrome through
     its DevTools remote-debugging port (a tiny stdlib-only WebSocket client below; nothing to install).

Run from the repository root:
    python tools/make_sample_screenshots.py                 # build pages and capture PNGs (needs Edge or Chrome)
    python tools/make_sample_screenshots.py --pages-only    # just build tools/_build/*.html to screenshot yourself
Re-run after changing the viewer so the screenshots stay current.
"""
import base64
import json
import os
import random
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import rc_audit_trail as rc  # noqa: E402

SHOTS = ROOT / "docs" / "screenshots"
BUILD = ROOT / "tools" / "_build"          # scratch pages (git-ignored)
SAMPLE = ROOT / "docs" / "sample_events_FICTIONAL.json"
META = "SAMPLE DATA (fictional, for documentation) | run 2026-01-15 07:00 | {n} events ({n} new this run)"
PORT = 9333                                 # DevTools port for the private browser instance

# ----------------------------------------------------------------------------- fictional people
ADMINS = [("Alex Morgan", "101", "Super Admin"), ("Priya Shah", "102", "Admin"), ("Jordan Lee", "103", "Admin")]
STAFF = [("Jane Doe", "1042"), ("Sam Rivera", "1043"), ("Chris Taylor", "1044"), ("Dana Kim", "1045"),
         ("Lee Patel", "1046"), ("Main Office", "20001"), ("Support Queue", "20002")]
IPS = ["203.0.113.10", "203.0.113.22", "198.51.100.7"]       # reserved documentation ranges, not real hosts
# (actionId, eventType exactly as the real API reports it, which is not a reliable signal)
ACTIONS = [("LOGIN_SUCCESSFULLY", "Create"), ("ASSIGN_USER", "CREATE"), ("ADD_EXTENSION:NON_USER", "Create"),
           ("ASSIGN_NUMBER", "Create"), ("ASSIGN_NUMBER:Department", "Create"), ("CHANGE_PHONE_NUMBER_TYPE", "Create"),
           ("CHANGE_REGIONAL_SETTINGS:NON_USER", "Create"), ("ADD_USER_TO_CQ", "Create"),
           ("ASSIGN_SMS_RECIPIENT:CALL_QUEUE", "UPDATE"), ("RELEASE_NUMBER", "Create"),
           ("DOWNLOAD_USER_LIST", "Create"), ("ADD_CUSTOM_RULE:BizLocation", "Create")]


def make_events():
    """Deterministic events over the 30 days before 2026-01-15, including 3 extension deletions."""
    rng = random.Random(7)
    base = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    events = []

    def add(action, etype, who, target, hours_ago, comment=None, params=None, fail=False):
        ev = {"id": str(uuid.UUID(int=rng.getrandbits(128), version=4)),
              "eventTime": (base - timedelta(hours=hours_ago, minutes=rng.randint(0, 59))).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
              "accountId": "1000000001", "actionId": action, "clientIp": rng.choice(IPS),
              "initiator": {"extensionId": "1000000001" if who[0] == "Alex Morgan" else "100000000" + who[1][-1],
                            "extensionNumber": who[1], "name": who[0], "role": who[2]}}
        if not fail:                       # real failed-login events carry no eventType or details
            ev["eventType"] = etype
            ev["details"] = {"parameters": params or [{"key": "", "value": ""}]}
        if target:
            ev["target"] = {"objectId": str(rng.randint(10**10, 10**11)), "objectType": "Extension",
                            "name": target[0], "extensionNumber": target[1]}
        if comment:
            ev["comment"] = comment
        events.append(ev)

    for i in range(40):                                         # general activity
        action, etype = rng.choice(ACTIONS)
        who = rng.choice(ADMINS)
        target = None if action.startswith(("LOGIN", "DOWNLOAD")) else rng.choice(STAFF)
        add(action, etype, who, target, rng.randint(1, 700))
    for who in rng.sample(ADMINS, 2):                           # a couple of failed sign-ins
        add("FAIL_LOGIN", None, who, None, rng.randint(20, 600), fail=True)
    for h, (name, ext), who in [(30, STAFF[3], ADMINS[0]), (95, STAFF[1], ADMINS[1]), (260, STAFF[4], ADMINS[0])]:
        add("DELETE_USER:USER", "Create", who, (name, ext), h)  # the events the project exists to find
    add("DELETE_CUSTOM_RULE:BizLocation", "Create", ADMINS[2], ("Main Office", "20001"), 410,
        comment="Deleted custom rule After-Hours Rule")
    add("RELEASE_NUMBER", "Create", ADMINS[1], ("Support Queue", "20002"), 150)
    events.sort(key=lambda e: e["eventTime"], reverse=True)
    return events


# ----------------------------------------------------------------------------- page variants
# name -> (JavaScript run after the viewer has drawn, helper state: False / "ok" / "failed", width, height)
CHIP = "document.querySelector('.chip[data-n=\"%s\"]').click();"
FAKE_FETCH = """<script>
/* Documentation only: stands in for the local helper so the settings panel can show a live-looking state. */
window.fetch=async(p,o)=>({ok:true,statusText:'OK',json:async()=>({
 config:{enabled:true,every_days:30,time:'07:00',window_days:35},
 task:{exists:true,state:'Ready',next:'2026-02-14T07:00:00',last:'2026-02-14T07:00:09'},
 last_run:__LAST__})});
</script>"""
LAST_OK = "{time:'2026-01-15T07:00:12',ok:true,message:'47 events collected, 47 new'}"
LAST_FAILED = ("{time:'2026-02-14T07:00:09',ok:false,message:'Setup needs attention (1 problem). First step: The JWT expired on "
               "2026-02-10. Create a new JWT credential and replace RC_JWT in .env.'}")
VARIANTS = {
    "01-overview": ("", False, 1500, 820),
    "02-filter-extension-deleted": (CHIP % "Extension deleted", False, 1500, 820),
    "03-filter-combined": (CHIP % "Anything deleted" +
                           "document.getElementById('who').value='Alex Morgan';document.getElementById('who').onchange();"
                           "document.getElementById('d1').value='2026-01-01';document.getElementById('d1').oninput();", False, 1500, 820),
    "04-column-filter": ("var i=document.querySelector('#f input[data-c=\"target.extensionNumber\"]');"
                         "i.value='1043';i.oninput();", False, 1500, 820),
    "05-schedule-settings": ("document.getElementById('gear').click();", "ok", 1500, 700),
    "06-schedule-warning": ("document.getElementById('gear').click();"   # the panel refills itself from the helper, so edit afterwards
                            "setTimeout(function(){var w=document.getElementById('s_win');w.value=10;w.oninput();},250);", True, 1500, 700),
    "07-schedule-readonly": ("document.getElementById('gear').click();", False, 1500, 560),
    "09-last-run-failed": ("document.getElementById('gear').click();", "failed", 1500, 700),
    "08-dark-mode": (CHIP % "Extension deleted" + "setTheme('dark',false);", False, 1500, 820),
}


# ----------------------------------------------------------------------------- minimal DevTools client
class CDP:
    """Just enough WebSocket + Chrome DevTools Protocol to navigate, wait for load and take a PNG."""

    def __init__(self, ws_url):
        u = urllib.parse.urlparse(ws_url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=60)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(1024)
        if b" 101 " not in buf.split(b"\r\n")[0]:
            raise RuntimeError("WebSocket handshake failed")
        self.next_id, self.events = 0, []

    def _read(self, n):
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise RuntimeError("DevTools connection closed")
            data += chunk
        return data

    def _frame(self, opcode, payload):          # client frames must be masked
        head = bytearray([0x80 | opcode])
        n = len(payload)
        head.append(0x80 | (n if n < 126 else 126 if n < 65536 else 127))
        head += struct.pack(">H", n) if 126 <= n < 65536 else struct.pack(">Q", n) if n >= 65536 else b""
        mask = os.urandom(4)
        self.sock.sendall(bytes(head) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _message(self):                         # returns one complete text message (handles fragments, pings)
        text = b""
        while True:
            b1, b2 = self._read(2)
            fin, opcode, n = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            if opcode == 0x9:
                self._frame(0xA, payload)
            elif opcode == 0x8:
                raise RuntimeError("DevTools closed the connection")
            elif opcode in (0x1, 0x0):
                text += payload
                if fin:
                    return json.loads(text)

    def call(self, method, **params):
        self.next_id += 1
        self._frame(0x1, json.dumps({"id": self.next_id, "method": method, "params": params}).encode())
        while True:
            m = self._message()
            if m.get("id") == self.next_id:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result", {})
            if "method" in m:
                self.events.append(m["method"])

    def wait_for(self, event, timeout=30):
        end = time.time() + timeout
        while event not in self.events:
            if time.time() > end:
                raise RuntimeError(f"timed out waiting for {event}")
            m = self._message()
            if "method" in m:
                self.events.append(m["method"])
        self.events.remove(event)


def find_browser():
    for p in [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"]:
        if Path(p).exists():
            return p
    return shutil.which("msedge") or shutil.which("google-chrome") or shutil.which("chromium")


def start_browser(exe):
    """Launch a private headless browser (own profile, so it never touches an Edge/Chrome you have open)."""
    subprocess.Popen([exe, "--headless=new", "--disable-gpu", "--no-first-run", f"--remote-debugging-port={PORT}",
                      f"--user-data-dir={BUILD / 'profile'}", "about:blank"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=2) as r:
                pages = [t for t in json.load(r) if t.get("type") == "page"]
            if pages:
                return pages[0]["webSocketDebuggerUrl"]
        except OSError:
            pass
        time.sleep(0.5)
    sys.exit("Could not reach the headless browser's DevTools port.")


def capture(cdp, page, png, w, h):
    cdp.call("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=1, mobile=False)
    cdp.events.clear()
    cdp.call("Page.navigate", url=page.as_uri())
    cdp.wait_for("Page.loadEventFired")
    time.sleep(1.0)                              # let the page's own script and the injected state settle
    shot = cdp.call("Page.captureScreenshot", format="png")
    png.write_bytes(base64.b64decode(shot["data"]))


def main():
    pages_only = "--pages-only" in sys.argv
    events = make_events()
    SAMPLE.write_text(json.dumps(events, indent=1), encoding="utf-8")
    print(f"wrote {SAMPLE.relative_to(ROOT)} ({len(events)} fictional events)")

    browser = None if pages_only else find_browser()
    if not pages_only and not browser:
        sys.exit("Edge or Chrome not found; sample data written. Use --pages-only to build the pages instead.")
    BUILD.mkdir(exist_ok=True)
    SHOTS.mkdir(parents=True, exist_ok=True)
    rows = [rc.flatten(e) for e in events]
    base = BUILD / "base.html"
    rc.write_viewer(base, rows, META.format(n=len(rows)))
    html0 = base.read_text(encoding="utf-8")

    pages = {}
    for name, (js, live, w, h) in VARIANTS.items():
        html = html0
        if live:   # pretend the local helper is running; live is "ok" or "failed" (what the last collection reported)
            fake = FAKE_FETCH.replace("__LAST__", LAST_FAILED if live == "failed" else LAST_OK)
            html = html.replace('"__TOKEN__"', '"sample-token"').replace("<body>", "<body>" + fake, 1)
        page = BUILD / f"{name}.html"
        page.write_text(html.replace("</body>", f"<script>{js}</script></body>"), encoding="utf-8")
        pages[name] = (page, w, h)
        if pages_only:
            print("built", page.relative_to(ROOT))
    if pages_only:
        return

    cdp = CDP(start_browser(browser))
    try:
        cdp.call("Page.enable")
        for name, (page, w, h) in pages.items():
            png = SHOTS / f"{name}.png"
            capture(cdp, page, png, w, h)
            print("captured", png.relative_to(ROOT))
    finally:
        try:
            cdp.call("Browser.close")
        except Exception:
            pass
    time.sleep(1)
    shutil.rmtree(BUILD, ignore_errors=True)


if __name__ == "__main__":
    main()
