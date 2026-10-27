# Telemetry collector contract (schema version 1)

What a collector in a customer's estate reads, how it shapes the numbers,
and what it posts. The payload shape itself is `schema-v1.json` (served
at `/telemetry/schema/v1`); this document is the part a schema cannot
say: which reads, which fields, and the arithmetic. The four reference
editions (PowerShell, Python, the Docker image and the Azure Function,
the last two wrapping the Python one) implement exactly this, and the
conformance check proves they produce the same payload from the fixtures
in `fixtures/`.

Every judgement stays on the server. A collector reads, counts, buckets
and posts. It never decides what is healthy or slow.

## Principles

- **Counts, timings and version strings only.** No message subject, text,
  sender or body; no user names; no file names. The licence identifiers
  (`CUSTOMER_ID`, `INSTALLATION_ID`) sit on the same rows as the user
  count and are read, then dropped. Nothing read is ever logged: progress
  output names reads and counts.
- **Credentials stay in the estate.** The IAM client secret, the PSO
  password and the collector token come from environment variables,
  runbook parameters or a Key Vault read with a managed identity (the
  secret's name as `*_NAME` or, in PowerShell, as a `*Name` parameter,
  since Azure Automation variables are not visible to a script). The
  collector token authorises exactly one `POST`; nothing under `/api`
  returns data.
- **Direct and unredirected.** No proxy (the managed identity endpoint
  is link local and IFS is reached directly) and no redirect is ever
  followed, so a bearer token or session never travels to a host other
  than the one configured.
- **Read-only.** The only non-GET requests are the IAM token exchange
  and, for PSO, opening and closing the gateway session.
- **A partial read is still a report.** A refused or failing projection,
  or an answer of an unexpected shape, costs only its own figure; the heartbeat's `sources.*.last_error`
  carries the first reasons (status and read name, 120 printable ASCII
  characters). Only a failed IFS sign-in makes the run exit non-zero,
  and even then the heartbeat is posted so support can see the
  collector is alive. A Key Vault or managed identity failure is
  reported the same way, naming the variable, never the secret.

## The reads

All IFS reads go through the OData projections the `STASHLESS_READ`
permission set grants (the same set the pull connector uses), under
`/main/ifsapplications/projection/v1/{Projection}.svc/`. The OData option
names must stay raw (`$select`, never `%24select`), with only the values
percent-encoded; IFS Cloud answers HTTP 400 otherwise.

