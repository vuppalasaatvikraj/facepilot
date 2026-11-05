#!/usr/bin/env python3
"""
FacePilot: Real-time hands-free mouse controller using webcam face mesh tracking.

Features:
- Relative joystick velocity cursor control with circular deadzone and EMA smoothing.
- Independent Left and Right Eye Aspect Ratio (EAR) wink detection.
- Blink rejection for natural bilateral blinks.
- Left click (single wink), double click (two winks within 450ms), and right click (right wink).
- Rich visual HUD overlay with mirror view, deadzone indicator, EAR gauges, and action banners.
- Keyboard shortcuts:
    [c] Recalibrate neutral head center position
    [p] Pause/unpause mouse control
    [q] or [ESC] Quit cleanly
    [+] / [-] Increase / decrease speed factor
    [[] / []] Decrease / increase deadzone radius
"""

import os
import time
import math
import json
import socket
import argparse
import urllib.request
import cv2
import numpy as np

# Ensure low latency for GUI automation
try:
    from pynput.mouse import Controller as PynputController, Button as PynputButton
    PYNPUT_AVAILABLE = True
except BaseException:
    PYNPUT_AVAILABLE = False

try:
    import pyautogui
    pyautogui.PAUSE = 0.0
    pyautogui.FAILSAFE = False
    PYAUTOGUI_AVAILABLE = True
except BaseException:
    PYAUTOGUI_AVAILABLE = False


# Canonical Eye Landmark Indices in MediaPipe Face Mesh
# Right eye anatomical (appears on viewer-left in unmirrored, viewer-right in mirrored)
RIGHT_EYE_LANDMARKS = {
    'corners': (33, 133),      # outer, inner
    'vertical_1': (160, 144),  # top-left, bot-left
    'vertical_2': (159, 145),  # top-mid, bot-mid
    'vertical_3': (158, 153),  # top-right, bot-right
    'all': [33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246]
}

# Left eye anatomical (appears on viewer-right in unmirrored, viewer-left in mirrored)
LEFT_EYE_LANDMARKS = {
    'corners': (362, 263),     # inner, outer
    'vertical_1': (385, 380),  # top-left, bot-left
    'vertical_2': (386, 374),  # top-mid, bot-mid
    'vertical_3': (387, 373),  # top-right, bot-right
    'all': [362, 382, 381, 380, 374, 373, 390, 249, 263, 466, 388, 387, 386, 385, 384, 398]
}

NOSE_TIP_LANDMARK = 1
MODEL_URL = "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task"
MODEL_FILENAME = "face_landmarker.task"


class HyprlandMouse:
    """Native Hyprland Wayland IPC cursor controller using direct UNIX domain socket."""

    def __init__(self):
        sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        if not sig:
            raise RuntimeError("HYPRLAND_INSTANCE_SIGNATURE environment variable not set.")
        self.sock_path = f"/run/user/{os.getuid()}/hypr/{sig}/.socket.sock"
        if not os.path.exists(self.sock_path):
            raise RuntimeError(f"Hyprland socket does not exist at {self.sock_path}")

        self.min_x = 0
        self.min_y = 0
        self.max_x = 1920
        self.max_y = 1080
        self._init_bounds()
        self.cur_x, self.cur_y = 960.0, 540.0
        self.get_position()
        print(f"[HyprlandMouse] Connected to Hyprland socket at ({self.cur_x:.0f}, {self.cur_y:.0f}), desktop bounds: {self.max_x}x{self.max_y}")

    def _init_bounds(self):
        try:
            raw = self._send(b"j/monitors")
            mons = json.loads(raw.decode("utf-8", errors="ignore"))
            max_x = 0
            max_y = 0
            for m in mons:
                scale = float(m.get("scale", 1.0))
                mx = int(m.get("x", 0))
                my = int(m.get("y", 0))
                mw = int(m.get("width", 1920) / scale)
                mh = int(m.get("height", 1080) / scale)
                max_x = max(max_x, mx + mw)
                max_y = max(max_y, my + mh)
            if max_x > 0 and max_y > 0:
                self.max_x = max_x
                self.max_y = max_y
        except Exception:
            pass

    def _send(self, payload: bytes) -> bytes:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(self.sock_path)
            s.sendall(payload)
            data = b""
            while True:
                chunk = s.recv(1024)
                if not chunk:
                    break
                data += chunk
            return data

    def get_position(self):
        try:
            resp = self._send(b"cursorpos").decode("utf-8", errors="ignore").strip()
            parts = resp.split(",")
            if len(parts) == 2:
                self.cur_x = float(parts[0].strip())
                self.cur_y = float(parts[1].strip())
        except Exception:
            pass
        return self.cur_x, self.cur_y

    def move(self, dx: int, dy: int):
        if dx == 0 and dy == 0:
            return
        target_x = max(self.min_x, min(self.max_x - 1, self.cur_x + dx))
        target_y = max(self.min_y, min(self.max_y - 1, self.cur_y + dy))
        self.cur_x = target_x
        self.cur_y = target_y
        cmd = f"dispatch hl.dsp.cursor.move({{ x = {int(target_x)}, y = {int(target_y)} }})"
        self._send(cmd.encode("utf-8"))

    def click(self, button: str = "left", count: int = 1):
        key = "mouse:272" if button == "left" else "mouse:273"
        cmd = f'dispatch hl.dsp.send_shortcut({{ mods = "", key = "{key}" }})'.encode("utf-8")
        for i in range(count):
            self._send(cmd)
            if count > 1 and i < count - 1:
                time.sleep(0.04)


