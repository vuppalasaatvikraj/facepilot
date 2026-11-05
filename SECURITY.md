# Security Policy

## Scope

FacePilot is a local desktop prototype that reads a webcam and injects pointer and click events. Security-relevant concerns include:

- Unintended or stuck pointer/click events.
- Camera data being written, transmitted, or logged unexpectedly.
- A compromised or substituted MediaPipe model file.
- Command or path injection through configuration or model paths.
- Secrets accidentally committed to the repository.
- Dependency or build-supply-chain issues.

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** for this repository:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability**.
3. Include the affected version or commit, a minimal reproduction, and the impact.

Do not open a public issue for an unpatched vulnerability. Do not include camera images, personal data, credentials, or other sensitive material in a report. If a report accidentally contains a secret, rotate that secret immediately.

If private reporting is unavailable, open a minimal public issue asking for a private contact channel; do not disclose the details.

## Security model and known limitations

- **Local only:** there is no account, telemetry, analytics, or cloud inference. Camera frames stay in process memory.
- **One network request:** the app downloads `face_landmarker.task` from `storage.googleapis.com` when the file is missing. The download uses HTTPS, but the application does not verify a checksum. Verify the file yourself if you need a stronger supply-chain guarantee.
- **Global input injection:** the app can move the pointer and click anywhere the user can. Run it only on a desktop you control. The `p` shortcut pauses control, but it requires the OpenCV window to be focused.
- **Fallback failsafe:** the `pyautogui` fallback disables PyAutoGUI's corner failsafe for low latency. Do not rely on that failsafe.
- **No central safety gate:** each controller calls the mouse backend directly. A future version is planned to route every action through one fail-closed gate.
- **Secrets:** the project requires no API keys, tokens, passwords, or `.env` file. Do not add any. `.env` and common credential files are ignored by `.gitignore`.
- **Camera data:** no frames are saved by the application. If you add debug logging or recording, keep it opt-in and local, and document it.

## Safe usage

- Start with the pointer over a harmless area and keep the pause shortcut in mind.
- Use a dedicated test account or desktop when evaluating early builds.
- Review changes to `MouseManager`, `HyprlandMouse`, and anything that writes files or opens sockets.
- Keep dependencies current and review `requirements.txt` changes.

## Supported versions

This is a prototype with no release branch or long-term support commitment. Security fixes are applied to the default branch.
