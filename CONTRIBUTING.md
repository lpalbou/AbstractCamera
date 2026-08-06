# Contributing to AbstractCamera

Thanks for taking the time to contribute. This repository aims to stay small, stable-by-design, and easy to integrate.

AbstractCamera is part of the **AbstractFramework** ecosystem:
- AbstractFramework: <https://github.com/lpalbou/AbstractFramework>
- AbstractCore: <https://github.com/lpalbou/abstractcore>
- AbstractRuntime: <https://github.com/lpalbou/abstractruntime>

## Ground rules

- Keep the public API stable (`CameraManager`, `CameraHub`, and the session protocol in [`src/abstractcamera/camera_manager.py`](src/abstractcamera/camera_manager.py) and [`src/abstractcamera/wire.py`](src/abstractcamera/wire.py)).
- Prefer additive changes (new fields, new family adapters, new tools) over breaking changes.
- Do not commit model weights, large binaries, or cache artifacts.
- Make docs and examples match the code (the repo is intended to be readme-first).
- Keep `import abstractcamera` light — the camera stack (OpenCV/numpy) loads on first attribute use, never at package import (see [`src/abstractcamera/__init__.py`](src/abstractcamera/__init__.py)).

## Development setup

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

Optional (if you work on AbstractCore integration locally):

```bash
python -m pip install abstractcore
```

AbstractCore is intentionally supplied by the host application. The capability plugin registers through the `abstractcore.capabilities_plugins` entry point when both packages are installed.

## Run tests

```bash
python -m pytest tests/ -q
```

Camera-less CI uses the built-in simulator (`ABSTRACTCAMERA_FAKE=1` is set in CI). Hardware validation scripts live under `scripts/` and are run manually against real bodies.

## Common contribution types

### 1) Improve documentation

Core entrypoints:
- [`README.md`](README.md)
- [`docs/getting-started.md`](docs/getting-started.md)
- [`docs/architecture.md`](docs/architecture.md)
- [`docs/api.md`](docs/api.md)
- [`docs/faq.md`](docs/faq.md)
- [`docs/adr/README.md`](docs/adr/README.md)

After doc changes, regenerate the AI-readable bundle:

```bash
python scripts/generate_llms_full.py
```

Doc hygiene checklist:
- Commands are copy-pastable.
- Links resolve (relative links are preferred).
- Support claims match the current code and ADRs.
- Prefer Mermaid diagrams when they improve clarity ([`docs/architecture.md`](docs/architecture.md) is the canonical place).

### 2) Add or extend a camera family

Family adapters live in [`src/abstractcamera/adapters/`](src/abstractcamera/adapters/). Transport sessions and drivers live in [`src/abstractcamera/drivers/`](src/abstractcamera/drivers/).

Checklist:
- Implement the session protocol ([`src/abstractcamera/wire.py`](src/abstractcamera/wire.py), [`src/abstractcamera/session.py`](src/abstractcamera/session.py)).
- Add a family adapter with honest capability descriptors (ADR 0004).
- Extend `tests/test_session_protocol.py` and add family-specific tests.
- Document the family in [`docs/api.md`](docs/api.md) and add an ADR when the design choice is load-bearing.
- Validate against real hardware before claiming support (ADR 0007).

### 3) Extend AbstractCore integration

Integration surfaces:
- [`src/abstractcamera/service.py`](src/abstractcamera/service.py) — synchronous operation layer
- [`src/abstractcamera/integrations/abstractcore_plugin.py`](src/abstractcamera/integrations/abstractcore_plugin.py) — capability plugin
- [`src/abstractcamera/integrations/abstractcore_tools.py`](src/abstractcamera/integrations/abstractcore_tools.py) — explicit AI tool set

Checklist:
- Delegate through `CameraService`; do not duplicate capture/wait logic in the plugin or tools.
- Update `CAMERA_TOOL_CLASSIFICATION` when adding tools.
- Add tests under `tests/test_abstractcore_plugin.py` and `tests/test_abstractcore_tools.py`.

## Submitting a change

Please include:
- A short explanation of the change and why it is needed.
- Test results (`python -m pytest tests/ -q`).
- Doc updates and `llms-full.txt` regeneration when public behavior or setup changes.

## Questions / discussions

If you are unsure about scope or design, open an issue with a minimal proposal and a concrete example (inputs/outputs).
