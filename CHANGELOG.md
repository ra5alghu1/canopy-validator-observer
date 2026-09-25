# Changelog

Notable changes to Canopy Validator Observer are recorded here.

## Unreleased

### Added

- Configurable timestamped report retention (30 days by default, `0` to disable),
  with cleanup only after a successful report save.

- Added Docker-native health reporting based on the status and freshness of `reports/latest.json`.
  Missing, malformed, stale, and `CRITICAL` reports now make the observer container `unhealthy`.
- Added persistent block-height progress tracking. A responsive node is now marked `CRITICAL` when
  its height has not advanced for the configured interval.
- Added an always-on Docker Compose service with automatic restart and configurable polling.
- Added GitHub Actions tests on Python 3.11 and 3.12.
