# Changelog

All notable public changes are documented here. This log covers only the sanitized public showcase and intentionally excludes private research history.

## [Unreleased]

### Added

- Public Playwright flow covering approval, real fixture execution, named regression output, and repository path rejection.
- Public architecture overview documenting responsibilities, evidence lifecycle, and resource-limit boundaries.
- CI and release workflows now install Chromium and run the browser gate.
- Timed-out public commands now terminate their whole child process group.

## [0.1.0] - 2026-09-01

### Added

- First formal public showcase release under Apache-2.0.
- Shuimu Yanma branding and version identity in the local UI.
- Runnable capability registry required by the local service.
- Public/private boundary, security policy, contribution guide, and release checks.
- GitHub Actions checks for the Python smoke test, privacy boundary, frontend tests, and production build.

### Fixed

- Documented commands now match files that exist in the public repository.
- The public service can start without relying on private configuration files.
- The frontend test command now has a maintained smoke suite.
