# First public release checklist

## Before initializing Git

- Remove the temporary address-ID checker before running `git init` and creating the GitHub
  repository:
  - Delete `tools/check_address_ids.py`.
  - Remove the `Check whether address IDs changed` section from `README.md`.
  - Delete `tools/helpers/data/address-ids-baseline.json` and any leftover
    `address-ids-baseline.json.tmp` created by the checker, if present.
- Keep the regular `tools/helpers/data/address-data.json` only if it is still needed for local
  setup-helper use. Do not add it, dated address snapshots, calendars, caches, or logs to the
  public repository; `.gitignore` and `.dockerignore` already exclude the data directory.
- Review `.gitignore` and `.dockerignore` before initializing Git. Keep `.env`, local settings,
  runtime data, virtual environments, generated XML, and local tooling artifacts out of the
  first commit. Keep `.env.example` only after confirming every value is a safe placeholder.
- Confirm the repository name, GitHub owner, visibility, project description, default branch,
  license, copyright holder, and author/commit identity before creating the remote repository.

## Before committing

- Confirm the intended GitHub account, author name, and commit email. Keep both MIT
  licenses, upstream credits, and the existing author notice.
- Initialize Git separately when ready. Inspect `git diff --cached --stat` and
  `git diff --cached`; confirm no runtime data, `.env`, logs, generated XML containing keys,
  local environments, or `.codegraph/` files are staged.
- Run Gitleaks on the intended source directory and all available Git history with
  redaction enabled. If a credential was committed, rotate it and clean history before
  publication. Do not assume deleting a file removes it from history.
- Publish only code, documentation, and synthetic fixtures. Do not redistribute downloaded
  address snapshots or calendars; this project makes no claim of dataset redistribution rights.

## Release gates

- All Windows/Linux Python 3.11–3.13 CI jobs, installer checks, setup checks, dependency
  audit, and source/history secret scans pass for the intended release commit.
- Both amd64 and arm64 image candidates pass the offline container checks. Docker must
  be running for local checks; an unavailable runtime is an outstanding gate, not a pass.
- On a real Debian/Raspberry Pi system, verify installation, update, rollback and removal.
- Import generated XML into Loxone Config. Verify server-selected intervals, waste names,
  missing waste types, stale responses, and unavailable data on a Miniserver. Record the
  tested versions and hardware in the release notes; do not claim unperformed verification.
- Explain that pickup URLs now use the server's default interval; manual overrides remain optional.
- Enable GitHub private vulnerability reporting and confirm repository visibility settings.

## Publish when authorized

- Create an existing `vMAJOR.MINOR.PATCH` tag, optionally with a SemVer prerelease suffix.
  Push the tag or dispatch publishing with that tag. Publishing validates and tests its exact commit.
- Stable releases update `latest`; prereleases publish version tags only.
- Make the GHCR package public, replace `OWNER` in deployment settings, and confirm an
  anonymous pull of the versioned image and both architectures. Confirm `latest` only
  for a stable release. Never put registry credentials into examples.
- Publish release notes with migration instructions and actual verification results.

The implementation does not initialize Git, change author identity, publish source or
images, contact Umweltprofis for validation, or verify real hardware automatically.
