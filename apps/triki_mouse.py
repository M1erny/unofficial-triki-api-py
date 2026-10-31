# Turns Triki motion into mouse input, with the same gestures as the Paint app:
#
#   tilt   forward/back/left/right moves the cursor, faster the further you tilt
#          (like a marble: lower the right edge and the cursor rolls right)
#   twist  turning it on its own axis, like a cap, scrolls (clockwise = down)
#   tap    a short tap on the controller clicks; two taps double-click
#   flip   turning it face-down pauses the mouse until it is turned back up
#
# "Level" (where the cursor stands still) is however you hold it when you switch the mouse on,
# and it is set again each time you turn it face-down and back up, like lifting a mouse to
# re-grip it.
#
# MouseEngine is pure Python and knows nothing about Windows: feed() it samples, take() the
# resulting movement, wheel and clicks. TrikiApps.py injects them into Windows or sends them
# to the Mouse page for the preview mode. test_mouse.py exercises it with synthetic motion.
import math
import sys
from collections import deque

NOMINAL_DT = 1 / 52

DEFAULTS = {
    "mode": "off",           # off | preview | live
    "rotation": 0,           # turns the tilt directions by 0/90/180/270°, like Paint's "Tilt direction"
    "speed": 14.0,           # cursor speed at full tilt = speed × 70 px/s
    "scroll": 1.0,           # wheel notches multiplier
    "tap": 0.45,             # tap threshold in g (lower = more sensitive)
    "click": "tap",          # tap | dwell | both
    "reverse_scroll": False,
}

TILT_DEADZONE, TILT_FULL = 0.05, 0.5     # sin(tilt): ignore under ~3°, full speed at 30° (as in Paint)
TWIST_DEADBAND = 12.0                    # °/s, as in the Timer and Paint
TWIST_DEG_PER_NOTCH = 10.0               # one wheel notch per 10° of twist
TAP_MAX_SECONDS = 0.12                   # a jolt shorter than this is a tap
TAP_GROUP_GAP = 0.28                     # taps closer than this belong to one gesture
DWELL_SECONDS, DWELL_TRAVEL = 0.9, 40.0


def _dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
def _cross(a, b): return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
def _scale(a, k): return (a[0] * k, a[1] * k, a[2] * k)
def _sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def _norm(a):
    n = math.sqrt(_dot(a, a))
    return (a[0] / n, a[1] / n, a[2] / n) if n > 1e-9 else (0.0, 0.0, 1.0)
def _lerp(lp, a, k): return a if lp is None else tuple(l + k * (x - l) for l, x in zip(lp, a))