| Read name | Projection and entity set | Options | Used for |
| --- | --- | --- | --- |
| `token` | `/auth/realms/{realm}/protocol/openid-connect/token` | `grant_type=client_credentials`, `scope=openid` | sign-in; the realm comes from `IFS_REALM` or the `WWW-Authenticate` challenge of `/main/ifsapplications/web/` |
| `modules` | `InstalledComponentsHandling/ModuleSet` | none (a `$select` is a 400 here) | `release.base` from the **`FNDBAS`** row only, `release.base_registered_at` from its `RegDate` |
| `service_update` | `FrameworkServices/GetSystemParameter(ParameterKey='SERVICE_UPDATE')` | | `release.service_update` |
| `framework_update` | `FrameworkServices/GetSystemParameter(ParameterKey='MT_SERVICE_UPDATE')` | | `release.framework_update` |
| `server_alerts_last_24h` | `ServerAlertLogHandling/ServerLogs/$count` | `$filter=DateCreated ge {24h ago}` | `health.server_alerts_last_24h` |
| `server_alerts_unchecked` | `ServerAlertLogHandling/ServerLogs/$count` | `$filter=Checked eq false` | `health.server_alerts_unchecked` |
| `job_statuses` | `BackgroundJobsHandling/DeferredJobStatuses` | `$select=JobId,StatusType`, `$filter={time field} ge {24h ago}`, paged | `health.background_job_errors`, `health.background_job_warnings` |
| `logins_count` | `LoginAuditEvents/LoginEvents/$count` | `$filter=EventType eq 'LOGIN' and EventTimeUtc ge {7d ago}` | `health.logins_last_7_days` |
| `logins_last` | `LoginAuditEvents/LoginEvents` | `$filter=EventType eq 'LOGIN'`, `$orderby=EventTimeUtc desc`, `$top=1`, `$select=EventTimeUtc` | `health.last_login_at` |
| `licenses` | `LicenseMetricHandling/FndLicenses` | `$select=Parameter,Value` | `health.full_user_licenses` from `NUMBER_OF_FULL_USERS`; the identifiers are dropped |
| `batch_queues` | `BackgroundJobsHandling/BatchQueues` | none | `health.pso_batch_queue_jobs`: rows whose `Description` (or `Name`, `QueueName`, `QueueDescription`) contains "scheduling optimization", any case |
| `parallel_batch_queues` | `FrameworkServices/GetSystemParameter(ParameterKey='PARALLEL_BATCH_QUEUES')` | | `health.pso_batch_queues_configured` |
| `scheduling_datasets` | `PsoIntegrationDatasets/PsoSchedulingDatasetEntities` | none | `health.active_scheduling_datasets`: rows with `Active` true (a boolean, or `TRUE`/`Yes`) and a `ProcessType` other than blank or `None` |
| `messages` | `ApplicationMessagesHandling/ApplicationMessageSet` | `$select=Queue,StateDb,Receiver,CreatedDate,StateDate`, `$filter=CreatedDate ge {2h ago}`, `$orderby=CreatedDate asc`, paged | `message_timings` |

Rules behind the table:

- A `$count` answers a bare integer.
- `GetSystemParameter` answers `{"value": "..."}`; some releases wrap the
  value as a JSON string `{"Value": "..."}`, which is unwrapped. A blank
  value means the parameter is not set.
- `DeferredJobStatuses` names its timestamp property differently between
  releases. Try `Timestamp`, then `StatusTimestamp`, `StatusDate`,
  `CreatedDate`, `DateCreated`; an HTTP 400 means the next one. Count
  **distinct** `JobId`s per `StatusType` (`Error`, `Warning`, any case).
  No rows at all while other reads return data means the user cannot
  see status lines: send nothing for those two figures, never zero.
- `BatchQueues` with no rows, or rows without a name field: send nothing.
- Paged reads use `$top=5000` and `$skip`, up to 50,000 rows.
- Every figure that could not be read is left out of the payload. A
  section with nothing in it is left out.

### Server time

IFS Cloud returns some timestamps in the server's local time labelled as
UTC, and filters compare the same way. With `IFS_SERVER_TIMEZONE` set
(an IANA zone such as `America/Regina`), every timestamp the collector
reads is taken as wall-clock time in that zone (the `Z` or offset IFS
wrote is ignored), and every filter literal is written as that zone's
wall clock followed by `Z`. Without it, timestamps are what they say.
This is the same rule the pull connector applies.

## Message timing buckets

From the `messages` read, two hours back from the collector's clock and
floored to the hour (the server replaces every reported hour whole, so
the oldest bucket must be read complete):

1. Keep only messages whose `StateDb` is `Finished` or `Failed` (any
   case). Everything else is still in flight and is ignored.
2. Key each message by its created hour in UTC (`CreatedDate` floored to
   the hour), its queue and its receiver.
3. **Queue:** trimmed, any character outside `A-Z a-z 0-9 _ # . ( ) / space -`
   becomes `_`, blank becomes `(none)`, 60 characters.
4. **Receiver:** cut at the first dash followed by a space (IFS writes
   per-task text such as `WM Changes AL- Task seq - 398604`), every run
   of digits becomes `#`, any character outside the lane charset above
   becomes `_`, whitespace collapses to one space, leading and
   trailing spaces and dashes go, 60 characters, trailing spaces and
   dashes go again, blank becomes `(none)`. So `PSO_2024_NORTH` and
   `PSO_2025_NORTH` are one lane, `PSO_#_NORTH`. The server applies the
   same rule, and rejects a payload with two rows on one key, so the
   collector must apply it first.
