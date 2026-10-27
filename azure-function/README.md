# Azure Function edition

A ready-to-deploy Function app: one timer-triggered Python function that runs the [Python collector](../python/) on a schedule. `stashless_collector.py` here is byte for byte the file in `python/`; the conformance check keeps it so.

## Deploy

You need the [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local) (`func`) and the Azure CLI.

1. Create a Function app (Python 3.11 or newer, Consumption or Flex Consumption plan) with a **system-assigned managed identity**:

   ```sh
   az functionapp create --name <app> --resource-group <rg> --storage-account <storage> \
     --consumption-plan-location <region> --runtime python --runtime-version 3.11 --functions-version 4 --os-type Linux
   az functionapp identity assign --name <app> --resource-group <rg>
   ```
2. In your Key Vault, create the IAM client secret and the collector token as secrets, and give the app's identity **Key Vault Secrets User** on them.
3. Set the app settings. No secret values go here, only names:

   ```sh
   az functionapp config appsettings set --name <app> --resource-group <rg> --settings \
     STASHLESS_SCHEDULE="0 0 * * * *" \
     STASHLESS_ENDPOINT="https://stashless.outofjam.com/api/telemetry/v1" \
     IFS_URL="https://customer.ifs.cloud" \
     IFS_CLIENT_ID="STASHLESS_READ" \
     KEY_VAULT_URL="https://<vault>.vault.azure.net" \
     IFS_CLIENT_SECRET_NAME="stashless-ifs-client" \
     STASHLESS_TOKEN_NAME="stashless-token"
   ```

   `STASHLESS_SCHEDULE` is an NCRONTAB expression with seconds first: `0 0 * * * *` is every hour, `0 */15 * * * *` every fifteen minutes, `0 0 */4 * * *` every four hours. Match the interval you chose in Stashless. Add `IFS_SERVER_TIMEZONE` and the `PSO_*` settings from the [top-level README](../README.md#configuration) if you need them.
4. From this folder:

   ```sh
   func azure functionapp publish <app>
   ```
5. Run it once from the portal (**Code + Test**, **Test/Run**) or wait for the first tick, then open **Monitor**: the invocation log ends with `Stashless answered 202`. In Stashless, the environment's status moves to **Reporting**. A failed sign-in or a refused report fails the invocation, so Application Insights alerts can watch it.

## Run locally

Copy `local.settings.example.json` to `local.settings.json`, fill it in (values are fine locally, this file is never published), and:

```sh
pip install -r requirements.txt
func start
```

To look at a report without IFS, run the collector directly: `python3 -I stashless_collector.py --fixtures ../fixtures --print`.

## Files

| File | What it is |
| --- | --- |
| `function_app.py` | The timer trigger; calls `stashless_collector.main()` |
| `stashless_collector.py` | The collector, unchanged |
| `host.json`, `requirements.txt` | Standard Function app files; the only dependency is `azure-functions` |
| `local.settings.example.json` | A template for local runs |