class MouseManager:
    """Multi-backend low-latency mouse manager supporting Hyprland Wayland, pynput, and pyautogui."""

    def __init__(self, backend="auto"):
        self.backend = backend
        self._hypr_mouse = None
        self._pynput_mouse = None

        # Always try to have pynput as auxiliary for XWayland
        if PYNPUT_AVAILABLE:
            try:
                self._pynput_mouse = PynputController()
            except Exception:
                self._pynput_mouse = None

        if backend in ("auto", "hyprland") and os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            try:
                self._hypr_mouse = HyprlandMouse()
                self.backend = "hyprland"
            except Exception as e:
                print(f"[MouseManager] Hyprland backend init failed ({e}), falling back.")
                self._hypr_mouse = None

        if self._hypr_mouse is None:
            if backend in ("auto", "pynput") and self._pynput_mouse is not None:
                self.backend = "pynput"
            elif PYAUTOGUI_AVAILABLE:
                self.backend = "pyautogui"
            else:
                self.backend = "dummy"

        print(f"[MouseManager] Active mouse backend: {self.backend.upper()}")

    def sync_position(self):
        """Syncs internal cursor position with physical mouse when stationary."""
        if self._hypr_mouse:
            self._hypr_mouse.get_position()

    def move(self, dx: int, dy: int):
        if dx == 0 and dy == 0:
            return
        if self._hypr_mouse:
            self._hypr_mouse.move(dx, dy)
        elif self.backend == "pynput" and self._pynput_mouse:
            try:
                self._pynput_mouse.move(dx, dy)
            except Exception:
                pass
        elif self.backend == "pyautogui" and PYAUTOGUI_AVAILABLE:
            try:
                pyautogui.moveRel(dx, dy, _pause=False)
            except Exception:
                pass

    def click(self, button: str = "left", count: int = 1):
        if self._hypr_mouse:
            self._hypr_mouse.click(button, count)
        # Also dispatch to pynput so XWayland surfaces receive the click
        if self._pynput_mouse:
            try:
                btn = PynputButton.left if button == "left" else PynputButton.right
                self._pynput_mouse.click(btn, count)
            except Exception:
                pass
        elif self.backend == "pyautogui" and PYAUTOGUI_AVAILABLE:
            try:
                if button == "left":
                    if count == 2:
                        pyautogui.doubleClick(_pause=False)
                    else:
                        pyautogui.click(button="left", _pause=False)
                elif button == "right":
                    pyautogui.click(button="right", _pause=False)
            except Exception:
                pass


class FaceTracker:
    """Detects facial landmarks using MediaPipe Tasks or MediaPipe Solutions."""

    def __init__(self, model_path: str = MODEL_FILENAME):
        self.model_path = model_path
        self._detector = None
        self._mode = None
        self._setup_detector()

    def _ensure_model(self):
        if not os.path.exists(self.model_path):
            print(f"[FaceTracker] Model '{self.model_path}' not found. Downloading from Google CDN...")
            urllib.request.urlretrieve(MODEL_URL, self.model_path)
            print(f"[FaceTracker] Model downloaded ({os.path.getsize(self.model_path)} bytes).")

    def _setup_detector(self):
        # 1. Try MediaPipe Tasks API (modern mediapipe >= 0.10)
        try:
            import mediapipe as mp
            from mediapipe.tasks import python
            from mediapipe.tasks.python import vision

            self._ensure_model()
            base_options = python.BaseOptions(model_asset_path=self.model_path)
            options = vision.FaceLandmarkerOptions(
                base_options=base_options,
                output_face_blendshapes=False,
                output_facial_transformation_matrixes=False,
                num_faces=1,
            )
            self._detector = vision.FaceLandmarker.create_from_options(options)
            self._mode = "tasks"
            print("[FaceTracker] MediaPipe FaceLandmarker Tasks API initialized.")
            return
        except Exception as e:
            print(f"[FaceTracker] MediaPipe Tasks initialization failed: {e}")

        # 2. Try MediaPipe legacy solutions API (mediapipe < 0.10)
        try:
            import mediapipe as mp
            if hasattr(mp, "solutions") and hasattr(mp.solutions, "face_mesh"):
                self._detector = mp.solutions.face_mesh.FaceMesh(
                    max_num_faces=1,
                    refine_landmarks=True,
                    min_detection_confidence=0.5,
                    min_tracking_confidence=0.5
                )
                self._mode = "solutions"
                print("[FaceTracker] MediaPipe FaceMesh Solutions API initialized.")
                return
        except Exception as e:
            print(f"[FaceTracker] MediaPipe Solutions initialization failed: {e}")

        raise RuntimeError("Could not initialize MediaPipe Face Mesh detector with either Tasks or Solutions API.")

    def process(self, bgr_frame):
        """Processes a BGR frame and returns landmark list (normalized x,y,z) or None."""
        rgb_frame = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)

        if self._mode == "tasks":
            import mediapipe as mp
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)
            result = self._detector.detect(mp_image)
            if result and result.face_landmarks and len(result.face_landmarks) > 0:
                return result.face_landmarks[0]
            return None
        elif self._mode == "solutions":
            results = self._detector.process(rgb_frame)
            if results and results.multi_face_landmarks:
                return results.multi_face_landmarks[0].landmark
            return None

    def close(self):
        if self._mode == "tasks" and hasattr(self._detector, "close"):
            self._detector.close()
        elif self._mode == "solutions" and hasattr(self._detector, "close"):
            self._detector.close()


