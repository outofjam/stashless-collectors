# Stashless reference collector as a container (plan 16, phase 4): the
# Python collector with nothing added, for a customer who schedules
# containers (Azure Container Apps jobs, Kubernetes CronJob, a plain
# cron with docker run). Build from the collectors directory so the
# Python script is in the context:
#
#   docker build -f docker/Dockerfile -t stashless-collector .
#
# Run it with the same environment variables the script documents:
#
#   docker run --rm \
#     -e STASHLESS_ENDPOINT=https://stashless.example.com/api/telemetry/v1 \
#     -e STASHLESS_TOKEN=sl_... \
#     -e IFS_URL=https://customer.ifs.cloud -e IFS_CLIENT_ID=STASHLESS_READ -e IFS_CLIENT_SECRET=... \
#     stashless-collector
#
# The image holds no credentials: every value arrives at run time, from
# the scheduler's secret store or a Key Vault through a managed identity
# (KEY_VAULT_URL plus the *_NAME variables). It reports as
# implementation "docker" so support can tell the editions apart.
FROM python:3.12-alpine

RUN adduser -D -H collector
USER collector
WORKDIR /collector

COPY --chown=collector:collector python/stashless_collector.py /collector/stashless_collector.py

ENV STASHLESS_COLLECTOR_IMPLEMENTATION=docker \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["python3", "-I", "/collector/stashless_collector.py"]
