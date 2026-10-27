#!/usr/bin/env python3
"""
Stashless reference collector, Python edition (plan 16, phase 4).

Runs inside the customer's estate, reads one IFS Cloud environment (and,
optionally, the PSO gateway paired with it) with credentials the customer
holds, shapes the version 1 telemetry payload and posts it to Stashless
with a collector token. Counts, timings and version strings only: it
selects no message subject, text or body, drops the licence identifiers
it cannot avoid reading, and never logs a value it read. Every judgement
(what is healthy, what is slow) stays on the server.

The contract it implements is docs/telemetry/aggregation.md; the schema
is docs/telemetry/schema-v1.json (served at /telemetry/schema/v1).

Configuration, as environment variables (every one also a flag):

  STASHLESS_ENDPOINT     https://stashless.example.com/api/telemetry/v1
  STASHLESS_TOKEN        the collector token (or STASHLESS_TOKEN_NAME in Key Vault)
  IFS_URL                https://customer.ifs.cloud
  IFS_CLIENT_ID          the read-only IAM client (STASHLESS_READ permission set)
  IFS_CLIENT_SECRET      its secret (or IFS_CLIENT_SECRET_NAME in Key Vault)
  IFS_REALM              optional; discovered from the login challenge when blank
  IFS_SERVER_TIMEZONE    optional IANA zone when IFS labels server-local time as UTC
  PSO_URL                optional; the PSO host (the gateway path is appended)
  PSO_ACCOUNT_ID, PSO_USER_NAME, PSO_PASSWORD (or PSO_PASSWORD_NAME in Key Vault)
  KEY_VAULT_URL          optional; secrets named *_NAME are read with the managed identity

Modes: post (default), --print (to stdout), --out FILE, and --fixtures DIR
(read canned answers instead of IFS and PSO, for the conformance test).
Exit code 0 when the report was posted and accepted (or written); 1 when
IFS could not be signed in to or the post was refused.

Python 3.9 or newer, standard library only.
"""

import argparse
import json
import math
from decimal import ROUND_HALF_UP, Decimal
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

VERSION = "1.0.0"
TIMEOUT_SECONDS = 15
PAGE = 5000
CAP = 50000
MESSAGE_FIELDS = "Queue,StateDb,Receiver,CreatedDate,StateDate"
JOB_STATUS_TIME_FIELDS = ["Timestamp", "StatusTimestamp", "StatusDate", "CreatedDate", "DateCreated"]
BATCH_QUEUE_NAME_FIELDS = ["Description", "Name", "QueueName", "QueueDescription"]
GATEWAY_PATH = "/IFSSchedulingRESTfulGateway"
USAGE_TYPES = {0: "resources_count", 1: "activities_count", 2: "schedule_window_days", 3: "appointment_window_days", 4: "datasets_count"}