def calculate_ear(landmarks, eye_def, img_w: int, img_h: int) -> float:
    """Calculates Eye Aspect Ratio (EAR) using 3 vertical pairs and 1 horizontal pair."""
    def pt(idx):
        lm = landmarks[idx]
        return np.array([lm.x * img_w, lm.y * img_h], dtype=np.float32)

    c1 = pt(eye_def['corners'][0])
    c2 = pt(eye_def['corners'][1])
    h_dist = float(np.linalg.norm(c1 - c2))
    if h_dist < 1e-4:
        return 0.0

    v1_top, v1_bot = pt(eye_def['vertical_1'][0]), pt(eye_def['vertical_1'][1])
    v2_top, v2_bot = pt(eye_def['vertical_2'][0]), pt(eye_def['vertical_2'][1])
    v3_top, v3_bot = pt(eye_def['vertical_3'][0]), pt(eye_def['vertical_3'][1])

    v_dist1 = float(np.linalg.norm(v1_top - v1_bot))
    v_dist2 = float(np.linalg.norm(v2_top - v2_bot))
    v_dist3 = float(np.linalg.norm(v3_top - v3_bot))

    ear = (v_dist1 + v_dist2 + v_dist3) / (3.0 * h_dist)
    return ear


class WinkClickDetector:
    """Detects left wink, right wink, double wink, and natural blinks."""

    def __init__(
        self,
        mouse: MouseManager,
        ear_thresh: float = 0.20,
        ear_open_thresh: float = 0.23,
        double_click_window: float = 0.450,
        debounce_cooldown: float = 0.250,
        wink_consec_frames: int = 2
    ):
        self.mouse = mouse
        self.ear_thresh = ear_thresh
        self.ear_open_thresh = ear_open_thresh
        self.double_click_window = double_click_window
        self.debounce_cooldown = debounce_cooldown
        self.wink_consec_frames = wink_consec_frames

        self.left_counter = 0
        self.right_counter = 0
        self.left_held = False
        self.right_held = False

        self.pending_left_click = False
        self.pending_left_time = 0.0

        self.last_action_time = 0.0
        self.last_action_name = ""
        self.action_display_expiry = 0.0

    def update(self, left_ear: float, right_ear: float, is_paused: bool, now: float = None):
        """
        Updates wink state machine.
        left_ear: EAR of user's left eye (screen left in mirror view).
        right_ear: EAR of user's right eye (screen right in mirror view).
        """
        if now is None:
            now = time.time()

        left_closed = left_ear < self.ear_thresh
        right_closed = right_ear < self.ear_thresh
        left_open = left_ear >= self.ear_open_thresh
        right_open = right_ear >= self.ear_open_thresh

        # Check pending single left click expiration
        if self.pending_left_click and (now - self.pending_left_time > self.double_click_window):
            if not is_paused:
                self.mouse.click(button="left", count=1)
            self._trigger_action("Left Click", now)
            self.pending_left_click = False
            self.last_action_time = now

        # Case 1: Natural Blink (Both eyes closed simultaneously) -> Reject!
        if left_closed and right_closed:
            self.left_counter = 0
            self.right_counter = 0
            # Natural blink does not trigger clicks
            self._trigger_action("Blink (Ignored)", now, duration=0.4)
            return

        # Case 2: Left Eye Wink (Left EAR drops while Right EAR remains open)
        if left_closed and right_open:
            self.right_counter = 0
            if not self.left_held:
                self.left_counter += 1
                if self.left_counter >= self.wink_consec_frames and (now - self.last_action_time >= self.debounce_cooldown):
                    self.left_held = True
                    # Check if this is the second wink within double_click_window
                    if self.pending_left_click and (now - self.pending_left_time <= self.double_click_window):
                        # Double click triggered instead of separate single clicks
                        if not is_paused:
                            self.mouse.click(button="left", count=2)
                        self._trigger_action("Double Click", now)
                        self.pending_left_click = False
                        self.last_action_time = now
                    else:
                        # First wink: wait to see if a second wink arrives within 450ms
                        self.pending_left_click = True
                        self.pending_left_time = now
                        self._trigger_action("Wink (Left)...", now, duration=0.25)
        elif left_open:
            self.left_held = False
            self.left_counter = 0

        # Case 3: Right Eye Wink (Right EAR drops while Left EAR remains open)
        if right_closed and left_open:
            self.left_counter = 0
            if not self.right_held:
                self.right_counter += 1
                if self.right_counter >= self.wink_consec_frames and (now - self.last_action_time >= self.debounce_cooldown):
                    self.right_held = True
                    # Resolve any pending left click first
                    if self.pending_left_click:
                        if not is_paused:
                            self.mouse.click(button="left", count=1)
                        self.pending_left_click = False
                    # Trigger right click
                    if not is_paused:
                        self.mouse.click(button="right", count=1)
                    self._trigger_action("Right Click", now)
                    self.last_action_time = now
        elif right_open:
            self.right_held = False
            self.right_counter = 0

    def cancel_pending(self, now: float | None = None):
        """Cancel an armed left click, e.g. when face tracking is lost."""
        if self.pending_left_click:
            self.pending_left_click = False
            self.pending_left_time = 0.0
            if now is not None:
                self._trigger_action("Click Cancelled", now, duration=0.5)

    def reset_tracking(self, now: float | None = None):
        """Clear per-eye state and any pending click after tracking loss."""
        self.left_counter = 0
        self.right_counter = 0
        self.left_held = False
        self.right_held = False
        self.cancel_pending(now)

    def _trigger_action(self, name: str, now: float, duration: float = 1.2):
        self.last_action_name = name
        self.action_display_expiry = now + duration

    def get_action_display(self, now: float = None):
        if now is None:
            now = time.time()
        if now < self.action_display_expiry:
            return self.last_action_name
        return ""


