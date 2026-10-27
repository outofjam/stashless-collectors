# Container edition

The [Python collector](../python/) on `python:3.12-alpine`, with no credentials in the image: every value arrives at run time from your scheduler's secret store or from a Key Vault through a managed identity. It reports as implementation `docker`.

## Build

From the root of this repository:

```sh
docker build -f docker/Dockerfile -t stashless-collector .
```

Push it to your own registry; there is no public image, so what you run is what you built.

## Run

```sh
docker run --rm \
  -e STASHLESS_ENDPOINT=https://stashless.outofjam.com/api/telemetry/v1 \
  -e STASHLESS_TOKEN=sl_... \
  -e IFS_URL=https://customer.ifs.cloud \
  -e IFS_CLIENT_ID=STASHLESS_READ \
  -e IFS_CLIENT_SECRET=... \
  stashless-collector
```

Add the optional variables from the [top-level README](../README.md#configuration) the same way. To look before posting:

```sh
docker run --rm -v "$PWD/fixtures:/fixtures:ro" stashless-collector --fixtures /fixtures --print
```

## Azure Container Apps job

1. Create a **Container Apps job**, trigger type **Schedule**, with a cron expression at the interval you chose in Stashless (for example `0 * * * *`).
2. Image: the one you pushed. Give the job a **system-assigned managed identity** with **Key Vault Secrets User** on your vault.
3. Environment variables: `STASHLESS_ENDPOINT`, `IFS_URL`, `IFS_CLIENT_ID`, `KEY_VAULT_URL`, `IFS_CLIENT_SECRET_NAME`, `STASHLESS_TOKEN_NAME` (and the PSO ones if needed). No secret values anywhere in the job definition.
4. Run it once (**Run now**) and read the log: it ends with `Stashless answered 202`.

## Kubernetes CronJob

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: stashless-collector
spec:
  schedule: "0 * * * *"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: collector
              image: your-registry/stashless-collector:1.0.0
              envFrom:
                - secretRef:
                    name: stashless-collector
```

with a Secret named `stashless-collector` holding the variables. Workload identity with a Key Vault works the same way as on Azure, through `KEY_VAULT_URL` and the `*_NAME` variables.
