# Triki apps: a live dashboard, a twist timer and a paint app for the Triki controller.
#
# Holds the BLE connection (via TrikiPy) and serves the three pages in this folder
# plus a Server-Sent Events stream of every IMU sample on http://127.0.0.1:8770.
# Only the standard library and bleak are needed.
#
#   python apps/TrikiApps.py                 # opens the dashboard in your browser
#   python apps/TrikiApps.py --page timer    # or the timer / paint app
#   python apps/TrikiApps.py --no-open       # just serve
#   pythonw apps/TrikiApps.py --app --exit-when-idle 20
#                                            # what the desktop shortcuts run: no console,
#                                            # app-style window, quits once it is closed
#
# Note on channel order: the 14-byte packet carries the gyroscope first and the
# accelerometer second (LSM6-style register order). TrikiPy names them the other
# way round, so TrikiData.ax/ay/az is really gyro X/Y/Z and gx/gy/gz is accel X/Y/Z.
# At rest the second triple has a magnitude of ~2048 = 1 g at the +-16 g range the
# wake command selects, which confirms it.
import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import math
import struct
import tempfile
import wave
import webbrowser
from collections import deque
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))     # TrikiPy.py lives in the repository root
from TrikiPy import TrikiDevice

try:
    import winsound
except ImportError:          # not on Windows: the timer page falls back to browser audio
    winsound = None

PAGES = {"/": HERE / "dashboard.html", "/timer": HERE / "timer.html", "/paint": HERE / "paint.html"}
NOMINAL_DT = 1 / 52          # wake command selects a 52 Hz output data rate
HISTORY_SECONDS = 30         # replayed to a browser tab when it (re)connects
BATTERY_POLL_SECONDS = 30
ALARM_MAX_SECONDS = 180      # the alarm silences itself after this long


def alarm_wav() -> Path:
    """Writes a short beep-beep-BEEP pattern that loops well, and returns its path."""
    path = Path(tempfile.gettempdir()) / "triki_timer_alarm_v1.wav"
    if path.exists():
        return path
    rate, frames = 22050, bytearray()
    for freq, dur in [(880, 0.12), (0, 0.07), (880, 0.12), (0, 0.07), (1175, 0.22), (0, 0.65)]:
        n = int(rate * dur)
        for i in range(n):
            if freq:
                env = min(1.0, i / (rate * 0.004), (n - i) / (rate * 0.03))
                x = 2 * math.pi * freq * i / rate
                v = int(11000 * env * (math.sin(x) + 0.25 * math.sin(2 * x)))
            else:
                v = 0
            frames += struct.pack("<h", v)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


