# Releasing MiraBox Stream Dock SDK

Releases are built and published by `.github/workflows/release.yml` when a tag
matching `v*.*.*` is pushed. The workflow verifies the version, runs all checks,
builds the wheel and source archive, publishes them to PyPI with Trusted
Publishing, and creates a GitHub Release with the distributions attached.

## One-time PyPI setup

Create the `mirabox-stream-dock-sdk` project, or a pending Trusted Publisher, on
PyPI with these values:

- PyPI project name: `mirabox-stream-dock-sdk`
- GitHub owner: `Nekit678`
- GitHub repository: `MiraboxStreamDockSDK`
- Workflow filename: `release.yml`
- Environment name: `pypi`

Create a GitHub environment named `pypi` as well. Requiring approval for that
environment is recommended so a pushed tag cannot publish without review. No
long-lived PyPI token is required.

## Preparing a release

1. Choose a [PEP 440](https://packaging.python.org/en/latest/specifications/version-specifiers/)
   version compatible with a `vMAJOR.MINOR.PATCH` Git tag.
2. Update `project.version` in `pyproject.toml` and `__version__` in
   `src/mirabox_sdk/__init__.py`.
3. Move the relevant entries from `[Unreleased]` into a dated version section
   in `CHANGELOG.md` and add comparison links.
4. Run the complete verification suite:

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

5. Commit and merge the release changes into `main`.

## Publishing

Create the tag from the verified `main` commit and push it:

```bash
git tag -a v0.3.0 -m "Release v0.3.0"
git push origin v0.3.0
```

`scripts/verify_version.py` rejects a tag that does not exactly match both
version declarations. Do not move or reuse a tag after publishing; prepare a
new patch release instead.

## Verification after publishing

1. Confirm that every job in the Release workflow completed successfully.
2. Confirm that the GitHub Release contains one wheel and one source archive.
3. Confirm that PyPI shows the expected version and renders the README without
   errors.
4. Test installation in a clean environment:

   ```bash
   python -m venv release-check
   release-check/bin/python -m pip install mirabox-stream-dock-sdk==0.3.0
   release-check/bin/mirabox-sdk copy-property-inspector release-check/pi
   ```

   On Windows, use `release-check\Scripts\python.exe` and
   `release-check\Scripts\mirabox-sdk.exe`.
