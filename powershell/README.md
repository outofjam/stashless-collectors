# PowerShell edition

`Stashless-Collector.ps1` is a single PowerShell 7 script with no module dependencies. It runs as an Azure Automation runbook, a Windows scheduled task or from any PowerShell 7 shell.

## Azure Automation runbook

1. In your Automation account, give it a **system-assigned managed identity** (Account settings, Identity). In your Key Vault's Access control, give that identity **Key Vault Secrets User** on the vault, or on just the two secrets below.
2. In the Key Vault, create two secrets: the IAM client secret (for example `stashless-ifs-client`) and the collector token (for example `stashless-token`).
3. In the Automation account, **Runbooks**, **Import a runbook**: upload `Stashless-Collector.ps1`, runtime version PowerShell 7.2 or later. Publish it.
4. Open the runbook, **Schedules**, **Add a schedule**: recurring, at the interval you chose in Stashless. On the same screen, under **Parameters and run settings**, fill in:

   | Parameter | Value |
   | --- | --- |
   | `Endpoint` | `https://stashless.outofjam.com/api/telemetry/v1` |
   | `IfsUrl` | `https://<customer>.ifs.cloud` |
   | `IfsClientId` | the read-only IAM client, for example `STASHLESS_READ` |
   | `KeyVaultUrl` | `https://<vault>.vault.azure.net` |
   | `IfsClientSecretName` | the name of the IAM secret in the vault |
   | `TokenName` | the name of the token secret in the vault |
   | `IfsServerTimeZone` | optional, the IFS server's IANA zone |
   | `PsoUrl`, `PsoAccountId`, `PsoUserName`, `PsoPasswordName` | optional, for the paired PSO environment |

   Automation variables are not visible to the script; pass parameters, as above. Passing the secret values themselves as parameters also works, but they then appear in the job's input, so prefer the names.
5. Run it once by hand (**Start**) and read the output: it ends with `Stashless answered 202` and the report summary. In Stashless, the environment's status moves to **Reporting**.

## Windows Task Scheduler or any PowerShell 7 host

Set the variables in the task's environment (or a wrapper script that is not world-readable), never on the command line, and run:

```powershell
pwsh -NoProfile -NonInteractive -File .\Stashless-Collector.ps1
```

With a managed identity on an Azure VM, `KEY_VAULT_URL` and the `*_NAME` variables work here too.

## Parameters

Every environment variable in the [top-level README](../README.md#configuration) is also a parameter: `-Endpoint`, `-Token`, `-IfsUrl`, `-IfsClientId`, `-IfsClientSecret`, `-IfsRealm`, `-IfsServerTimeZone`, `-PsoUrl`, `-PsoAccountId`, `-PsoUserName`, `-PsoPassword`, `-KeyVaultUrl`, `-IfsClientSecretName`, `-PsoPasswordName`, `-TokenName`. A parameter wins over the variable of the same name.

Three more are for looking before posting:

- `-Print` prints the report instead of posting it.
- `-Out <file>` writes the report to a file instead of posting it.
- `-Fixtures <dir>` reads the canned answers in [../fixtures](../fixtures) instead of IFS and PSO.

```powershell
pwsh -NoProfile -File .\Stashless-Collector.ps1 -Fixtures ..\fixtures -Print
```

## Exit codes

`0` when the report was posted and accepted (or written with `-Out`); `1` when IFS could not be signed in to, the token could not be read, or Stashless refused the report. A refused read of one projection does not fail the run: that figure is left out and the reason is in the report's heartbeat.

## What it needs on the network

Outbound HTTPS to the IFS Cloud host (the IAM token endpoint and the OData projections), to `stashless.outofjam.com`, and, with PSO configured, to the PSO host. No proxy is used and no redirect is followed. With Key Vault names, the managed identity endpoint of the host and `https://<vault>.vault.azure.net`.
