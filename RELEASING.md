# Releasing

Releases are tags `v<version>` on `main`, where `<version>` is `__version__` in `src/pdigger_factory_sdk/_version.py`.

1. Update `__version__` (the user agent and the package version follow it; the tests read it too), move the
   "Unreleased" notes of `CHANGELOG.md` under the new version, and merge.
2. Tag and push: `git tag v0.1.0 && git push origin v0.1.0`.
3. The `Release` workflow runs the Docker tests, checks that the tag is the version, builds the sdist and wheel,
   publishes them to PyPI, and makes the GitHub release with the changelog's notes and the files.

## What the repository needs

- **PyPI trusted publishing**, no token: on PyPI, add a trusted publisher for the project `pdigger-factory-sdk` with
  owner `eprojac`, repository `pdigger-factory-sdk-python`, workflow `release.yml` and environment `pypi`. For the
  first release, add it as a "pending publisher".
- **A GitHub environment `pypi`**, ideally with required reviewers, so a tag alone cannot publish.
- **Branch protection** on `main`, with the CI checks required.

No other secret is used: the GitHub release uses the workflow's own token.