class TrikiHub:
    """Owns the controller connection and fans samples out to browser clients."""

    def __init__(self, bt_name: str):
        self.bt_name = bt_name
        self.triki = None
        self.clients = set()
        self.idle_since = time.monotonic()
        self.alarm_task = None
        self.blink_task = None
        self.history = deque(maxlen=int(HISTORY_SECONDS / NOMINAL_DT))
        self.seq = 0
        self.t_last = None
        self.t0 = time.perf_counter()
        self.status = {
            "boot": int(time.time() * 1000),   # lets the page notice a server restart
            "state": "starting", "name": None, "address": None, "firmware": None,
            "battery": None, "led": False, "message": "",
        }

    # --- broadcasting -----------------------------------------------------

    def _broadcast(self, event: str, payload: dict):
        msg = f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()
        for q in list(self.clients):
            if q.qsize() < 500:      # drop frames for a stalled tab instead of buffering forever
                q.put_nowait(msg)

    def set_status(self, **changes):
        self.status.update(changes)
        self._broadcast("status", self.status)

    def _timestamp(self) -> float:
        # Notifications arrive in bursts of ~3 samples, so arrival time alone is
        # jagged. Advance by the nominal sample period and pull gently towards
        # the wall clock; re-anchor after a gap (e.g. a reconnect).
        now = time.perf_counter() - self.t0
        if self.t_last is None or abs(now - (self.t_last + NOMINAL_DT)) > 0.5:
            t = now
        else:
            pred = self.t_last + NOMINAL_DT
            t = pred + 0.03 * (now - pred)
        self.t_last = t
        return round(t, 4)

    def _push_samples(self, samples):
        rows = []
        for d in samples:
            self.seq += 1
            # [seq, t, gyroX, gyroY, gyroZ, accelX, accelY, accelZ] as raw int16
            row = [self.seq, self._timestamp(), d.ax, d.ay, d.az, d.gx, d.gy, d.gz]
            rows.append(row)
            self.history.append(row)
        self._broadcast("imu", {"s": rows})

    # --- BLE --------------------------------------------------------------

    async def run(self):
        while True:
            triki = TrikiDevice(BTName=self.bt_name, literal=False)
            self.set_status(state="scanning", message=f'Looking for "{self.bt_name}"')
            try:
                if not await triki.connectTriki(timeout=8.0):
                    self.set_status(state="not_found", message="Not found - make sure it is on and not connected to your phone")
                    await asyncio.sleep(2)
                    continue

                self.set_status(state="connecting", name=triki.getName(),
                                address=triki._client.address,
                                firmware=triki.getFirmwareVersion(),
                                battery=await triki.getBatteryLevel(), led=False,
                                message="Waking the motion sensor")
                if not await triki.startTriki():
                    raise RuntimeError("wake command failed")

                self.triki = triki
                self.set_status(state="streaming", message="")
                await self._stream(triki)
                self.set_status(state="reconnecting", message="Connection lost - reconnecting")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.set_status(state="reconnecting", message=f"Error: {e}")
            finally:
                self.triki = None
                if triki._client and triki._client.is_connected:
                    # Puts the IMU back to sleep (and the LED off) to save battery.
                    await asyncio.shield(triki.stopTriki())
            await asyncio.sleep(1)

    async def _stream(self, triki: TrikiDevice):
        next_battery = time.monotonic() + BATTERY_POLL_SECONDS
        while True:
            try:
                first = await asyncio.wait_for(triki.getTrikiData(), timeout=2.0)
            except asyncio.TimeoutError:
                if not triki._client.is_connected:
                    return
                continue
            batch = [first]
            while not triki._data_queue.empty():
                batch.append(triki._data_queue.get_nowait())
            self._push_samples(batch)

            if time.monotonic() >= next_battery:
                next_battery = time.monotonic() + BATTERY_POLL_SECONDS
                level = await triki.getBatteryLevel()
                if level >= 0 and level != self.status["battery"]:
                    self.set_status(battery=level)

    async def set_led(self, on: bool) -> bool:
        if not self.triki:
            return False
        ok = await self.triki.setLED(on)
        if ok:
            self.set_status(led=on)
        return ok

    # --- timer alarm --------------------------------------------------------

    def set_alarm(self, on: bool) -> bool:
        """Starts/stops the alarm: LED flashing plus a looping sound. Returns True if sound plays here."""
        if self.alarm_task:
            self.alarm_task.cancel()
            self.alarm_task = None
        if winsound:
            winsound.PlaySound(None, 0)
        if not on:
            return False
        sound = False
        if winsound:
            try:
                winsound.PlaySound(str(alarm_wav()), winsound.SND_FILENAME | winsound.SND_ASYNC
                                   | winsound.SND_LOOP | winsound.SND_NODEFAULT)
                sound = True
            except RuntimeError:
                pass
        self.alarm_task = asyncio.create_task(self._flash_led(period=0.3, duration=ALARM_MAX_SECONDS))
        return sound

    def blink(self, times: int):
        if self.blink_task and not self.blink_task.done():
            return
        self.blink_task = asyncio.create_task(self._flash_led(period=0.15, count=max(1, min(times, 10)) * 2))

    async def _flash_led(self, period: float, count: int = None, duration: float = None):
        end = time.monotonic() + duration if duration else None
        on, n = False, 0
        try:
            while (count is None or n < count) and (end is None or time.monotonic() < end):
                on = not on
                n += 1
                if self.triki:
                    await self.triki.setLED(on)
                await asyncio.sleep(period)
            if duration and winsound:       # alarm timed out on its own
                winsound.PlaySound(None, 0)
        finally:
            if self.triki:
                await asyncio.shield(self.triki.setLED(self.status["led"]))

    # --- HTTP -------------------------------------------------------------

    async def handle_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request = await reader.readline()
            parts = request.decode("latin-1").split()
            if len(parts) < 2:
                return
            method, path = parts[0], parts[1].split("?")[0]
            headers = {}
            while (line := await reader.readline()) not in (b"\r\n", b"\n", b""):
                key, _, value = line.decode("latin-1").partition(":")
                headers[key.strip().lower()] = value.strip()

            if method == "GET" and path in PAGES:
                await self._respond(writer, 200, "text/html; charset=utf-8", PAGES[path].read_bytes())
            elif method == "GET" and path == "/events":
                await self._serve_events(writer)
            elif method == "POST" and path in ("/api/led", "/api/alarm", "/api/blink"):
                length = int(headers.get("content-length", 0) or 0)
                body = json.loads((await reader.readexactly(length)) or b"{}") if length else {}
                code = 200
                if path == "/api/led":
                    ok = await self.set_led(bool(body.get("on")))
                    code = 200 if ok else 409
                    result = {"ok": ok, "led": self.status["led"]}
                elif path == "/api/alarm":
                    result = {"ok": True, "sound": self.set_alarm(bool(body.get("on")))}
                else:
                    self.blink(int(body.get("times", 2)))
                    result = {"ok": True}
                await self._respond(writer, code, "application/json", json.dumps(result).encode())
            else:
                await self._respond(writer, 404, "text/plain", b"Not found")
        except (ConnectionError, asyncio.IncompleteReadError, json.JSONDecodeError):
            pass
        finally:
            writer.close()

    async def _respond(self, writer, code: int, ctype: str, body: bytes):
        reason = {200: "OK", 404: "Not Found", 409: "Conflict"}[code]
        writer.write(f"HTTP/1.1 {code} {reason}\r\nContent-Type: {ctype}\r\n"
                     f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n"
                     f"Connection: close\r\n\r\n".encode() + body)
        await writer.drain()

    async def _serve_events(self, writer: asyncio.StreamWriter):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                     b"Cache-Control: no-store\r\nConnection: keep-alive\r\n\r\n")
        q = asyncio.Queue()
        q.put_nowait(f"event: status\ndata: {json.dumps(self.status)}\n\n".encode())
        if self.history:
            q.put_nowait(f"event: imu\ndata: {json.dumps({'s': list(self.history), 'history': True}, separators=(',', ':'))}\n\n".encode())
        self.clients.add(q)
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    msg = b": keep-alive\n\n"
                writer.write(msg)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            self.clients.discard(q)
            self.idle_since = time.monotonic()
            if not self.clients and self.alarm_task:
                self.set_alarm(False)        # never leave an alarm ringing with no page open

    async def exit_when_idle(self, seconds: float):
        """Returns once no dashboard tab has been open for `seconds`."""
        self.idle_since = time.monotonic()
        while True:
            await asyncio.sleep(2)
            if not self.clients and time.monotonic() - self.idle_since > seconds:
                return


