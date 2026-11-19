# Sandbox

Runs the whole UPS server on a laptop against a pretend world. You can click through every screen, including power cuts, shutdown countdowns and wake-ups, without touching a real machine.

```bash
python3 -m venv .venv && .venv/bin/pip install flask gunicorn pytest
.venv/bin/python dev/run_local.py --reset
```

| What | Where |
|---|---|
| Web GUI | http://127.0.0.1:8080/ |
| Control panel (scenarios, pretend machines, recorded emails) | http://127.0.0.1:8080/dev |
| UPS Hub API (token `sandbox-test-token`) | http://127.0.0.1:5050/ |

## Why it is harmless

- `ping`, Wake-on-LAN and `upsc` are replaced by the scripts in `dev/bin/`. They only read and write files in `dev/.sandbox/`.
- All addresses are in `192.0.2.0/24` (TEST-NET-1, never routed). All MAC addresses are dummies.
- Emails are recorded in the control panel and never sent.
- Every server listens on `127.0.0.1` only.
- No production file is modified. The sandbox points the modules' file paths at `dev/.sandbox/` after importing them.
- `dev/` and `tests/` are excluded from the Docker image (see `.dockerignore`).

Settings you change in the sandbox UI persist in `dev/.sandbox/etc/nut/power_manager.conf` until you start with `--reset`.
