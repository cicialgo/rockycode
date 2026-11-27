# RockyCode desktop

- Use python3, not python. Read README.md (the dependency policy section) before dependency changes.
- Keep the agent loop in the upstream Python core. Do not implement a second loop or silently claim full CLI capability parity.
- Preserve per-thread drafts, user-owned notes, real cancellation and interrupted-run recovery.
- npm lifecycle scripts are disabled. Do not bypass this globally. Review source, exact versions, lockfile diff and installation behavior before adding/updating packages.
- Use the existing Node/Python standard libraries for small utilities. Never install a scanner merely to check other packages without reviewing the scanner first.
- Electron download is explicit via npm run setup:electron. Do not restore implicit download on dev/start.
- Keep machine audit output in ignored artifacts/security; never commit local paths, credentials or personal audit records.
- If installing packages, downloading models, or opening webpages fails more than three times, stop that step and ask the user to take over.
- CI is a manual draft until official Action commit SHAs are independently verified. Do not silently enable unattended dependency upgrades or release publishing.
