# Releasing `codex-orch`

`codex-orch` is published to PyPI as a Python CLI package and installed by
operators with `pipx install codex-orch`.

## Prerequisites

- PyPI project `codex-orch` exists.
- PyPI Trusted Publisher is configured for this GitHub repository.
- The Trusted Publisher workflow path is
  `.github/workflows/publish-pypi.yml`.
- You have already decided the new version number.

## Release checklist

1. Update the version in `pyproject.toml`.
2. Run local verification:

   ```bash
   pytest -q
   uv build
   ```

3. Commit the release change.
4. Create and push a version tag:

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

5. Wait for the `publish-pypi` workflow to finish.

You can also run the workflow manually with `workflow_dispatch` when you want to
publish the checked-out version without creating the tag first.

## What the workflow does

The publish workflow:

- builds wheel and sdist with `uv build`
- installs the built wheel with `pipx`
- runs `codex-orch --help`
- runs `codex-orch --version`
- publishes the built distributions to PyPI

## Post-release verification

Validate the published package from outside this repository:

```bash
pipx install codex-orch
codex-orch --version
codex-orch --help
```

Then enter a codex-orch program directory and verify basic operator flows:

```bash
codex-orch task list .
codex-orch run list .
```
