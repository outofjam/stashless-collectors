# Python edition

`stashless_collector.py` is a single file for Python 3.9 or newer, standard library only: nothing to install. It runs from cron, a systemd timer, Windows Task Scheduler, or inside the [Azure Function](../azure-function/) and [container](../docker/) editions, which wrap this same file.

## cron

1. Put the script somewhere the job's user can read, for example `/opt/stashless/stashless_collector.py`.
2. Put the settings in a file only that user can read, for example `/etc/stashless/collector.env`:

   ```sh
   STASHLESS_ENDPOINT=https://stashless.outofjam.com/api/telemetry/v1
   STASHLESS_TOKEN=sl_...
   IFS_URL=https://customer.ifs.cloud
   IFS_CLIENT_ID=STASHLESS_READ
   IFS_CLIENT_SECRET=...
   # optional
   IFS_SERVER_TIMEZONE=America/Regina
   PSO_URL=https://customer-pso.ifs.cloud
   PSO_ACCOUNT_ID=...
   PSO_USER_NAME=...
   PSO_PASSWORD=...
   ```

   ```sh
   chmod 600 /etc/stashless/collector.env
   ```
3. Add the job at the interval you chose in Stashless, for example hourly:

   ```cron
   0 * * * *  set -a; . /etc/stashless/collector.env; set +a; python3 -I /opt/stashless/stashless_collector.py >> /var/log/stashless-collector.log 2>&1
   ```

   Secrets go in the environment, never on the command line, so they do not show in the process list.
4. Run it once by hand the same way and read the log: it ends with `Stashless answered 202`. In Stashless, the environment's status moves to **Reporting**.

On an Azure VM with a managed identity, set `KEY_VAULT_URL` and the `*_NAME` variables instead of the secret values.

## Flags

Every environment variable in the [top-level README](../README.md#configuration) is also a flag: `--endpoint`, `--token`, `--ifs-url`, `--ifs-client-id`, `--ifs-client-secret`, `--ifs-realm`, `--ifs-server-timezone`, `--pso-url`, `--pso-account-id`, `--pso-user-name`, `--pso-password`, `--key-vault-url`. A flag wins over the variable.

Three more are for looking before posting:

- `--print` prints the report instead of posting it.
- `--out <file>` writes the report to a file instead of posting it.
- `--fixtures <dir>` reads the canned answers in [../fixtures](../fixtures) instead of IFS and PSO.

```sh
python3 -I stashless_collector.py --fixtures ../fixtures --print
```

`--implementation docker` (or `STASHLESS_COLLECTOR_IMPLEMENTATION=docker`) makes the report say it came from the container edition; the container sets it for you.

## Exit codes

`0` when the report was posted and accepted (or written with `--out`); `1` when IFS could not be signed in to, the token could not be read, or Stashless refused the report. A refused read of one projection does not fail the run: that figure is left out and the reason is in the report's heartbeat.

## What it needs on the network

Outbound HTTPS to the IFS Cloud host (the IAM token endpoint and the OData projections), to `stashless.outofjam.com`, and, with PSO configured, to the PSO host. The script ignores `HTTP_PROXY` and `HTTPS_PROXY` on purpose and never follows a redirect. With Key Vault names, the managed identity endpoint of the host and `https://<vault>.vault.azure.net`.
