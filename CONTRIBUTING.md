# Contributing to MiraBox Stream Dock SDK

Thanks for helping improve the SDK. Changes are most useful when they stay
focused, describe the Stream Dock behavior they address, and include a test at
the wire-message or public-API boundary.

## Before opening an issue

- Check the existing issues for the same problem or proposal.
- Confirm whether the behavior belongs to this Python SDK, the Stream Dock
  application, or a plugin's own code.
- Remove credentials, private URLs, personal data, and unrelated log output from
  reproductions.

For protocol bugs, include the `mirabox-stream-dock-sdk`, Python, operating
system, and Stream Dock versions. A minimal incoming JSON message or expected
outgoing command is especially helpful.

## Development setup

```bash
git clone https://github.com/Nekit678/MiraboxStreamDockSDK.git
cd MiraboxStreamDockSDK
python -m venv .venv
```

Activate the environment and install the editable package:

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Use `.venv\Scripts\Activate.ps1` on Windows or `source .venv/bin/activate` on
Linux and WSL.

## Making changes

- Keep public behavior compatible unless the change is intentional and
  documented.
- Export supported public objects from `mirabox_sdk` and add type annotations.
- Preserve unknown protocol events and fields where that improves forward
  compatibility without weakening validation for known messages.
- Add a regression test for bug fixes and observable behavior changes.
- Update `README.md`, `README.ru.md`, `docs/PROTOCOL.md`, and `CHANGELOG.md` when
  their documented behavior changes.
- Do not commit build output, virtual environments, debug logs, or a copied
  plugin executable.

### Protocol changes

Compare proposed protocol changes with the
[official documentation](https://sdk.key123.vip/en/) and the
[official templates](https://github.com/MiraboxSpace/StreamDock-Plugin-SDK).
If runtime behavior differs from the published reference, state the exact
Stream Dock version used for verification and capture the observed wire shape
in a test.

## Checks

Run the narrowest relevant tests while developing. Before opening a pull
request, run the complete local suite:

```bash
python -m unittest discover -s tests -v
PYTHONPATH=examples/counter_plugin/src \
  python -m unittest discover -s examples/counter_plugin/tests -v
python -m compileall -q src tests scripts examples
ruff check src tests scripts examples
ruff format --check src tests scripts examples
python -m build
python scripts/verify_distribution.py dist
python -m twine check dist/*
```

The Windows CI jobs exercise platform-specific path and packaging assumptions;
the tests themselves do not require a running Stream Dock instance.

## Pull requests

Describe the user-visible outcome, the reason for the change, and the checks
you ran. Keep unrelated refactors out of the same pull request. If a change is
not covered by automated tests, explain the manual verification and remaining
risk.
