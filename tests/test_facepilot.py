"""Unit tests for the webcam-independent parts of FacePilot.

These tests deliberately avoid a camera, MediaPipe, a display server, and any
real pointer/keyboard injection. They cover the EAR calculation, the cursor
math, and the wink/double-click state machine.
"""

from __future__ import annotations

import math
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import facepilot as fp


class FakeMouse:
    """Minimal MouseManager stand-in that records side effects."""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def move(self, dx: int, dy: int) -> None:
        self.events.append(("move", dx, dy))

    def click(self, button: str = "left", count: int = 1) -> None:
        self.events.append(("click", button, count))

    def sync_position(self) -> None:
        self.events.append(("sync",))

    @property
    def clicks(self) -> list[tuple]:
        return [event for event in self.events if event[0] == "click"]


def landmark(x: float, y: float) -> SimpleNamespace:
    return SimpleNamespace(x=x, y=y)


def eye_landmarks(vertical_offset: float) -> list[SimpleNamespace]:
    """Build eight synthetic landmarks for one eye.

    The horizontal corners are fixed; ``vertical_offset`` controls how far the
    three top/bottom pairs are apart.
    """
    top = 0.40
    bottom = top + vertical_offset
    return [
        landmark(0.20, 0.50),  # corner 1
        landmark(0.40, 0.50),  # corner 2
        landmark(0.25, top),
        landmark(0.25, bottom),
        landmark(0.30, top),
        landmark(0.30, bottom),
        landmark(0.35, top),
        landmark(0.35, bottom),
    ]


EYE_DEF = {
    "corners": (0, 1),
    "vertical_1": (2, 3),
    "vertical_2": (4, 5),
    "vertical_3": (6, 7),
}


class TestCalculateEar(unittest.TestCase):
    def test_open_eye_has_higher_ear_than_closed_eye(self) -> None:
        opened = fp.calculate_ear(eye_landmarks(0.05), EYE_DEF, 640, 480)
        closed = fp.calculate_ear(eye_landmarks(0.01), EYE_DEF, 640, 480)
        self.assertGreater(opened, closed)
        self.assertAlmostEqual(opened, 0.1875, places=4)
        self.assertAlmostEqual(closed, 0.0375, places=4)

    def test_degenerate_horizontal_distance_returns_zero(self) -> None:
        landmarks = eye_landmarks(0.05)
        landmarks[1] = landmark(0.20, 0.50)  # same point as the other corner
        self.assertEqual(fp.calculate_ear(landmarks, EYE_DEF, 640, 480), 0.0)


class TestJoystickController(unittest.TestCase):
    def make_controller(self) -> fp.JoystickController:
        controller = fp.JoystickController(
            deadzone=25.0,
            speed_factor=0.35,
            max_speed=40.0,
            pos_alpha=1.0,
            vel_alpha=1.0,
        )
        controller.calibrate(320.0, 240.0)
        return controller

    def test_cursor_is_stationary_inside_deadzone(self) -> None:
        controller = self.make_controller()
        move_x, move_y, distance, outside = controller.update(330.0, 240.0)
        self.assertEqual((move_x, move_y), (0, 0))
        self.assertEqual(distance, 10.0)
        self.assertFalse(outside)

    def test_cursor_moves_in_the_expected_direction(self) -> None:
        controller = self.make_controller()
        move_x, move_y, distance, outside = controller.update(420.0, 240.0)
        self.assertTrue(outside)
        self.assertGreater(move_x, 0)
        self.assertEqual(move_y, 0)
        self.assertAlmostEqual(distance, 100.0)

    def test_speed_is_capped(self) -> None:
        controller = self.make_controller()
        move_x, move_y, _, outside = controller.update(1000.0, 240.0)
        self.assertTrue(outside)
        self.assertEqual(move_x, 40)  # --max-speed
        self.assertEqual(move_y, 0)

    def test_recalibration_resets_the_neutral_point(self) -> None:
        controller = self.make_controller()
        controller.update(500.0, 240.0)
        controller.calibrate(500.0, 240.0)
        move_x, move_y, distance, outside = controller.update(500.0, 240.0)
        self.assertEqual((move_x, move_y), (0, 0))
        self.assertEqual(distance, 0.0)
        self.assertFalse(outside)

    def test_first_update_calibrates_automatically(self) -> None:
        controller = fp.JoystickController()
        move_x, move_y, distance, outside = controller.update(123.0, 456.0)
        self.assertEqual((move_x, move_y), (0, 0))
        self.assertEqual(distance, 0.0)
        self.assertFalse(outside)
        self.assertTrue(controller.is_calibrated)


