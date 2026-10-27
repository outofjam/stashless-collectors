# Stashless collectors

Reference collectors for [Stashless](https://stashless.outofjam.com) production telemetry. A collector runs inside your own estate on a schedule, reads one IFS Cloud environment (and, optionally, the PSO gateway paired with it) with a read-only IAM client whose secret stays with you, and posts counts, timings and the installed release to Stashless with a collector token. Stashless never signs in to your production system and holds no credential for it: the token can post one report and read nothing back.

Pick the edition that matches where you already run scheduled jobs. Every edition produces the same report from the same reads, and the [conformance check](scripts/conformance.sh) in this repository proves it on every change.

| Edition | Runs where | Start here |
| --- | --- | --- |
| [PowerShell](powershell/) | Azure Automation runbook, Windows Task Scheduler, any PowerShell 7 host | [powershell/README.md](powershell/README.md) |
| [Python](python/) | cron, systemd timers, any host with Python 3.9 or newer, no packages to install | [python/README.md](python/README.md) |
| [Azure Function](azure-function/) | A timer-triggered Function app, ready to deploy | [azure-function/README.md](azure-function/README.md) |
| [Container](docker/) | Azure Container Apps jobs, Kubernetes CronJob, `docker run` on a schedule | [docker/README.md](docker/README.md) |

## What every edition does

1. Signs in to IFS Cloud with your read-only IAM client (the same `STASHLESS_READ` permission set a connected non-production environment uses).
2. Makes the same reads the Stashless connector makes: the installed release, server alerts, background jobs in error or warning, logins, full-user licences, PSO batch queues and datasets, and two hours of application message timings. Only counts, timings and version strings; no message content, no user names. The licence identifiers are read and dropped.
3. Optionally opens a session on the PSO gateway for the PSO section, and closes it again.
4. Posts one JSON report to `https://stashless.outofjam.com/api/telemetry/v1` with the token in a bearer header. The report format is published at [docs/schema-v1.json](docs/schema-v1.json) and served by Stashless at `/telemetry/schema/v1`; the reads and the arithmetic are in [docs/aggregation.md](docs/aggregation.md).

A read that is refused costs only its own figure and is named in the report's heartbeat, so support can see what the collector could not read. Only a failed IFS sign-in or a refused post exits non-zero, and even then the heartbeat is posted.

## Before you start

In Stashless, on the production environment's edit page under **Telemetry**, choose the reporting interval and press **Create token**. The token is shown once. Keep it in your scheduler's secret store or in a Key Vault, never in a script.

In IFS Cloud, create the read-only IAM client and import the permission set [ifs/CMDB_READ-permission-set.xml](ifs/CMDB_READ-permission-set.xml) exactly as for a connected environment (the Stashless setup guide, "Connect IFS Cloud"), this time in production. Keep its secret with the token. [ifs/README.md](ifs/README.md) says what the set grants.

## Configuration

Every setting is an environment variable. The PowerShell edition also takes each one as a parameter; the Python edition as a flag.

| Variable | Value |
| --- | --- |
| `STASHLESS_ENDPOINT` | `https://stashless.outofjam.com/api/telemetry/v1` |
| `STASHLESS_TOKEN` | The collector token from the Telemetry panel |
| `IFS_URL` | The production environment's address, as on its environment page |
| `IFS_CLIENT_ID`, `IFS_CLIENT_SECRET` | The read-only IAM client and its secret |
| `IFS_REALM` | Optional: discovered from the environment's own sign-in challenge when blank |
| `IFS_SERVER_TIMEZONE` | Optional: the IFS server's IANA time zone (for example `America/Regina`) when IFS labels local time as UTC, as set on the environment page |
| `PSO_URL`, `PSO_ACCOUNT_ID`, `PSO_USER_NAME`, `PSO_PASSWORD` | Optional: the paired PSO environment, read with a PSO user; its figures land on the paired environment in Stashless |
| `KEY_VAULT_URL` | Optional: a Key Vault the managed identity may read secrets from |
| `IFS_CLIENT_SECRET_NAME`, `PSO_PASSWORD_NAME`, `STASHLESS_TOKEN_NAME` | Optional: the names of those secrets in that vault, used instead of the values |

A secret given by name is read with the managed identity of wherever the collector runs (Azure Automation, Functions, a VM or a container app with an identity), so no secret value is ever written into configuration.

## Running without network, for a look

Every edition takes `--fixtures fixtures` (`-Fixtures` in PowerShell) and reads the canned answers in [fixtures/](fixtures/) instead of IFS and PSO, then writes or prints the report it would post. [fixtures/expected-payload.json](fixtures/expected-payload.json) is what you should get. The fixtures describe a fictional environment.

## Security

- The collector is read-only. Its only non-GET requests are the IAM token exchange, the one POST to Stashless and, when PSO is configured, opening and closing the gateway session.
- No proxy and no redirect is ever followed, so the token and the session never travel to a host other than the one configured.
- Nothing read is logged. Progress output names reads and counts.
- Revoke the token in Stashless and the next report is refused at once; rotate it and the old one keeps working for 24 hours.

## Versions

The collector reports its edition and version in every report, so support can see what reported. The current version is 1.0.0 in every edition.

## Licence

MIT. See [LICENSE](LICENSE).
