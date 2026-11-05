# Contributing to FacePilot

Thanks for taking a look. FacePilot is a small prototype, so focused changes are more useful than broad rewrites.

## Development setup

```bash
git clone https://github.com/vuppalasaatvikraj/facepilot.git
cd facepilot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Run the hardware-independent tests:

```bash
python -m unittest discover -s tests -v
```

The tests import `facepilot.py`, so they need NumPy and OpenCV installed. They do not need a camera, a display server, MediaPipe model weights, or input-injection permissions.

## What to work on

Good first contributions:

- Add tests for uncovered pure logic, especially edge cases in the EAR math and wink state machine.
- Improve error messages and backend diagnostics.
- Document a verified hardware/desktop setup.
- Add replayable synthetic signal fixtures.
- Reduce the coupling in `facepilot.py` without changing behavior.

Please open an issue before a large refactor or a new dependency. The project deliberately keeps the runtime small.

## Code expectations

- Preserve the existing user-visible behavior unless the change is explicitly about that behavior.
- Keep the camera loop responsive; do not add blocking work to the frame path.
- Do not add telemetry, analytics, cloud calls, or camera recording.
- Do not commit credentials, `.env` files, personal data, camera frames, or model binaries.
- Add or update tests when changing `calculate_ear`, `JoystickController`, `WinkClickDetector`, or argument parsing.
- Keep documentation accurate. If a feature is planned rather than implemented, say so.
- Run `python -m py_compile facepilot.py` and the unit tests before opening a pull request.

There is no enforced formatter or linter configuration yet. Match the surrounding style: standard library imports first, then third-party imports, four-space indentation, and concise docstrings.

## Hardware testing

If your change touches a backend or the HUD, include the following in the pull request:

- Desktop/compositor and version.
- Python and dependency versions.
- Which backend was selected (the startup log prints it).
- What you tested manually and what you could not test.

Never claim a hardware path works because the code compiles. If you cannot test it, say so.

## Pull requests

- Keep commits focused and use clear messages.
- Explain the problem, the change, and the verification performed.
- Link related issues.
- Do not include generated build artifacts, virtual environments, or model files.

## License

By contributing, you agree that your contribution is licensed under the repository's MIT License.
