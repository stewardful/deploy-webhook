# deploy-delivery

Private delivery tooling for stewardful deployment projects. Python 3 standard library and Bash only; no daemon, polling service, Docker Engine or build toolchain on the target.

## Boundary

- `scripts/deploy-test.sh`: runner-side strict SSH host verification, small control messages and status queries.
- `scripts/artifact-request.py`: CI-side GitHub Artifact identity and temporary HTTPS URL authorization. GitHub tokens stay on the runner.
- `scripts/remote-control.py`: root-private request staging and independent systemd worker.
- `scripts/delivery.py`: bounded HTTPS download, URL refresh, Range resume, closed ZIP inventory and SHA256 verification.
- `scripts/remote-install.py`: serialized execution of the platform installer; it never owns schema migration or rollback policy.
- `scripts/os.sh`: supported Ubuntu host policy.

The current installer adapter expects deploy-platform `.run` + `.sha256` artifacts and calls `--check` then `--upgrade`. It is intentionally explicit, rather than an arbitrary remote command runner.

## Use

Pin a full commit SHA. Set the environment documented by `scripts/deploy-test.sh` (five `TEST_*` SSH settings plus GitHub run/repository/commit, release version and artifact directory), then run:

```bash
bash scripts/deploy-test.sh
```

Do not print authorization JSON or signed download URLs. The bootstrap is sent through SSH stdin; large payloads are pulled over HTTPS. Installer tasks survive runner disconnect. A disconnected runner cannot refresh an expired URL; downloads fail within bounded deadlines before installation.

## Test and consumption

```bash
python3 -B -m unittest discover -s tests -v
bash -n scripts/*.sh
shellcheck scripts/*.sh
```

Tests use temporary files and fake transports, without cloud credentials. Full disposable Ubuntu/systemd/container integration belongs to deploy-platform, which owns the installer.

A private repository's workflow token cannot automatically read another private repository. deploy-platform therefore commits a reviewed source snapshot and a lock containing this repository's full commit SHA and file digests. Normal builds need no cross-repository token or runtime GitHub credentials. Update the snapshot explicitly using deploy-platform's vendor synchronization command; do not edit the snapshot directly.
