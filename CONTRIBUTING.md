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

## README and release maintenance

- Preserve the Chinese and English product introductions, core evidence model, navigation, license/runtime badges, copyable commands and accurate language-support boundaries when updating a release.
- Link previews to their explicit release tags. GitHub's `/releases/latest` does not select prereleases. Verify platform filenames and SHA-256 values against the uploaded assets.
- Keep personal biographies out of the README; retain required license and third-party notices.
- Publish changed product snapshots under a **new version tag**. Do not move a previously published tag or replace its frozen source history.
- Before pushing a release tag, freeze the public commit and tag-bound notes. Set `PUBLIC_COMMIT_SHA`, `PUBLIC_TREE_SHA256` and `RELEASE_NOTES_SHA256` to that reviewed snapshot and run `tools/release_metadata.py verify` with those values. These repository variables are shared across release runs: finish one release before changing them for another.
- The release workflow expects the title `水木验码 <tag>`, a prerelease, the exact frozen notes body and a target resolving to the frozen commit. Bind the target to the full commit, not a moving branch. README-only maintenance on `main` does not require moving the release tag or refreshing its frozen digests.
- Confirm both Public CI and Release results before claiming a successful release. An uploaded asset alone does not show that release verification passed.
