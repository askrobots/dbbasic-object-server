---
package:
  type: app
  runs: server
  install: scripts/one-click-install.sh
  cloud_init: cloud-init.yaml
  scheme: https
  port: 443
  health: /health
  setup_code: /etc/dbbasic-setup-code.sha256
  setup: /setup
  min_memory_mb: 1024
---
# DBBASIC Object Server, one click

What a one-click installer needs to put this server on a new machine in someone's own cloud account.
GitStable's "Install on DigitalOcean" reads the block above at the release being installed.

- `cloud_init` is handed to the cloud as the server's user data. It runs `scripts/one-click-install.sh`
  at that release.
- `setup_code` is where the installer writes the SHA-256 of a one-time setup code it shows only to the
  person who installed. The server stays closed, answering only `/health` and `/setup`, until that person
  opens `/setup` with the code and chooses their own email and password (`object_setup.py`).
- `scheme`, `port` and `health` are how the installer knows it worked: `https://<address>/health`
  answers with the `release` and `commit` installed, and `"setup": "waiting"` until setup is done.
- `min_memory_mb`: the smallest server offered. 512 MB hasn't been tried with the Python packages this
  installs, so 1 GB is the floor until it has.

For an install by hand, see [docs/quickstart.md](docs/quickstart.md).
