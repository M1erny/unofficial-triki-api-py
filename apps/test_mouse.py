# Tests the Triki mouse gestures with synthetic 52 Hz motion: python apps/test_mouse.py
import unittest

from triki_mouse import MouseEngine

DT = 1 / 52
FLAT = (0.0, 0.0, 1.0)                  # lying flat / held flat, face up
RIGHT_DOWN = (-0.3, 0.0, 0.954)         # right (+X) edge lowered ~17°
AWAY_DOWN = (0.0, -0.3, 0.954)          # far (+Y) edge lowered ~17°
BIAS = (0.8, -0.4, 0.6)                 # a realistic resting gyro offset in °/s
CLOCKWISE = (0, 0, -120)                # twisting like a cap, clockwise seen from the face


class Sim:
    """Feeds an engine like the server does: samples in bursts of three, outputs collected."""

    def __init__(self, **settings):
        self.engine = MouseEngine()
        self.engine.configure(mode="preview", **settings)
        self.t = 100.0
        self.n = 0
        self.total = self._empty()
        self.hold(1.0)                       # rests flat: learns the gyro bias and the level
        self.clear()

    @staticmethod
    def _empty():
        return {"dx": 0.0, "dy": 0.0, "wheel": 0.0, "clicks": 0, "events": []}

    def feed(self, gyro=(0, 0, 0), accel=FLAT):
        self.t += DT
        self.engine.feed(self.t, tuple(b + x for b, x in zip(BIAS, gyro)), accel)
        self.n += 1
        if self.n % 3 == 0:
            self.collect()

    def collect(self):
        out = self.engine.take()
        for k in ("dx", "dy", "wheel", "clicks"):
            self.total[k] += out[k]
        self.total["events"] += out["events"]

    def run(self, seconds, gyro=(0, 0, 0), accel=FLAT):
        for _ in range(round(seconds / DT)):
            self.feed(gyro, accel)
        self.collect()
        return self.total

    def hold(self, seconds, accel=FLAT):
        return self.run(seconds, accel=accel)

    def tap(self, accel=FLAT):
        self.feed(accel=tuple(a + j for a, j in zip(accel, (0.9, 0.0, 0.0))))   # one-sample jolt
        self.feed(accel=accel)

    def clear(self):
        self.collect()
        self.total = self._empty()


class Tilt(unittest.TestCase):
    def test_resting_controller_stays_put(self):
        out = Sim().hold(3.0)
        self.assertLess(abs(out["dx"]) + abs(out["dy"]), 1.0)
        self.assertEqual((out["wheel"], out["clicks"]), (0.0, 0))

    def test_level_is_set_when_switched_on(self):
        self.assertTrue(Sim().engine.public_state()["calibrated"])

    def test_switching_on_while_held_on_its_side_does_not_drift(self):
        e = MouseEngine()
        on_side = (-0.55, 0.69, 0.25)            # how the real Triki was lying: ~74° from flat
        t = 0.0
        for _ in range(52):                      # resting like that while the mouse is off
            t += DT; e.feed(t, BIAS, on_side)
        e.configure(mode="preview")
        for _ in range(104):
            t += DT; e.feed(t, BIAS, on_side)
        out = e.take()
        self.assertLess(abs(out["dx"]) + abs(out["dy"]), 1.0)

    def test_tilt_right_moves_right(self):
        out = Sim().run(1.0, accel=RIGHT_DOWN)
        self.assertGreater(out["dx"], 100)
        self.assertLess(abs(out["dy"]), 5)

    def test_tilt_left_moves_left(self):
        self.assertLess(Sim().run(1.0, accel=(0.3, 0.0, 0.954))["dx"], -100)

    def test_tilt_away_moves_up(self):
        out = Sim().run(1.0, accel=AWAY_DOWN)
        self.assertLess(out["dy"], -100)
        self.assertLess(abs(out["dx"]), 5)

    def test_tilt_towards_you_moves_down(self):
        self.assertGreater(Sim().run(1.0, accel=(0.0, 0.3, 0.954))["dy"], 100)

    def test_further_tilt_is_faster(self):
        gentle = Sim().run(1.0, accel=(-0.15, 0.0, 0.989))["dx"]
        steep = Sim().run(1.0, accel=(-0.4, 0.0, 0.917))["dx"]
        self.assertGreater(steep, gentle * 2)

    def test_tiny_tilt_is_ignored(self):
        self.assertEqual(Sim().run(1.0, accel=(-0.04, 0.0, 0.999))["dx"], 0.0)

    def test_rotation_setting_turns_directions(self):
        out = Sim(rotation=90).run(1.0, accel=RIGHT_DOWN)
        self.assertGreater(abs(out["dy"]), 100)
        self.assertLess(abs(out["dx"]), 5)

    def test_set_level_at_an_angle(self):
        s = Sim()
        s.hold(1.5, accel=RIGHT_DOWN)
        s.engine.calibrate()
        s.clear()
        out = s.hold(1.0, accel=RIGHT_DOWN)
        self.assertLess(abs(out["dx"]) + abs(out["dy"]), 2.0)