class JoystickController:
    """Relative joystick velocity cursor control with circular deadzone and EMA smoothing."""

    def __init__(
        self,
        deadzone: float = 25.0,
        speed_factor: float = 0.35,
        max_speed: float = 40.0,
        pos_alpha: float = 0.65,
        vel_alpha: float = 0.70
    ):
        self.deadzone = deadzone
        self.speed_factor = speed_factor
        self.max_speed = max_speed
        self.pos_alpha = pos_alpha
        self.vel_alpha = vel_alpha

        self.center_x = 320.0
        self.center_y = 240.0
        self.is_calibrated = False

        self.smooth_nose_x = 320.0
        self.smooth_nose_y = 240.0

        self.v_x = 0.0
        self.v_y = 0.0

        # Fractional pixel accumulators for sub-pixel accuracy at low speeds
        self.accum_x = 0.0
        self.accum_y = 0.0

    def calibrate(self, x: float, y: float):
        """Sets neutral center position to current nose posture."""
        self.center_x = float(x)
        self.center_y = float(y)
        self.smooth_nose_x = float(x)
        self.smooth_nose_y = float(y)
        self.v_x = 0.0
        self.v_y = 0.0
        self.accum_x = 0.0
        self.accum_y = 0.0
        self.is_calibrated = True

    def update(self, raw_nose_x: float, raw_nose_y: float):
        """
        Updates smoothed position, computes joystick vector, and outputs discrete pixel moves.
        Returns: (move_x, move_y, distance, is_outside_deadzone)
        """
        if not self.is_calibrated:
            self.calibrate(raw_nose_x, raw_nose_y)

        # 1. EMA position smoothing to suppress micro-tremor
        self.smooth_nose_x = self.pos_alpha * raw_nose_x + (1.0 - self.pos_alpha) * self.smooth_nose_x
        self.smooth_nose_y = self.pos_alpha * raw_nose_y + (1.0 - self.pos_alpha) * self.smooth_nose_y

        dx = self.smooth_nose_x - self.center_x
        dy = self.smooth_nose_y - self.center_y
        dist = math.hypot(dx, dy)

        # 2. Deadzone check: Cursor remains completely stationary inside deadzone
        if dist <= self.deadzone:
            self.v_x = 0.0
            self.v_y = 0.0
            self.accum_x = 0.0
            self.accum_y = 0.0
            return 0, 0, dist, False

        # 3. Beyond deadzone: Proportional velocity along direction vector
        excess = dist - self.deadzone
        u_x = dx / dist
        u_y = dy / dist

        # Scale speed proportionally to deflection beyond deadzone
        target_speed = min(self.max_speed, self.speed_factor * excess)
        target_vx = u_x * target_speed
        target_vy = u_y * target_speed

        # 4. EMA velocity smoothing
        self.v_x = self.vel_alpha * target_vx + (1.0 - self.vel_alpha) * self.v_x
        self.v_y = self.vel_alpha * target_vy + (1.0 - self.vel_alpha) * self.v_y

        # 5. Fractional pixel accumulation for silky smooth low-speed movement
        self.accum_x += self.v_x
        self.accum_y += self.v_y

        move_x = int(self.accum_x)
        move_y = int(self.accum_y)
        self.accum_x -= move_x
        self.accum_y -= move_y

        return move_x, move_y, dist, True