class ReadError(Exception):
    """A read that did not succeed: status 401/403 is a refusal, anything else unreachable."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status

    @property
    def refused(self) -> bool:
        return self.status in (401, 403)


def log(message: str) -> None:
    """Progress to stderr. Names reads and counts, never values read."""
    sys.stderr.write(message + "\n")


# ----------------------------------------------------------------------
# Time
# ----------------------------------------------------------------------

def parse_time(value: str, server_local: bool = False, zone: Optional[str] = None) -> datetime:
    """RFC 3339 (Z or offset, any fraction) to an aware UTC datetime. With
    server_local and a zone, the offset IFS wrote is ignored and the wall
    clock is read in that zone, since IFS labels local time as UTC."""
    text = value.strip()
    if server_local and zone:
        text = re.sub(r"(Z|[+-]\d{2}:\d{2})$", "", text)
    text = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(zone) if (server_local and zone) else timezone.utc)
    return parsed.astimezone(timezone.utc)


def filter_literal(at: datetime, server_local: bool, zone: Optional[str]) -> str:
    """A moment as IFS compares it: wall clock with a Z for a server-local
    property, real UTC otherwise."""
    if server_local and zone:
        return at.astimezone(ZoneInfo(zone)).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rounded(value: float, decimals: int) -> float:
    """Half away from zero, like the PowerShell edition and the server."""
    quantum = Decimal(1).scaleb(-decimals)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def rfc3339(at: Optional[datetime]) -> Optional[str]:
    return None if at is None else at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------
# Sources: live HTTP or canned fixtures, same shape
# ----------------------------------------------------------------------

class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: a bearer token must never follow one."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


# Every request the collector makes: no proxy (the IMDS address is link
# local and IFS is reached directly), no redirects, a fixed timeout.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ssl.create_default_context()), NoRedirect())


class LiveSource:
    """Plain HTTPS through the shared opener."""

    def __init__(self, timeout: int):
        self.timeout = timeout
        self.opener = OPENER

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def request(self, method: str, url: str, headers: Dict[str, str], body: Optional[bytes] = None) -> Any:
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as error:
            raise ReadError("HTTP %d" % error.code, error.code)
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise ReadError("not reachable (%s)" % type(error).__name__)

    def ifs(self, name: str, url: str, headers: Dict[str, str]) -> Any:
        return self.request("GET", url, headers)

    def pso(self, name: str, method: str, url: str, headers: Dict[str, str], body: Optional[bytes] = None) -> Any:
        return self.request(method, url, headers, body)


class FixtureSource:
    """Canned answers from a directory: clock.json, ifs-cloud.json and
    pso.json, keyed by read name. A missing key is a refusal; a missing
    pso.json means no PSO is configured."""

    def __init__(self, directory: str):
        self.directory = directory
        self.ifs_data = self._load("ifs-cloud.json") or {}
        self.pso_data = self._load("pso.json")
        clock = self._load("clock.json") or {}
        self.fixed_now = parse_time(clock.get("now", "2026-01-01T00:00:00Z"))

    def _load(self, file: str) -> Optional[Any]:
        path = os.path.join(self.directory, file)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def now(self) -> datetime:
        return self.fixed_now

    def ifs(self, name: str, url: str, headers: Dict[str, str]) -> Any:
        if name not in self.ifs_data:
            raise ReadError("HTTP 403", 403)
        return self.ifs_data[name]

    def pso(self, name: str, method: str, url: str, headers: Dict[str, str], body: Optional[bytes] = None) -> Any:
        if self.pso_data is None or name not in self.pso_data:
            raise ReadError("HTTP 403", 403)
        return self.pso_data[name]


# ----------------------------------------------------------------------
# IFS Cloud
# ----------------------------------------------------------------------

class IfsReader:
    """The reads a Stashless pull sync makes, through the OData projections
    the STASHLESS_READ permission set grants, and nothing more."""

    def __init__(self, source: Any, url: str, client_id: str, secret: str, realm: Optional[str], zone: Optional[str]):
        self.source = source
        self.base = url.rstrip("/")
        self.client_id = client_id
        self.secret = secret
        self.realm = realm
        self.zone = zone
        self.token: Optional[str] = None
        self.errors: List[str] = []

    # -- plumbing --------------------------------------------------------

    def sign_in(self) -> None:
        """Client credentials against the environment's own IAM realm."""
        if isinstance(self.source, FixtureSource):
            self.token = str(self.source.ifs("token", "", {}).get("access_token", ""))
            return
        realm = self.realm or self.discover_realm()
        url = "%s/auth/realms/%s/protocol/openid-connect/token" % (self.base, realm)
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.secret, "scope": "openid"}).encode()
        try:
            answer = self.source.request("POST", url, {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, body)
        except ReadError as error:
            if error.status in (400, 401, 403):
                raise ReadError("IAM rejected the client credentials", error.status)
            raise ReadError("IAM %s" % error)
        token = (answer or {}).get("access_token")
        if not token:
            raise ReadError("IAM returned no token")
        self.token = token

    def discover_realm(self) -> str:
        """The realm named by the environment's own login challenge."""
        request = urllib.request.Request(self.base + "/main/ifsapplications/web/", method="GET")
        try:
            with OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
                challenge = response.headers.get("WWW-Authenticate", "")
        except urllib.error.HTTPError as error:
            challenge = error.headers.get("WWW-Authenticate", "")
        except (urllib.error.URLError, OSError) as error:
            raise ReadError("could not reach the environment to discover its realm (%s)" % type(error).__name__)
        match = re.search(r"/auth/realms/([^/\"\s]+)/?", challenge)
        if not match:
            raise ReadError("the environment presented no IFS IAM login challenge")
        return match.group(1)

    def read(self, name: str, projection: str, entity_set: str, query: Optional[Dict[str, str]] = None, accept: str = "application/json") -> Any:
        """One authenticated GET. The OData option names stay raw ($select,
        never %24select) and only the values are percent-encoded, which is
        what IFS Cloud accepts."""
        url = "%s/main/ifsapplications/projection/v1/%s.svc/%s" % (self.base, projection, entity_set)
        if query:
            url += "?" + "&".join("%s=%s" % (option, urllib.parse.quote(value, safe="'(),:")) for option, value in query.items())
        return self.source.ifs(name, url, {"Authorization": "Bearer %s" % self.token, "Accept": accept})

    def count(self, name: str, projection: str, entity_set: str, filter_: str) -> int:
        answer = self.read(name, projection, entity_set + "/$count", {"$filter": filter_}, accept="text/plain")
        return int(str(answer).strip())

    def paged(self, name: str, projection: str, entity_set: str, query: Dict[str, str]) -> List[Dict[str, Any]]:
        """$top/$skip pages to the cap; the fixtures answer one page."""
        rows: List[Dict[str, Any]] = []
        skip = 0
        while len(rows) < CAP:
            page = (self.read(name, projection, entity_set, {**query, "$top": str(PAGE), "$skip": str(skip)}) or {}).get("value", [])
            rows.extend(row for row in page if isinstance(row, dict))
            if len(page) < PAGE or isinstance(self.source, FixtureSource):
                return rows
            skip += PAGE
        log("IFS %s stopped at the %d row cap." % (entity_set, CAP))
        return rows[:CAP]

    def literal(self, at: datetime) -> str:
        return filter_literal(at, bool(self.zone), self.zone)

    def time(self, value: Any) -> Optional[datetime]:
        return parse_time(str(value), bool(self.zone), self.zone) if value else None

    def attempt(self, label: str, read: Callable[[], Any]) -> Any:
        """A read whose failure costs only its own figure: the reason is
        kept for the heartbeat (status and name, never content)."""
        try:
            return read()
        except ReadError as error:
            self.errors.append("%s: %s" % (label, error))
            log("IFS %s not read (%s)." % (label, error))
            return None
        except Exception as error:  # noqa: BLE001  an unexpected shape costs this figure only
            self.errors.append("%s: unexpected answer (%s)" % (label, type(error).__name__))
            log("IFS %s not read (unexpected answer, %s)." % (label, type(error).__name__))
            return None

    # -- the reads ---------------------------------------------------------

    def release(self) -> Optional[Dict[str, Any]]:
        """FNDBAS from ModuleSet (never the max module version: a customer's
        own layer shows as a module with an older version) plus the two
        FrameworkServices system parameters the About dialog reads."""
        modules = self.attempt("ModuleSet", lambda: (self.read("modules", "InstalledComponentsHandling", "ModuleSet") or {}).get("value", []))
        platform = next((row for row in modules or [] if isinstance(row, dict) and row.get("Module") == "FNDBAS"), None)
        if not platform or not platform.get("Version"):
            return None
        release = {"base": str(platform["Version"]).strip()}
        service_update = self.attempt("SERVICE_UPDATE", lambda: self.system_parameter("service_update", "SERVICE_UPDATE"))
        framework_update = self.attempt("MT_SERVICE_UPDATE", lambda: self.system_parameter("framework_update", "MT_SERVICE_UPDATE"))
        if service_update:
            release["service_update"] = service_update
        if framework_update:
            release["framework_update"] = framework_update
        if platform.get("RegDate"):
            release["base_registered_at"] = rfc3339(parse_time(str(platform["RegDate"])))
        return release

    def system_parameter(self, name: str, key: str) -> Optional[str]:
        answer = self.read(name, "FrameworkServices", "GetSystemParameter(ParameterKey='%s')" % key) or {}
        value = answer.get("value")
        if isinstance(value, str) and value.strip().startswith("{"):
            value = json.loads(value).get("Value")
        return value.strip() if isinstance(value, str) and value.strip() else None

    def health(self, now: datetime) -> Dict[str, Any]:
        """Counts only. The licence identifiers are on the same rows as the
        user count and are dropped here, on purpose."""
        day_ago, week_ago = now - timedelta(days=1), now - timedelta(days=7)
        health: Dict[str, Any] = {}
        health["server_alerts_last_24h"] = self.attempt("ServerLogs", lambda: self.count("server_alerts_last_24h", "ServerAlertLogHandling", "ServerLogs", "DateCreated ge " + self.literal(day_ago)))
        health["server_alerts_unchecked"] = self.attempt("ServerLogs", lambda: self.count("server_alerts_unchecked", "ServerAlertLogHandling", "ServerLogs", "Checked eq false"))
        jobs = self.attempt("DeferredJobStatuses", lambda: self.job_status_counts(day_ago))
        if jobs:
            health["background_job_errors"], health["background_job_warnings"] = jobs
        health["logins_last_7_days"] = self.attempt("LoginEvents", lambda: self.count("logins_count", "LoginAuditEvents", "LoginEvents", "EventType eq 'LOGIN' and EventTimeUtc ge " + self.literal(week_ago)))
        last_login = self.attempt("LoginEvents", lambda: ((self.read("logins_last", "LoginAuditEvents", "LoginEvents", {"$filter": "EventType eq 'LOGIN'", "$orderby": "EventTimeUtc desc", "$top": "1", "$select": "EventTimeUtc"}) or {}).get("value") or [{}])[0].get("EventTimeUtc"))
        health["last_login_at"] = rfc3339(self.time(last_login)) if last_login else None
        licenses = self.attempt("FndLicenses", lambda: (self.read("licenses", "LicenseMetricHandling", "FndLicenses", {"$select": "Parameter,Value"}) or {}).get("value", []))
        users = next((row.get("Value") for row in licenses or [] if isinstance(row, dict) and row.get("Parameter") == "NUMBER_OF_FULL_USERS"), None)
        health["full_user_licenses"] = int(users) if isinstance(users, (int, str)) and str(users).strip().isdigit() else None
        health["pso_batch_queue_jobs"] = self.attempt("BatchQueues", self.pso_batch_queues)
        configured = self.attempt("PARALLEL_BATCH_QUEUES", lambda: self.system_parameter("parallel_batch_queues", "PARALLEL_BATCH_QUEUES"))
        health["pso_batch_queues_configured"] = int(configured) if configured and configured.isdigit() else None
        health["active_scheduling_datasets"] = self.attempt("PsoSchedulingDatasetEntities", self.active_scheduling_datasets)
        return {key: value for key, value in health.items() if value is not None}

    def job_status_counts(self, since: datetime) -> Optional[tuple]:
        """Distinct jobs with an Error or Warning status line in the last 24
        hours, from DeferredJobStatuses (job id and status type only). The
        timestamp property differs between releases; a 400 tries the next."""
        rows = None
        for field in JOB_STATUS_TIME_FIELDS:
            try:
                rows = self.paged("job_statuses", "BackgroundJobsHandling", "DeferredJobStatuses", {"$select": "JobId,StatusType", "$filter": "%s ge %s" % (field, self.literal(since))})
                break
            except ReadError as error:
                if error.status != 400:
                    raise
        if rows is None:
            raise ReadError("no known timestamp property", 400)
        if not rows:
            return None
        jobs: Dict[str, set] = {"error": set(), "warning": set()}
        for row in rows:
            kind = str(row.get("StatusType", "")).strip().lower()
            if kind in jobs and row.get("JobId") is not None:
                jobs[kind].add(str(row["JobId"]))
        return len(jobs["error"]), len(jobs["warning"])

    def pso_batch_queues(self) -> Optional[int]:
        rows = [row for row in (self.read("batch_queues", "BackgroundJobsHandling", "BatchQueues") or {}).get("value", []) if isinstance(row, dict)]
        if not rows:
            return None
        fields = [field for field in BATCH_QUEUE_NAME_FIELDS if field in rows[0]]
        if not fields:
            return None
        return sum(1 for row in rows if any("scheduling optimization" in str(row.get(field, "")).lower() for field in fields))

    def active_scheduling_datasets(self) -> Optional[int]:
        rows = [row for row in (self.read("scheduling_datasets", "PsoIntegrationDatasets", "PsoSchedulingDatasetEntities") or {}).get("value", []) if isinstance(row, dict)]
        if any("Active" not in row or "ProcessType" not in row for row in rows):
            raise ReadError("rows lack Active or ProcessType")
        return sum(1 for row in rows if enabled(row["Active"]) and str(row["ProcessType"]).strip().lower() not in ("", "none"))

    def messages(self, since: datetime) -> List[Dict[str, Any]]:
        """Exactly the five timing fields of ApplicationMessageSet."""
        return self.paged("messages", "ApplicationMessagesHandling", "ApplicationMessageSet", {"$select": MESSAGE_FIELDS, "$filter": "CreatedDate ge " + self.literal(since), "$orderby": "CreatedDate asc"})


def enabled(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() in ("true", "yes"))


# ----------------------------------------------------------------------
# Message timing aggregation (the server applies the same rules again)
# ----------------------------------------------------------------------

LANE_CHARSET = re.compile(r"[^A-Za-z0-9_#.()/ -]")


def normalise_receiver(receiver: str) -> str:
    """Cut at the first dash followed by a space, digits become #, whitespace
    collapses, 60 characters: the server's rule, applied here too so two
    receivers that land on one lane never reach the server as duplicates.
    A character the server's lane charset refuses becomes an underscore,
    since one odd receiver must not cost the whole report."""
    text = re.split(r"-\s", receiver, maxsplit=1)[0]
    text = re.sub(r"\d+", "#", text)
    text = LANE_CHARSET.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip(" -\t")
    text = text[:60].rstrip(" -")
    return text or "(none)"


def normalise_queue(queue: str) -> str:
    text = LANE_CHARSET.sub("_", queue.strip())
    return (text or "(none)")[:60]


def aggregate(messages: List[Dict[str, Any]], time: Callable[[Any], Optional[datetime]]) -> List[Dict[str, Any]]:
    """Hourly buckets per queue and receiver (created hour, UTC): Finished
    messages are timed (state minus created), Failed ones only counted,
    every other state ignored. p95 is nearest rank."""
    groups: Dict[tuple, Dict[str, Any]] = {}
    for row in messages:
        state = str(row.get("StateDb", "")).strip().lower()
        created = time(row.get("CreatedDate"))
        if state not in ("finished", "failed") or created is None:
            continue
        key = (created.replace(minute=0, second=0, microsecond=0), normalise_queue(str(row.get("Queue", ""))), normalise_receiver(str(row.get("Receiver", ""))))
        group = groups.setdefault(key, {"seconds": [], "failed": 0})
        if state == "failed":
            group["failed"] += 1
        elif row.get("StateDate"):
            finished = time(row["StateDate"])
            if finished is not None:
                group["seconds"].append(max(0.0, (finished - created).total_seconds()))
    buckets = []
    for (hour, queue, receiver), group in sorted(groups.items()):
        seconds = sorted(group["seconds"])
        buckets.append({
            "bucket_start": rfc3339(hour),
            "queue": queue,
            "receiver": receiver,
            "finished_count": len(seconds),
            "failed_count": group["failed"],
            "avg_seconds": rounded(sum(seconds) / len(seconds), 3) if seconds else 0.0,
            "p95_seconds": percentile(seconds, 0.95),
            "max_seconds": rounded(seconds[-1], 3) if seconds else 0.0,
        })
    return buckets


def percentile(sorted_values: List[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = max(0, math.ceil(fraction * len(sorted_values)) - 1)
    return rounded(sorted_values[min(index, len(sorted_values) - 1)], 3)


# ----------------------------------------------------------------------
# PSO gateway
# ----------------------------------------------------------------------

class PsoReader:
    """A session with a PSO user, the three status reads, the session
    closed again whatever happened. Event messages are counted, never kept."""

    def __init__(self, source: Any, url: str, account_id: str, user_name: str, password: str):
        self.source = source
        self.base = url.rstrip("/") + GATEWAY_PATH
        self.account_id = account_id
        self.user_name = user_name
        self.password = password
        self.token: Optional[str] = None
        self.errors: List[str] = []

    def open_session(self) -> None:
        boundary = "stashless-collector"
        parts = []
        for name, value in (("accountId", self.account_id), ("userName", self.user_name), ("password", self.password)):
            parts.append("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, name, value))
        body = ("".join(parts) + "--%s--\r\n" % boundary).encode()
        try:
            answer = self.source.pso("session", "POST", self.base + "/api/v1/scheduling/session", {"Content-Type": "multipart/form-data; boundary=" + boundary, "Accept": "application/json"}, body)
        except ReadError as error:
            if error.status in (400, 401, 403):
                raise ReadError("the gateway rejected the account, user name or password", error.status)
            raise
        token = (answer or {}).get("SessionToken")
        if not token:
            raise ReadError("the gateway returned no session token")
        self.token = str(token)

    def close_session(self) -> None:
        if self.token is None or isinstance(self.source, FixtureSource):
            return
        try:
            self.source.pso("session_close", "DELETE", self.base + "/api/v1/scheduling/session", {"apikey": self.token})
        except ReadError:
            log("PSO session could not be closed; it expires on its own.")

    def get(self, name: str, path: str, query: Optional[Dict[str, str]] = None) -> Any:
        url = self.base + "/" + path + ("?" + urllib.parse.urlencode(query) if query else "")
        return self.source.pso(name, "GET", url, {"apikey": self.token or "", "Accept": "application/json"})

    def attempt(self, label: str, read: Callable[[], Any]) -> Any:
        try:
            return read()
        except ReadError as error:
            self.errors.append("%s: %s" % (label, error))
            log("PSO %s not read (%s)." % (label, error))
            return None
        except Exception as error:  # noqa: BLE001
            self.errors.append("%s: unexpected answer (%s)" % (label, type(error).__name__))
            log("PSO %s not read (unexpected answer, %s)." % (label, type(error).__name__))
            return None

    def section(self, now: datetime) -> Dict[str, Any]:
        since = now - timedelta(days=1)
        status = self.attempt("systemstatus", lambda: self.get("systemstatus", "api/v1/scheduling/systemstatus"))
        status = status if isinstance(status, dict) else None
        section: Dict[str, Any] = {}
        if status is not None:
            section = {
                "recent_error_count": int(status.get("RecentErrorCount") or 0),
                "recent_warning_count": int(status.get("RecentWarningCount") or 0),
                "last_status_at": rfc3339(parse_time(status["LastStatusTime"])) if status.get("LastStatusTime") else None,
                "last_system_test_at": rfc3339(parse_time(status["LastSystemTestTime"])) if status.get("LastSystemTestTime") else None,
                "last_system_test_successful": bool(status["LastSystemTestSuccessful"]) if status.get("LastSystemTestSuccessful") is not None else None,
                "dse_capability": decimal(status.get("TotalDSECapability")),
                "live_dataset_footprint": decimal(status.get("TotalLiveDatasetFootprint")),
                "waiting_dataset_footprint": decimal(status.get("TotalWaitingDatasetFootprint")),
            }
        events = self.attempt("events", lambda: (self.get("events", "api/v1/scheduling/events", {"minimumSeverity": "E", "earliestTime": rfc3339(since), "maximumRows": "500"}) or {}).get("Events", []))
        if events is not None:
            section["errors_last_24h"] = sum(1 for event in events if isinstance(event, dict) and event.get("EventTime") and parse_time(str(event["EventTime"])) >= since)
        installations = self.attempt("installations", lambda: self.get("installations", "api/v1/system/installations"))
        first = installations[0] if isinstance(installations, list) and installations else installations if isinstance(installations, dict) else None
        if isinstance(first, dict) and first.get("Version"):
            section["version"] = str(first["Version"])
            if first.get("State"):
                section["installation_state"] = str(first["State"])
        usage = self.attempt("usage", lambda: (self.get("usage", "api/v1/scheduling/usage", {"minimumDateTime": (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S"), "maximumDateTime": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")}) or {}).get("ScheduleDataUsages", []))
        latest: Dict[int, tuple] = {}
        for row in usage or []:
            if not isinstance(row, dict) or row.get("DatasetId") is not None or row.get("ScheduleDataUsageType") is None or row.get("Value") is None:
                continue
            kind = int(row["ScheduleDataUsageType"])
            stamp = str(row.get("DatetimeStamp") or "")
            if kind in USAGE_TYPES and (kind not in latest or stamp >= latest[kind][0]):
                latest[kind] = (stamp, int(rounded(float(row["Value"]), 0)))
        for kind, (_, value) in latest.items():
            section[USAGE_TYPES[kind]] = value
        return {key: value for key, value in section.items() if value is not None}


def decimal(value: Any) -> Optional[float]:
    try:
        return rounded(float(value), 2) if value is not None else None
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# Secrets through the managed identity (Azure)
# ----------------------------------------------------------------------

def key_vault_secret(vault_url: str, name: str) -> str:
    """Reads one secret with the managed identity: the App Service and
    Automation endpoint when present, the IMDS endpoint otherwise. The
    value is returned to the caller and never logged."""
    resource = "https://vault.azure.net"
    if os.environ.get("IDENTITY_ENDPOINT") and os.environ.get("IDENTITY_HEADER"):
        url = "%s?%s" % (os.environ["IDENTITY_ENDPOINT"], urllib.parse.urlencode({"resource": resource, "api-version": "2019-08-01"}))
        headers = {"X-IDENTITY-HEADER": os.environ["IDENTITY_HEADER"]}
    else:
        url = "http://169.254.169.254/metadata/identity/oauth2/token?" + urllib.parse.urlencode({"resource": resource, "api-version": "2018-02-01"})
        headers = {"Metadata": "true"}
    with OPENER.open(urllib.request.Request(url, headers=headers), timeout=TIMEOUT_SECONDS) as response:
        token = json.loads(response.read().decode())["access_token"]
    secret_url = "%s/secrets/%s?api-version=7.4" % (vault_url.rstrip("/"), urllib.parse.quote(name))
    with OPENER.open(urllib.request.Request(secret_url, headers={"Authorization": "Bearer " + token}), timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode())["value"]


def setting(args: argparse.Namespace, flag: str, env: str) -> Optional[str]:
    value = getattr(args, flag, None) or os.environ.get(env)
    return value.strip() if isinstance(value, str) and value.strip() else None


def secret_setting(args: argparse.Namespace, flag: str, env: str) -> Optional[str]:
    """The value itself, or the Key Vault secret named by *_NAME. A vault
    or identity failure is a ReadError naming the variable, never the
    secret name or the URL, so it lands in the heartbeat."""
    direct = setting(args, flag, env)
    if direct:
        return direct
    name, vault = os.environ.get(env + "_NAME"), setting(args, "key_vault_url", "KEY_VAULT_URL")
    if not (name and vault):
        return None
    try:
        return key_vault_secret(vault, name)
    except urllib.error.HTTPError as error:
        raise ReadError("Key Vault read for %s_NAME: HTTP %d" % (env, error.code), error.code)
    except Exception as error:  # noqa: BLE001
        raise ReadError("Key Vault read for %s_NAME: %s" % (env, type(error).__name__))


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def brief(errors: List[str]) -> Optional[str]:
    """The first reasons, printable ASCII, 120 characters: the heartbeat's
    last_error, shown in the sync history and never stored elsewhere."""
    if not errors:
        return None
    text = re.sub(r"[^ -~]", "?", "; ".join(errors))
    return text[:120]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Read an IFS Cloud environment and report telemetry to Stashless.")
    parser.add_argument("--endpoint")
    parser.add_argument("--token")
    parser.add_argument("--ifs-url", dest="ifs_url")
    parser.add_argument("--ifs-client-id", dest="ifs_client_id")
    parser.add_argument("--ifs-client-secret", dest="ifs_client_secret")
    parser.add_argument("--ifs-realm", dest="ifs_realm")
    parser.add_argument("--ifs-server-timezone", dest="ifs_server_timezone")
    parser.add_argument("--pso-url", dest="pso_url")
    parser.add_argument("--pso-account-id", dest="pso_account_id")
    parser.add_argument("--pso-user-name", dest="pso_user_name")
    parser.add_argument("--pso-password", dest="pso_password")
    parser.add_argument("--key-vault-url", dest="key_vault_url")
    parser.add_argument("--implementation", choices=["python", "docker"], default=None)
    parser.add_argument("--fixtures", help="Directory of canned answers; no network is used")
    parser.add_argument("--out", help="Write the payload to this file instead of posting")
    parser.add_argument("--print", action="store_true", help="Print the payload instead of posting")
    args = parser.parse_args(argv)
    implementation = args.implementation or os.environ.get("STASHLESS_COLLECTOR_IMPLEMENTATION", "python").strip().lower()
    if implementation not in ("python", "docker"):
        log("STASHLESS_COLLECTOR_IMPLEMENTATION must be python or docker; reporting as python.")
        implementation = "python"

    source: Any = FixtureSource(args.fixtures) if args.fixtures else LiveSource(TIMEOUT_SECONDS)
    now = source.now()
    payload: Dict[str, Any] = {
        "schema_version": 1,
        "collected_at": rfc3339(now),
        "collector": {"implementation": implementation, "version": VERSION},
        "sources": {},
    }
    failed = False

    # IFS Cloud: sign in first, so a wrong secret or an unreachable IAM is
    # one clear error; after that each read only costs its own figure.
    ifs = IfsReader(
        source,
        setting(args, "ifs_url", "IFS_URL") or "https://fixture.invalid",
        setting(args, "ifs_client_id", "IFS_CLIENT_ID") or "",
        "",
        setting(args, "ifs_realm", "IFS_REALM"),
        setting(args, "ifs_server_timezone", "IFS_SERVER_TIMEZONE"),
    )
    try:
        ifs.secret = secret_setting(args, "ifs_client_secret", "IFS_CLIENT_SECRET") or ""
        ifs.sign_in()
        release = ifs.release()
        if release:
            payload["release"] = release
        health = ifs.health(now)
        if health:
            payload["health"] = health
        # Two hours back, floored to the hour: the server replaces every
        # reported hour whole, so the oldest bucket must be read complete.
        window_start = (now - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
        timings = ifs.attempt("ApplicationMessageSet", lambda: ifs.messages(window_start))
        if timings is not None:
            payload["message_timings"] = aggregate(timings, ifs.time)
        payload["sources"]["ifs"] = {"last_read_at": rfc3339(source.now()), "last_error": brief(ifs.errors)}
        log("IFS read: release %s, %d health figures, %d timing buckets." % ("yes" if release else "no", len(health), len(payload.get("message_timings", []))))
    except ReadError as error:
        failed = True
        payload["sources"]["ifs"] = {"last_read_at": None, "last_error": brief(["sign-in: %s" % error] + ifs.errors)}
        log("IFS could not be read: %s" % error)
    except Exception as error:  # noqa: BLE001  the heartbeat still goes out
        failed = True
        payload["sources"]["ifs"] = {"last_read_at": None, "last_error": brief(["sign-in: unexpected failure (%s)" % type(error).__name__] + ifs.errors)}
        log("IFS could not be read: unexpected failure (%s)." % type(error).__name__)

    # PSO, when configured (or when the fixtures carry it).
    pso_url = setting(args, "pso_url", "PSO_URL")
    if pso_url or (isinstance(source, FixtureSource) and source.pso_data is not None):
        pso = PsoReader(source, pso_url or "https://fixture.invalid", setting(args, "pso_account_id", "PSO_ACCOUNT_ID") or "", setting(args, "pso_user_name", "PSO_USER_NAME") or "", "")
        try:
            pso.password = secret_setting(args, "pso_password", "PSO_PASSWORD") or ""
            pso.open_session()
            try:
                payload["pso"] = pso.section(now)
            finally:
                pso.close_session()
            payload["sources"]["pso"] = {"last_read_at": rfc3339(source.now()), "last_error": brief(pso.errors)}
            log("PSO read: %d figures." % len(payload["pso"]))
        except ReadError as error:
            payload["sources"]["pso"] = {"last_read_at": None, "last_error": brief(["session: %s" % error] + pso.errors)}
            log("PSO could not be read: %s" % error)
        except Exception as error:  # noqa: BLE001
            payload["sources"]["pso"] = {"last_read_at": None, "last_error": brief(["session: unexpected failure (%s)" % type(error).__name__] + pso.errors)}
            log("PSO could not be read: unexpected failure (%s)." % type(error).__name__)

    body = json.dumps(payload, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(body + "\n")
        log("Payload written to %s." % args.out)
        return 1 if failed else 0
    if args.print:
        print(body)
        return 1 if failed else 0

    endpoint = setting(args, "endpoint", "STASHLESS_ENDPOINT")
    try:
        token = secret_setting(args, "token", "STASHLESS_TOKEN")
    except ReadError as error:
        log("The collector token could not be read: %s" % error)
        return 1
    if not endpoint or not token:
        log("STASHLESS_ENDPOINT and STASHLESS_TOKEN (or STASHLESS_TOKEN_NAME with KEY_VAULT_URL) are needed to post.")
        return 1
    request = urllib.request.Request(endpoint, data=body.encode(), method="POST", headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "stashless-collector-python/" + VERSION})
    try:
        with OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
            log("Stashless answered %d: %s" % (response.status, response.read().decode()[:300]))
            return 1 if failed else 0
    except urllib.error.HTTPError as error:
        log("Stashless refused the report with HTTP %d: %s" % (error.code, error.read().decode()[:300]))
        return 1
    except (urllib.error.URLError, OSError) as error:
        log("Stashless could not be reached (%s)." % type(error).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