class Twist(unittest.TestCase):
    def test_clockwise_scrolls_down(self):
        out = Sim().run(0.5, gyro=CLOCKWISE)
        self.assertLess(out["wheel"], -4)                  # 60° of twist ≈ 5 notches
        self.assertLess(abs(out["dx"]) + abs(out["dy"]), 1)

    def test_counter_clockwise_scrolls_up(self):
        self.assertGreater(Sim().run(0.5, gyro=(0, 0, 120))["wheel"], 4)

    def test_reverse_scroll(self):
        self.assertGreater(Sim(reverse_scroll=True).run(0.5, gyro=CLOCKWISE)["wheel"], 4)

    def test_slow_drift_does_not_scroll(self):
        self.assertEqual(Sim().run(1.0, gyro=(0, 0, -8))["wheel"], 0.0)

    def test_tilting_does_not_scroll(self):
        self.assertEqual(Sim().run(1.0, gyro=(60, -60, 0), accel=RIGHT_DOWN)["wheel"], 0.0)

    def test_twist_while_tilted_still_scrolls(self):
        s = Sim()
        s.hold(0.5, accel=RIGHT_DOWN)
        s.clear()
        self.assertLess(s.run(0.5, gyro=CLOCKWISE, accel=RIGHT_DOWN)["wheel"], -4)


class Clicks(unittest.TestCase):
    def test_single_tap_clicks_once(self):
        s = Sim()
        s.tap()
        out = s.hold(0.5)
        self.assertEqual(out["clicks"], 1)
        self.assertIn("click", out["events"])

    def test_double_tap_double_clicks(self):
        s = Sim()
        s.tap(); s.hold(0.15); s.tap()
        out = s.hold(0.5)
        self.assertEqual(out["clicks"], 2)
        self.assertIn("double", out["events"])

    def test_tap_does_not_move_the_cursor(self):
        s = Sim()
        s.tap()
        out = s.hold(0.5)
        self.assertLess(abs(out["dx"]) + abs(out["dy"]), 3)

    def test_tap_while_moving_keeps_the_cursor_on_course(self):
        s = Sim()
        s.run(0.5, accel=RIGHT_DOWN)
        before = s.total["dx"]
        s.tap(accel=RIGHT_DOWN)
        s.clear()
        self.assertEqual(s.hold(0.4, accel=FLAT)["clicks"], 1)
        self.assertGreater(before, 50)

    def test_shake_is_not_a_click(self):
        s = Sim()
        for _ in range(5):
            s.tap(); s.hold(0.08)
        out = s.hold(0.6)
        self.assertEqual(out["clicks"], 0)
        self.assertIn("shake", out["events"])

    def test_tilting_never_clicks(self):
        s = Sim()
        s.run(1.0, accel=RIGHT_DOWN); s.run(1.0, accel=AWAY_DOWN); s.run(1.0)
        self.assertEqual(s.total["clicks"], 0)

    def test_dwell_click(self):
        s = Sim(click="dwell")
        s.run(0.5, accel=RIGHT_DOWN)
        out = s.hold(1.4)
        self.assertEqual(out["clicks"], 1)
        self.assertIn("dwell", out["events"])

    def test_dwell_mode_ignores_taps(self):
        s = Sim(click="dwell")
        s.tap()
        self.assertEqual(s.hold(0.5)["clicks"], 0)


class Pause(unittest.TestCase):
    def test_face_down_pauses_and_face_up_resumes(self):
        s = Sim()
        out = s.hold(1.0, accel=(0.0, 0.0, -1.0))
        self.assertIn("pause", out["events"])
        self.assertTrue(s.engine.paused)
        s.clear()
        out = s.run(0.5, gyro=CLOCKWISE, accel=(0.0, 0.0, -1.0))
        self.assertEqual((out["dx"], out["wheel"]), (0.0, 0.0))
        s.hold(1.0)
        self.assertFalse(s.engine.paused)
        s.clear()
        self.assertGreater(s.run(1.0, accel=RIGHT_DOWN)["dx"], 100)

    def test_flip_and_back_relevels_at_the_new_grip(self):
        s = Sim()
        s.hold(1.0, accel=(0.0, 0.0, -1.0))      # face-down: pause
        s.hold(1.0, accel=RIGHT_DOWN)            # picked up again, held at a new angle
        self.assertFalse(s.engine.paused)
        s.clear()
        out = s.hold(1.0, accel=RIGHT_DOWN)      # that new angle is now level: no drift
        self.assertLess(abs(out["dx"]) + abs(out["dy"]), 2.0)
        s.clear()
        self.assertLess(s.run(1.0, accel=FLAT)["dx"], -50)   # tilting back from it moves left

    def test_off_mode_emits_nothing(self):
        s = Sim()
        s.engine.configure(mode="off")
        s.run(0.5, accel=RIGHT_DOWN)
        s.tap()
        out = s.hold(0.5)
        self.assertEqual((out["dx"], out["clicks"]), (0.0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
