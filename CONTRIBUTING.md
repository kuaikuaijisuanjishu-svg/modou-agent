# Contributing

Thank you for helping improve the sanitized public showcase of Shuimu Yanma.

## Before opening a change

- Keep the change limited to files and behavior that are already public.
- Do not attach internal plans, historical tests, private evaluation data, raw runs, model transcripts, credentials, personal paths, or private repository details.
- For security issues, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

## Local checks

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-agent.in
(cd web && npm ci)
python tools/public_release_check.py
python tests/run.py
(cd web && npm test)
(cd web && npm run build)
(cd web && npx playwright install chromium)
(cd web && npm run test:e2e)
```

## Pull requests

Describe the public problem, the observable change, and the checks you ran. A maintainer may decline changes that require disclosure of private research material or that overstate what the public evidence supports.
