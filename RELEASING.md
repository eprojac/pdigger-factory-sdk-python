# Releasing

A version tag on `main` starts the [release workflow](https://github.com/eprojac/pdigger-factory-sdk-python/actions/workflows/release.yml). It checks the version, runs
the Docker tests, builds the source distribution and wheel, publishes them to PyPI, and attaches them and the
changelog notes to the GitHub release.

1. Set `__version__` in `src/pdigger_factory_sdk/_version.py`. The package metadata and user agent read that value.
2. Move the unreleased changelog notes under the version being released and merge to `main`.
3. Tag the release commit and push the tag. For version 1.0.0, use `git tag v1.0.0` and `git push origin v1.0.0`.

## What the repository needs

- Configure PyPI trusted publishing for project `pdigger-factory-sdk`, owner `eprojac`, repository
  `pdigger-factory-sdk-python`, workflow `release.yml` and environment `pypi`. Before the first release, use a
  pending publisher.
- Create the `pypi` GitHub environment. Required reviewers can review each publish.
- Protect `main` with the CI checks required for release. GitHub supplies the token for the release, and PyPI uses
  trusted publishing.

## Publish on GitHub only

Before pushing the tag, set the repository Actions variable `SDK_GITHUB_ONLY_TAG` to the full tag name, including
the leading `v`. The workflow builds the wheel and source archive and saves them in the `dist` artifact.
Download that artifact and attach both files when you publish the GitHub release.