class TestWinkClickDetector(unittest.TestCase):
    def make_detector(self) -> tuple[fp.WinkClickDetector, FakeMouse]:
        mouse = FakeMouse()
        detector = fp.WinkClickDetector(
            mouse=mouse,
            ear_thresh=0.20,
            ear_open_thresh=0.23,
            double_click_window=0.45,
            debounce_cooldown=0.25,
            wink_consec_frames=2,
        )
        return detector, mouse

    def wink_left(self, detector: fp.WinkClickDetector, start: float) -> None:
        detector.update(0.10, 0.30, is_paused=False, now=start)
        detector.update(0.10, 0.30, is_paused=False, now=start + 0.01)

    def wink_right(self, detector: fp.WinkClickDetector, start: float) -> None:
        detector.update(0.30, 0.10, is_paused=False, now=start)
        detector.update(0.30, 0.10, is_paused=False, now=start + 0.01)

    def open_eyes(self, detector: fp.WinkClickDetector, now: float, paused: bool = False) -> None:
        detector.update(0.30, 0.30, is_paused=paused, now=now)

    def test_natural_blink_is_ignored(self) -> None:
        detector, mouse = self.make_detector()
        detector.update(0.10, 0.10, is_paused=False, now=1.0)
        self.assertEqual(mouse.clicks, [])
        self.assertEqual(detector.get_action_display(now=1.0), "Blink (Ignored)")

    def test_left_wink_emits_one_left_click_after_double_click_window(self) -> None:
        detector, mouse = self.make_detector()
        self.wink_left(detector, start=1.0)
        self.assertEqual(mouse.clicks, [])
        self.open_eyes(detector, now=1.50)
        self.assertEqual(mouse.clicks, [("click", "left", 1)])

    def test_two_left_winks_emit_a_double_click(self) -> None:
        detector, mouse = self.make_detector()
        self.wink_left(detector, start=1.0)
        self.open_eyes(detector, now=1.02)
        self.wink_left(detector, start=1.10)
        self.assertEqual(mouse.clicks, [("click", "left", 2)])
        self.assertEqual(detector.get_action_display(now=1.11), "Double Click")

    def test_right_wink_emits_one_right_click(self) -> None:
        detector, mouse = self.make_detector()
        self.wink_right(detector, start=1.0)
        self.assertEqual(mouse.clicks, [("click", "right", 1)])

    def test_pause_suppresses_pending_left_click(self) -> None:
        detector, mouse = self.make_detector()
        self.wink_left(detector, start=1.0)
        self.open_eyes(detector, now=1.50, paused=True)
        self.assertEqual(mouse.clicks, [])

    def test_tracking_loss_cancels_pending_left_click(self) -> None:
        detector, mouse = self.make_detector()
        self.wink_left(detector, start=1.0)
        detector.reset_tracking(now=1.02)
        self.open_eyes(detector, now=1.50)
        self.assertEqual(mouse.clicks, [])
        self.assertEqual(detector.get_action_display(now=1.02), "Click Cancelled")

    def test_cooldown_suppresses_a_second_click(self) -> None:
        detector, mouse = self.make_detector()
        self.wink_right(detector, start=1.0)
        self.open_eyes(detector, now=1.05)
        self.wink_right(detector, start=1.10)
        self.assertEqual(mouse.clicks, [("click", "right", 1)])


class TestArgumentParsing(unittest.TestCase):
    def test_defaults_and_overrides(self) -> None:
        with mock.patch.object(sys, "argv", ["facepilot.py"]):
            defaults = fp.parse_args()
        self.assertEqual(defaults.camera, 0)
        self.assertEqual(defaults.deadzone, 25.0)
        self.assertEqual(defaults.backend, "auto")

        with mock.patch.object(
            sys,
            "argv",
            ["facepilot.py", "--camera", "2", "--deadzone", "30", "--backend", "pynput"],
        ):
            overridden = fp.parse_args()
        self.assertEqual(overridden.camera, 2)
        self.assertEqual(overridden.deadzone, 30.0)
        self.assertEqual(overridden.backend, "pynput")


if __name__ == "__main__":
    unittest.main()
