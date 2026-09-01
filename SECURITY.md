# Security Policy

## Supported version

Security fixes are applied to the latest public release and the default branch.

## Reporting a vulnerability

Please do not publish exploit details, secrets, private paths, internal research material, or sensitive logs in a GitHub issue. Use GitHub's private vulnerability reporting feature on this repository when available. If it is unavailable, open a minimal issue asking the maintainer for a private contact channel without including sensitive details.

Include the affected public version, a concise reproduction using only public files, impact, and any suggested mitigation. We will acknowledge a complete report as soon as practical and coordinate disclosure after a fix is available.

## Local execution boundary

The review service runs repository tests, so only authorize repositories and test paths you trust. It binds to `127.0.0.1`, requires a startup token, and does not turn untrusted test execution into a security sandbox. See [docs/security-boundary.md](docs/security-boundary.md).