5. **Finished** messages are timed: `StateDate` minus `CreatedDate` in
   seconds, never below zero, three decimals. **Failed** messages are
   counted in `failed_count` and never timed (their state date is when
   someone cleaned them up).
6. Per bucket: `finished_count` (timed messages), `failed_count`,
   `avg_seconds` (mean, three decimals), `p95_seconds` (nearest rank:
   the value at index `ceil(0.95 × n) - 1` of the sorted seconds),
   `max_seconds`. With no timed message the three seconds are `0.0`.
7. Sort buckets by hour, queue, receiver (ordinal). Every rounding in
   the payload is away from zero at the half (not banker's rounding).

Keeping the top twenty pairs by seven-day volume and rolling the rest
into `Other` is optional on the collector: the server does it again on
every report. The server also ignores buckets older than seven days or
more than an hour ahead, and refuses more than 48 distinct hours or
2,000 rows in one report.

## PSO (optional)

When `PSO_URL` is set, the collector opens a session on the IFS
Scheduling RESTful Gateway (`{PSO_URL}/IFSSchedulingRESTfulGateway`)
and closes it again whatever happens.

| Read name | Request | Used for |
| --- | --- | --- |
| `session` | `POST api/v1/scheduling/session`, multipart `accountId`, `userName`, `password` | the `apikey` header for the reads below; `DELETE` on the same path closes it |
| `systemstatus` | `GET api/v1/scheduling/systemstatus` | `recent_error_count`, `recent_warning_count`, `last_status_at`, `last_system_test_at`, `last_system_test_successful`, `dse_capability`, `live_dataset_footprint`, `waiting_dataset_footprint` (two decimals) |
| `systemstatus` failure | | costs the status figures only; `events`, `installations` and `usage` are still read |
| `events` | `GET api/v1/scheduling/events?minimumSeverity=E&earliestTime={24h ago}&maximumRows=500` | `errors_last_24h`: events whose `EventTime` is within the day, counted client side too in case the gateway ignores `earliestTime`; the message text is never kept |
| `installations` | `GET api/v1/system/installations` | `version` and `installation_state` from the first installation; FORBIDDEN and 404 are normal (the version then stays what the export said) |
| `usage` | `GET api/v1/scheduling/usage?minimumDateTime={7d ago}&maximumDateTime={tomorrow}` | the organisation totals: the latest row per `ScheduleDataUsageType` with no `DatasetId` (0 resources, 1 activities, 2 schedule window days, 3 appointment window days, 4 datasets), rounded to whole numbers |

## Heartbeat

`collector.implementation` is `powershell`, `python`, `docker`, `artisan`
or `other`; `collector.version` is the collector's own version.
`sources.ifs` and `sources.pso` carry `last_read_at` (when the source was
last read successfully) and `last_error`, so support can see which
edition reported and what it could not read.

## Fixture mode and the conformance test

Each reference collector takes `--fixtures <dir>` (`-Fixtures` in
PowerShell) and reads canned answers instead of IFS and PSO:

- `clock.json`: `{"now": "..."}`, the collector's clock, so
  `collected_at`, every window and every `last_read_at` are fixed.
- `ifs-cloud.json`: answers keyed by the read names in the first table.
  A missing key is what a refused read looks like; a missing `token`
  means IAM is down.
- `pso.json`: answers keyed by the PSO read names; a missing file means
  no PSO is configured.
- `expected-payload.json`: what every collector must produce from them.

The fixtures describe a fictional environment. Never build them from a
real customer's data. The conformance test runs each collector with
`--out`, compares the payload with the expected one, then posts it to
the ingest endpoint under test with the server's clock set to the
fixture clock, and checks every section was accepted. A runtime the
machine lacks (`pwsh`, `python3`) skips its case. In the collectors
repository, `scripts/conformance.sh` runs the same comparison without a
server.