def open_dashboard(url: str, app_window: bool):
    """Opens the page, as a chromeless Edge/Chrome app window if asked and available."""
    if app_window:
        candidates = [
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
        for exe in candidates:
            if exe.is_file():
                # The autoplay switch lets the timer tick without a click first; Edge
                # ignores it when it is already running, and the page then asks for a click.
                subprocess.Popen([str(exe), f"--app={url}", "--window-size=1440,960",
                                  "--autoplay-policy=no-user-gesture-required"])
                return
    webbrowser.open(url)


async def main():
    parser = argparse.ArgumentParser(description="Live web dashboard for the Triki controller")
    parser.add_argument("--name", default="Triki", help="BLE name prefix to look for")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    parser.add_argument("--app", action="store_true", help="open as an app window (Edge/Chrome) instead of a tab")
    parser.add_argument("--exit-when-idle", type=float, metavar="SECONDS",
                        help="quit once no dashboard has been open for this long")
    parser.add_argument("--page", default="dashboard", choices=["dashboard", "timer", "paint"],
                        help="which app to open (default: dashboard)")
    args = parser.parse_args()

    hub = TrikiHub(args.name)
    page = "" if args.page == "dashboard" else args.page
    url = f"http://{args.host}:{args.port}/{page}"
    try:
        server = await asyncio.start_server(hub.handle_http, args.host, args.port)
    except OSError:
        # Already running (e.g. the shortcut was double-clicked twice): just show it.
        print(f"Port {args.port} is busy; assuming the Triki apps are already running at {url}")
        if not args.no_open:
            open_dashboard(url, args.app)
        return
    print(f"Triki apps running at {url}  (Ctrl+C to stop)", flush=True)
    if not args.no_open:
        open_dashboard(url, args.app)

    async with server:
        tasks = [asyncio.create_task(server.serve_forever()), asyncio.create_task(hub.run())]
        if args.exit_when_idle:
            tasks.append(asyncio.create_task(hub.exit_when_idle(args.exit_when_idle)))
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Cancelling hub.run() puts the controller back to sleep before exiting.
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped.")