class MouseEngine:
    def __init__(self):
        self.settings = dict(DEFAULTS)
        self.level = None        # the "neutral" up vector: where the cursor stands still
        self.relevel = False     # take the next orientation as level (after switching on)
        self.reset()

    # --- configuration ----------------------------------------------------------------------

    def reset(self):
        """Forget motion state (on connect, disconnect or mode change)."""
        self.bias = [0.0, 0.0, 0.0]
        self.bias_ready = False
        self.still = False
        self.win = deque(maxlen=40)
        self.lp_tap = None       # fast low-pass of accel, for tap detection
        self.lp_tilt = None      # tilt, smoothed like the Paint app
        self.lp_flip = None      # for face-down detection
        self.last_t = None
        self.paused = False
        self.flip_since = None
        self.up_since = None
        self.spike = None
        self.taps = []
        self.freeze_until = -1.0
        self.recent = deque()    # (t, dx, dy, wheel) recently emitted, so a tap or flip can undo it
        self.travel = 0.0
        self.last_motion_t = -1.0
        self._out = {"dx": 0.0, "dy": 0.0, "wheel": 0.0, "clicks": 0, "events": []}

    def configure(self, **changes):
        """Applies validated settings; returns True if anything changed."""
        changed = False
        for key, value in changes.items():
            if key not in DEFAULTS:
                continue
            if key == "mode" and value not in ("off", "preview", "live"): continue
            if key == "click" and value not in ("tap", "dwell", "both"): continue
            if key == "rotation":
                try:
                    value = int(value) % 360
                except (TypeError, ValueError):
                    continue
                if value not in (0, 90, 180, 270): continue
            if key in ("speed", "scroll", "tap"):
                lo, hi = {"speed": (2, 60), "scroll": (0.2, 5), "tap": (0.15, 1.5)}[key]
                value = min(hi, max(lo, float(value)))
            if key == "reverse_scroll":
                value = bool(value)
            if self.settings[key] != value:
                old = self.settings[key]
                self.settings[key] = value
                changed = True
                if key == "mode":
                    self.paused = False
                    self.taps, self.spike = [], None
                    if old == "off":
                        self.relevel = True      # however it is held right now is "level"
        return changed

    def calibrate(self):
        """The current orientation becomes 'level' (no movement)."""
        if self.lp_tilt is not None:
            self.level = _norm(self.lp_tilt)
            return True
        return False

    def public_state(self):
        return dict(self.settings, paused=self.paused, calibrated=self.level is not None)

    @property
    def active(self):
        return self.settings["mode"] != "off" and not self.paused

    # --- output ---------------------------------------------------------------------------------

    def take(self):
        """Returns and clears what happened since the last call."""
        out = self._out
        self._out = {"dx": 0.0, "dy": 0.0, "wheel": 0.0, "clicks": 0, "events": []}
        return out

    def _emit(self, t, dx=0.0, dy=0.0, wheel=0.0):
        self._out["dx"] += dx
        self._out["dy"] += dy
        self._out["wheel"] += wheel
        if dx or dy or wheel:
            self.recent.append((t, dx, dy, wheel))

    def _undo_between(self, t_from, t_to, wheel_too=False):
        dx = dy = wh = 0.0
        kept = deque()
        for item in self.recent:
            if t_from <= item[0] <= t_to:
                dx += item[1]; dy += item[2]; wh += item[3]
            else:
                kept.append(item)
        self.recent = kept
        self._out["dx"] -= dx
        self._out["dy"] -= dy
        if wheel_too:
            self._out["wheel"] -= wh

    def _event(self, name):
        self._out["events"].append(name)

    # --- per sample -----------------------------------------------------------------------------

    def feed(self, t, gyro_dps, accel_g):
        dt = NOMINAL_DT if self.last_t is None else t - self.last_t
        if not (0.001 < dt < 0.15):
            dt = NOMINAL_DT
        self.last_t = t
        while self.recent and t - self.recent[0][0] > 1.0:
            self.recent.popleft()

        # Gyro bias: learn it whenever the controller rests for ~0.8 s.
        amag = math.sqrt(_dot(accel_g, accel_g))
        self.win.append((gyro_dps, amag))
        self.still = False
        if len(self.win) == self.win.maxlen:
            mean = [sum(w[0][i] for w in self.win) / len(self.win) for i in range(3)]
            dev = max(abs(w[0][i] - mean[i]) for w in self.win for i in range(3))
            if dev < 2.0 and max(abs(w[1] - 1) for w in self.win) < 0.08:
                self.still = True
                k = 0.02 if self.bias_ready else 1.0
                self.bias = [b + k * (m - b) for b, m in zip(self.bias, mean)]
                self.bias_ready = True
        w = (gyro_dps[0] - self.bias[0], gyro_dps[1] - self.bias[1], gyro_dps[2] - self.bias[2])

        hp = 0.0 if self.lp_tap is None else math.sqrt(_dot(_sub(accel_g, self.lp_tap), _sub(accel_g, self.lp_tap)))
        self.lp_tap = _lerp(self.lp_tap, accel_g, 0.2)
        if self.lp_tilt is None or hp < self.settings["tap"] * 0.7:   # a tap's jolt is not a tilt
            self.lp_tilt = _lerp(self.lp_tilt, accel_g, 0.18)
        self.lp_flip = _lerp(self.lp_flip, accel_g, 0.15)

        if self.settings["mode"] == "off":
            return
        if self.relevel:
            self.relevel = False
            self.calibrate()

        rate = math.sqrt(_dot(w, w))
        self._check_flip(t, rate)
        if self.paused:
            return
        self._check_taps(t, hp, rate)

        if t < self.freeze_until:
            return
        dx, dy = self._tilt(dt)

        wheel = 0.0
        twist = -w[2]                                          # clockwise seen from the face is negative about +Z
        if abs(twist) > TWIST_DEADBAND:
            r = twist - math.copysign(TWIST_DEADBAND, twist)
            wheel = -r * dt / TWIST_DEG_PER_NOTCH * self.settings["scroll"]   # clockwise → scroll down
            if self.settings["reverse_scroll"]:
                wheel = -wheel
            dx *= 0.3; dy *= 0.3                               # steadier while scrolling
        self._emit(t, dx, dy, wheel)

        step = math.hypot(dx, dy)
        self.travel += step
        if step > 0.6:
            self.last_motion_t = t
        if (self.settings["click"] in ("dwell", "both") and self.travel >= DWELL_TRAVEL
                and t - self.last_motion_t >= DWELL_SECONDS):
            self.travel = 0.0
            self._out["clicks"] += 1
            self._event("dwell")

    def _tilt(self, dt):
        u = _norm(self.lp_tilt)
        n = self.level or (0.0, 0.0, 1.0)
        d = _sub(u, _scale(n, _dot(u, n)))
        e1 = _sub((1.0, 0.0, 0.0), _scale(n, n[0]))
        if math.sqrt(_dot(e1, e1)) < 0.3:
            e1 = _sub((0.0, 1.0, 0.0), _scale(n, n[1]))
        e1 = _norm(e1)
        e2 = _cross(n, e1)
        sx, sy = -_dot(d, e1), _dot(d, e2)                     # like a marble: lower the +X edge → right
        for _ in range(self.settings["rotation"] // 90):
            sx, sy = -sy, sx
        m = math.hypot(sx, sy)
        if m <= TILT_DEADZONE:
            return 0.0, 0.0
        f = min(1.0, (m - TILT_DEADZONE) / (TILT_FULL - TILT_DEADZONE)) ** 1.6
        speed = self.settings["speed"] * 70 * f                # px/s
        return sx / m * speed * dt, sy / m * speed * dt

    def _check_flip(self, t, rate):
        z = _dot(_norm(self.lp_flip), self.level or (0.0, 0.0, 1.0))
        if not self.paused:
            if z < -0.6:
                self.flip_since = self.flip_since if self.flip_since is not None else t
                if t - self.flip_since > 0.15:
                    self.paused = True
                    self.up_since = None
                    self._undo_between(t - 0.6, t, wheel_too=True)   # the flip itself moved and scrolled
                    self.taps, self.spike = [], None
                    self._event("pause")
            else:
                self.flip_since = None
        else:
            if z > 0.0 and rate < 60:                          # not face-down, and the hand has settled
                self.up_since = self.up_since if self.up_since is not None else t
                if t - self.up_since > 0.35:
                    self.paused = False
                    self.flip_since = None
                    self.freeze_until = t + 0.1
                    self.lp_tilt = self.lp_flip                # settle on the new grip, then level there
                    self.calibrate()
                    self._event("resume")
            else:
                self.up_since = None

    def _check_taps(self, t, hp, rate):
        threshold = self.settings["tap"]
        if hp > threshold:
            if self.spike is None:
                if rate < 200:                                 # a jolt during a fast swing is movement
                    self.spike = t
                    self.freeze_until = t + TAP_MAX_SECONDS    # hold the cursor while we find out
            elif t - self.spike > 0.25:
                self.spike = None                              # a long jolt is movement, not a tap
        elif self.spike is not None and hp < threshold * 0.5:
            if t - self.spike <= TAP_MAX_SECONDS:
                self.taps.append(self.spike)
                self._undo_between(self.spike - 0.025, self.spike)  # the sample the tap jerked: put it back
            self.spike = None

        if self.taps and self.spike is None and t - self.taps[-1] > TAP_GROUP_GAP:
            n = len(self.taps)
            self.taps = []
            if self.settings["click"] == "dwell":
                return
            if n == 1:
                self._out["clicks"] += 1
                self._event("click")
            elif n == 2:
                self._out["clicks"] += 2
                self._event("double")
            else:
                self._event("shake")                           # 3+ jolts: a shake, not a click


class WindowsMouse:
    """Moves the real Windows cursor with SendInput (relative moves, wheel, left clicks)."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        ULONG_PTR = ctypes.c_size_t

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class _U(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        self._MOUSEINPUT, self._INPUT, self._U = MOUSEINPUT, INPUT, _U
        self._send = ctypes.windll.user32.SendInput
        self._send.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        self._send.restype = wintypes.UINT
        self._size = ctypes.sizeof(INPUT)

    def _mouse(self, flags, dx=0, dy=0, data=0):
        mi = self._MOUSEINPUT(int(dx), int(dy), data & 0xFFFFFFFF, flags, 0, 0)
        return self._INPUT(0, self._U(mi=mi))

    def _emit(self, *inputs):
        arr = (self._INPUT * len(inputs))(*inputs)
        return self._send(len(inputs), arr, self._size)

    def move(self, dx, dy):
        return self._emit(self._mouse(0x0001, dx, dy))                 # MOUSEEVENTF_MOVE

    def wheel(self, delta):
        return self._emit(self._mouse(0x0800, data=int(delta)))        # MOUSEEVENTF_WHEEL, +120 = one notch up

    def click(self, count=1):
        downs_ups = []
        for _ in range(count):
            downs_ups += [self._mouse(0x0002), self._mouse(0x0004)]   # LEFTDOWN, LEFTUP
        return self._emit(*downs_ups)


def system_mouse():
    """The real-cursor backend, or None where it isn't supported."""
    if sys.platform != "win32":
        return None
    try:
        return WindowsMouse()
    except Exception:
        return None