class HUDOverlay:
    """Renders real-time cybernetic pilot HUD on the mirrored webcam feed."""

    @staticmethod
    def draw(
        frame,
        joystick: JoystickController,
        wink_det: WinkClickDetector,
        left_ear: float,
        right_ear: float,
        is_paused: bool,
        fps: float,
        landmarks=None
    ):
        h, w = frame.shape[:2]
        now = time.time()

        # 1. Draw subtle eye landmark contours if landmarks present
        if landmarks:
            for eye_def in (RIGHT_EYE_LANDMARKS, LEFT_EYE_LANDMARKS):
                pts = []
                for idx in eye_def['all']:
                    lm = landmarks[idx]
                    pts.append((int(lm.x * w), int(lm.y * h)))
                pts = np.array(pts, dtype=np.int32)
                cv2.polylines(frame, [pts], isClosed=True, color=(120, 180, 120), thickness=1, lineType=cv2.LINE_AA)

        # 2. Joystick HUD Visuals
        cx, cy = int(joystick.center_x), int(joystick.center_y)
        nx, ny = int(joystick.smooth_nose_x), int(joystick.smooth_nose_y)
        dz = int(joystick.deadzone)

        dx = joystick.smooth_nose_x - joystick.center_x
        dy = joystick.smooth_nose_y - joystick.center_y
        dist = math.hypot(dx, dy)
        is_outside = dist > joystick.deadzone

        # Color: Green/Cyan when inside deadzone, Vibrant Yellow/Red when joystick active
        if is_paused:
            dz_color = (100, 100, 100)
            line_color = (80, 80, 80)
        elif is_outside:
            # Color shifts from yellow to bright orange based on intensity
            ratio = min(1.0, (dist - joystick.deadzone) / 60.0)
            dz_color = (0, int(220 * (1 - ratio) + 80), int(255 * ratio))
            line_color = (0, int(255 * (1 - ratio)), 255)
        else:
            dz_color = (255, 220, 0)   # Cyan/Greenish
            line_color = (180, 180, 180)

        # Deadzone circle
        cv2.circle(frame, (cx, cy), dz, dz_color, thickness=2, lineType=cv2.LINE_AA)
        cv2.circle(frame, (cx, cy), 3, dz_color, thickness=-1, lineType=cv2.LINE_AA)

        # Crosshairs inside deadzone circle
        ch_len = 6
        cv2.line(frame, (cx - ch_len, cy), (cx + ch_len, cy), dz_color, 1, cv2.LINE_AA)
        cv2.line(frame, (cx, cy - ch_len), (cx, cy + ch_len), dz_color, 1, cv2.LINE_AA)

        # Line connecting center to current nose position
        if is_outside and not is_paused:
            cv2.arrowedLine(frame, (cx, cy), (nx, ny), line_color, 2, cv2.LINE_AA, tipLength=0.2)
        else:
            cv2.line(frame, (cx, cy), (nx, ny), line_color, 1, cv2.LINE_AA)

        # Nose tip tracker dot
        nose_color = (0, 0, 255) if is_outside else (0, 255, 255)
        cv2.circle(frame, (nx, ny), 5, nose_color, thickness=-1, lineType=cv2.LINE_AA)
        cv2.circle(frame, (nx, ny), 7, (255, 255, 255), thickness=1, lineType=cv2.LINE_AA)

        # Status indicator under reticle
        if is_paused:
            cv2.putText(frame, "PAUSED", (cx - 28, cy + dz + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (100, 100, 255), 1, cv2.LINE_AA)
        elif is_outside:
            cv2.putText(frame, f"MOVING (tilt: {int(dist)}px)", (cx - 65, cy + dz + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 255, 255), 1, cv2.LINE_AA)
        else:
            cv2.putText(frame, f"NEUTRAL ({int(dist)}/{dz}px)", (cx - 55, cy + dz + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.40, (180, 220, 180), 1, cv2.LINE_AA)

        # 3. Top Header Bar
        overlay = frame.copy()
        cv2.rectangle(overlay, (0, 0), (w, 42), (20, 20, 20), -1)
        cv2.addWeighted(overlay, 0.75, frame, 0.25, 0, frame)

        # Title & Mode
        cv2.putText(frame, "FacePilot Mouse", (14, 28), cv2.FONT_HERSHEY_DUPLEX, 0.75, (255, 255, 255), 1, cv2.LINE_AA)

        if is_paused:
            mode_text = "[PAUSED - Press 'p']"
            mode_color = (0, 0, 255)
        else:
            mode_text = "[ACTIVE]"
            mode_color = (0, 255, 120)
        cv2.putText(frame, mode_text, (230, 28), cv2.FONT_HERSHEY_DUPLEX, 0.65, mode_color, 2, cv2.LINE_AA)

        # FPS & Settings
        stats_text = f"FPS: {int(fps)} | Spd: {joystick.speed_factor:.2f} | Dz: {int(joystick.deadzone)}px | Tilt: {int(dist)}px"
        cv2.putText(frame, stats_text, (w - 390, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (200, 200, 200), 1, cv2.LINE_AA)

        # 4. EAR Gauges (Left Eye & Right Eye)
        # Render left gauge (user's left, on screen-left)
        HUDOverlay._draw_ear_gauge(frame, 15, h - 110, "L-Eye (Left Click)", left_ear, wink_det.ear_thresh, wink_det.ear_open_thresh)
        # Render right gauge (user's right, on screen-right)
        HUDOverlay._draw_ear_gauge(frame, w - 215, h - 110, "R-Eye (Right Click)", right_ear, wink_det.ear_thresh, wink_det.ear_open_thresh)

        # 5. Prominent Action Banner
        action_text = wink_det.get_action_display(now)
        if action_text:
            HUDOverlay._draw_action_banner(frame, action_text, w, h)

        # 6. Bottom Controls Bar
        overlay_bottom = frame.copy()
        cv2.rectangle(overlay_bottom, (0, h - 34), (w, h), (15, 15, 15), -1)
        cv2.addWeighted(overlay_bottom, 0.75, frame, 0.25, 0, frame)

        help_text = "[c] Recalibrate | [p] Pause/Resume | [q/ESC] Quit | [+/-] Speed | [[/]] Deadzone"
        cv2.putText(frame, help_text, (15, h - 11), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (180, 180, 180), 1, cv2.LINE_AA)

    @staticmethod
    def _draw_ear_gauge(frame, x: int, y: int, label: str, ear: float, thresh: float, open_thresh: float):
        box_w, box_h = 200, 60
        # Background card
        sub = frame[y:y+box_h, x:x+box_w]
        black = np.zeros_like(sub)
        cv2.addWeighted(black, 0.7, sub, 0.3, 0, sub)
        frame[y:y+box_h, x:x+box_w] = sub
        cv2.rectangle(frame, (x, y), (x + box_w, y + box_h), (80, 80, 80), 1, cv2.LINE_AA)

        # Status & Color
        if ear < thresh:
            status = "WINK"
            color = (0, 140, 255) # Orange
        elif ear < open_thresh:
            status = "TRANS"
            color = (0, 220, 255) # Yellow
        else:
            status = "OPEN"
            color = (0, 255, 100) # Green

        # Label & Value
        cv2.putText(frame, f"{label}", (x + 8, y + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (220, 220, 220), 1, cv2.LINE_AA)
        cv2.putText(frame, f"{ear:.2f} [{status}]", (x + 8, y + 38), cv2.FONT_HERSHEY_DUPLEX, 0.52, color, 1, cv2.LINE_AA)

        # Horizontal progress bar
        bar_x, bar_y = x + 8, y + 46
        bar_w, bar_h = 184, 8
        cv2.rectangle(frame, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h), (50, 50, 50), -1)

        # Fill bar (ear scaled from 0.0 to 0.40)
        fill_ratio = min(1.0, max(0.0, ear / 0.40))
        fill_w = int(bar_w * fill_ratio)
        cv2.rectangle(frame, (bar_x, bar_y), (bar_x + fill_w, bar_y + bar_h), color, -1)

        # Threshold marker
        thresh_pos = bar_x + int(bar_w * (thresh / 0.40))
        cv2.line(frame, (thresh_pos, bar_y - 2), (thresh_pos, bar_y + bar_h + 2), (0, 0, 255), 2, cv2.LINE_AA)

    @staticmethod
    def _draw_action_banner(frame, action: str, w: int, h: int):
        banner_w, banner_h = 320, 48
        bx = (w - banner_w) // 2
        by = 54

        sub = frame[by:by+banner_h, bx:bx+banner_w]
        black = np.zeros_like(sub)
        cv2.addWeighted(black, 0.85, sub, 0.15, 0, sub)
        frame[by:by+banner_h, bx:bx+banner_w] = sub

        if "Double" in action:
            border_color = (255, 0, 255)  # Magenta
            text_color = (255, 100, 255)
        elif "Left" in action:
            border_color = (0, 255, 0)    # Green
            text_color = (100, 255, 100)
        elif "Right" in action:
            border_color = (0, 200, 255)  # Orange
            text_color = (100, 220, 255)
        elif "Blink" in action:
            border_color = (180, 180, 180) # Gray
            text_color = (200, 200, 200)
        else:
            border_color = (0, 255, 255)
            text_color = (255, 255, 255)

        cv2.rectangle(frame, (bx, by), (bx + banner_w, by + banner_h), border_color, 2, cv2.LINE_AA)

        (tw, th), _ = cv2.getTextSize(action, cv2.FONT_HERSHEY_DUPLEX, 0.75, 2)
        tx = bx + (banner_w - tw) // 2
        ty = by + (banner_h + th) // 2 - 2
        cv2.putText(frame, action, (tx, ty), cv2.FONT_HERSHEY_DUPLEX, 0.75, text_color, 2, cv2.LINE_AA)


class FacePilotApp:
    """Main application loop coordinating camera capture, tracking, joystick, and wink clicks."""

    def __init__(self, args):
        self.args = args
        self.cap = None
        self.mouse = MouseManager(backend=args.backend)
        self.tracker = FaceTracker(model_path=args.model)
        self.joystick = JoystickController(
            deadzone=args.deadzone,
            speed_factor=args.speed,
            max_speed=args.max_speed,
            pos_alpha=args.smooth
        )
        self.wink_detector = WinkClickDetector(
            mouse=self.mouse,
            ear_thresh=args.ear_thresh,
            ear_open_thresh=args.ear_open,
            double_click_window=args.double_click_window,
            debounce_cooldown=args.cooldown,
            wink_consec_frames=args.wink_frames
        )
        self.is_paused = False

    def setup_camera(self):
        print(f"[FacePilot] Opening camera index {self.args.camera}...")
        self.cap = cv2.VideoCapture(self.args.camera)
        if not self.cap.isOpened():
            raise RuntimeError(f"Could not open camera {self.args.camera}. Check webcam connection and permissions.")

        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.args.width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.args.height)
        self.cap.set(cv2.CAP_PROP_FPS, 30)

        actual_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        print(f"[FacePilot] Camera ready at {actual_w}x{actual_h}")

    def run(self):
        self.setup_camera()
        window_name = "FacePilot - Real-Time Hands-Free Mouse"
        cv2.namedWindow(window_name, cv2.WINDOW_AUTOSIZE)

        prev_time = time.time()
        fps = 30.0

        print("\n" + "=" * 60)
        print(" FacePilot Hands-Free Mouse Controller Active!")
        print(" Controls:")
        print("   [c]        Recalibrate neutral center to your current posture")
        print("   [p]        Pause / unpause mouse control")
        print("   [q] / [ESC] Quit application")
        print("   [+] / [-]  Increase / decrease cursor speed")
        print("   [[] / []]  Decrease / increase circular deadzone")
        print("=" * 60 + "\n")

        try:
            while True:
                ret, frame = self.cap.read()
                if not ret or frame is None:
                    time.sleep(0.005)
                    continue

                # Specification 4: Display live feed horizontally flipped like a mirror
                frame = cv2.flip(frame, 1)
                h, w = frame.shape[:2]
                now = time.time()

                # Calculate real-time FPS
                dt = now - prev_time
                prev_time = now
                if dt > 0:
                    fps = 0.9 * fps + 0.1 * (1.0 / dt)

                # Track face landmarks
                landmarks = self.tracker.process(frame)

                left_ear = 0.30
                right_ear = 0.30

                if landmarks:
                    # Nose Tip Tracking (Landmark 1)
                    nose_lm = landmarks[NOSE_TIP_LANDMARK]
                    raw_nose_x = nose_lm.x * w
                    raw_nose_y = nose_lm.y * h

                    # Automatically initialize center on first detection
                    if not self.joystick.is_calibrated:
                        self.joystick.calibrate(raw_nose_x, raw_nose_y)

                    # Update joystick movement
                    mx, my, dist, is_outside = self.joystick.update(raw_nose_x, raw_nose_y)

                    # Move mouse cursor if outside deadzone and not paused
                    if not self.is_paused and (mx != 0 or my != 0):
                        self.mouse.move(mx, my)
                    elif not is_outside:
                        # Inside deadzone: sync cursor position so physical mouse moves are respected
                        self.mouse.sync_position()

                    # In mirrored frame:
                    # The user's left eye appears on the viewer's LEFT side (smaller x).
                    # The user's right eye appears on the viewer's RIGHT side (larger x).
                    # Check horizontal center of both canonical groups to guarantee exact alignment:
                    eye_a_cx = (landmarks[33].x + landmarks[133].x) / 2.0
                    eye_b_cx = (landmarks[362].x + landmarks[263].x) / 2.0

                    if eye_a_cx < eye_b_cx:
                        screen_left_def = RIGHT_EYE_LANDMARKS
                        screen_right_def = LEFT_EYE_LANDMARKS
                    else:
                        screen_left_def = LEFT_EYE_LANDMARKS
                        screen_right_def = RIGHT_EYE_LANDMARKS

                    left_ear = calculate_ear(landmarks, screen_left_def, w, h)
                    right_ear = calculate_ear(landmarks, screen_right_def, w, h)

                    # Update wink and click detector
                    self.wink_detector.update(left_ear, right_ear, is_paused=self.is_paused, now=now)
                else:
                    # Never resolve a stale wink after the face has left the frame.
                    self.wink_detector.reset_tracking(now=now)

                # Render Visual HUD overlay
                HUDOverlay.draw(
                    frame=frame,
                    joystick=self.joystick,
                    wink_det=self.wink_detector,
                    left_ear=left_ear,
                    right_ear=right_ear,
                    is_paused=self.is_paused,
                    fps=fps,
                    landmarks=landmarks
                )

                cv2.imshow(window_name, frame)

                # Keyboard shortcut handling
                key = cv2.waitKey(1) & 0xFF
                if key in (ord('q'), ord('Q'), 27):  # 'q' or ESC
                    print("[FacePilot] Quit requested by user.")
                    break
                elif key in (ord('c'), ord('C')):
                    # Recalibrate neutral posture
                    if landmarks:
                        nose_lm = landmarks[NOSE_TIP_LANDMARK]
                        self.joystick.calibrate(nose_lm.x * w, nose_lm.y * h)
                    else:
                        self.joystick.calibrate(w / 2.0, h / 2.0)
                    self.wink_detector._trigger_action("Recalibrated Center", now)
                    print(f"[FacePilot] Center posture recalibrated to ({self.joystick.center_x:.1f}, {self.joystick.center_y:.1f})")
                elif key in (ord('p'), ord('P')):
                    # Toggle pause
                    self.is_paused = not self.is_paused
                    status_str = "PAUSED" if self.is_paused else "RESUMED"
                    self.wink_detector._trigger_action(status_str, now)
                    print(f"[FacePilot] Mouse control {status_str}.")
                elif key in (ord('+'), ord('=')):
                    # Increase speed factor
                    self.joystick.speed_factor = round(min(1.5, self.joystick.speed_factor + 0.05), 2)
                    self.wink_detector._trigger_action(f"Speed: {self.joystick.speed_factor}", now, duration=0.8)
                elif key in (ord('-'), ord('_')):
                    # Decrease speed factor
                    self.joystick.speed_factor = round(max(0.05, self.joystick.speed_factor - 0.05), 2)
                    self.wink_detector._trigger_action(f"Speed: {self.joystick.speed_factor}", now, duration=0.8)
                elif key == ord(']'):
                    # Increase deadzone
                    self.joystick.deadzone = round(min(80.0, self.joystick.deadzone + 2.0), 1)
                    self.wink_detector._trigger_action(f"Deadzone: {int(self.joystick.deadzone)}px", now, duration=0.8)
                elif key == ord('['):
                    # Decrease deadzone
                    self.joystick.deadzone = round(max(5.0, self.joystick.deadzone - 2.0), 1)
                    self.wink_detector._trigger_action(f"Deadzone: {int(self.joystick.deadzone)}px", now, duration=0.8)

        finally:
            self.cleanup()

    def cleanup(self):
        print("[FacePilot] Cleaning up resources...")
        if self.cap and self.cap.isOpened():
            self.cap.release()
        cv2.destroyAllWindows()
        if hasattr(self.tracker, "close"):
            self.tracker.close()
        print("[FacePilot] Shutdown complete.")


def parse_args():
    parser = argparse.ArgumentParser(
        description="FacePilot: Real-time hands-free mouse controller via webcam."
    )
    parser.add_argument("--camera", type=int, default=0, help="Webcam device index (default: 0)")
    parser.add_argument("--width", type=int, default=640, help="Camera width (default: 640)")
    parser.add_argument("--height", type=int, default=480, help="Camera height (default: 480)")
    parser.add_argument("--deadzone", type=float, default=25.0, help="Circular deadzone radius in pixels (default: 25.0)")
    parser.add_argument("--speed", type=float, default=0.35, help="Joystick velocity speed factor (default: 0.35)")
    parser.add_argument("--max-speed", type=float, default=40.0, help="Maximum cursor speed limit (default: 40.0)")
    parser.add_argument("--smooth", type=float, default=0.65, help="EMA position smoothing alpha 0.1-1.0 (default: 0.65)")
    parser.add_argument("--ear-thresh", type=float, default=0.20, help="Wink EAR threshold (default: 0.20)")
    parser.add_argument("--ear-open", type=float, default=0.23, help="Open eye EAR threshold (default: 0.23)")
    parser.add_argument("--double-click-window", type=float, default=0.450, help="Window for double click in seconds (default: 0.45)")
    parser.add_argument("--cooldown", type=float, default=0.250, help="Debounce cooldown in seconds (default: 0.25)")
    parser.add_argument("--wink-frames", type=int, default=2, help="Consecutive frames to confirm intentional wink (default: 2)")
    parser.add_argument("--backend", type=str, choices=["auto", "hyprland", "pynput", "pyautogui"], default="auto", help="Mouse backend (default: auto)")
    parser.add_argument("--model", type=str, default=MODEL_FILENAME, help=f"Path to MediaPipe Face Landmarker model (default: {MODEL_FILENAME})")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    app = FacePilotApp(args)
    app.run()
