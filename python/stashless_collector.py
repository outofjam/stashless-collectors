#!/usr/bin/env python3
"""
Stashless reference collector, Python edition.

Runs inside the customer's estate, reads one IFS Cloud environment (and,
optionally, the PSO gateway paired with it) with credentials the customer
holds, shapes the version 1 telemetry payload and posts it to Stashless
with a collector token. Counts, timings and version strings only: it
selects no message subject, text or body, drops the licence identifiers
it cannot avoid reading, and never logs a value it read. Every judgement
(what is healthy, what is slow) stays on the server.

The contract it implements is aggregation.md and the schema is
schema-v1.json, both in the docs folder of the collectors repository and
served by Stashless at /telemetry/schema/v1.

NETWORK SURFACE, for whoever reviews this file: every request leaves
through the one OPENER below (no proxy, no redirects, a 15 second
timeout). The hosts reached are the IFS Cloud host (one POST to its IAM
token endpoint, then GETs), the PSO host when configured (one POST to
open a session, GETs, one DELETE to close it), the Key Vault and the
managed identity endpoint when secret names are used, and the Stashless
endpoint (one POST). Nothing else. Every configured URL must be https.

Configuration, as environment variables (every one is also a flag):

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
  STASHLESS_COLLECTOR_IMPLEMENTATION  python (default) or docker, for the heartbeat

Secrets belong in the environment or in Key Vault. The --token,
--ifs-client-secret and --pso-password flags exist for a one-off test
from a shell and show the value in the process list; do not schedule
them.

Modes: post (default), --print (to stdout), --out FILE, and --fixtures DIR
(canned answers instead of IFS and PSO, for the conformance check).
Exit code 0 when the report was posted and accepted (or written); 1 when
IFS could not be signed in to, a secret or the endpoint could not be
had, or the post was refused.

Python 3.9 or newer, standard library only.
"""

import argparse
import json
import logging
import math
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple
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
LANE_CHARSET = re.compile(r"[^A-Za-z0-9_#.()/ -]")
IDENTITY_RESOURCE = "https://vault.azure.net"


class ReadError(Exception):
    """A read that did not succeed: status 401 or 403 is a refusal, anything else unreachable."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def log(message: str) -> None:
    """Progress, naming reads and counts, never a value read. Goes to the
    logging module when something configured it (an Azure Function),
    else to stderr."""
    if logging.getLogger().handlers:
        logging.getLogger("stashless").info(message)
    else:
        sys.stderr.write(message + "\n")


def require_https(name: str, url: str) -> str:
    """A configured URL must be https, or a bearer token could travel in clear."""
    if not url.lower().startswith("https://"):
        raise ReadError("%s must start with https://" % name)
    return url.rstrip("/")


# ----------------------------------------------------------------------
# Time
# ----------------------------------------------------------------------

def parse_time(value: str, server_local: bool = False, zone: Optional[str] = None) -> datetime:
    """RFC 3339 (Z or offset, any fraction length) to an aware UTC datetime.
    With server_local and a zone, the offset IFS wrote is ignored and the
    wall clock is read in that zone, since IFS labels local time as UTC."""
    text = value.strip()
    if server_local and zone:
        text = re.sub(r"(Z|[+-]\d{2}:\d{2})$", "", text)
    # fromisoformat on 3.9 wants exactly 3 or 6 fraction digits; .NET sends any number.
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


def rfc3339(at: Optional[datetime]) -> Optional[str]:
    return None if at is None else at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rounded(value: float, decimals: int) -> float:
    """Half away from zero, like the PowerShell edition and the server."""
    quantum = Decimal(1).scaleb(-decimals)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def decimal(value: Any) -> Optional[float]:
    try:
        return rounded(float(value), 2) if value is not None else None
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# The one way out: a source is live HTTPS or canned fixtures, same shape
# ----------------------------------------------------------------------

class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: a bearer token must never follow one."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# No proxy (the identity endpoint is link local and IFS is reached
# directly), no redirects. Every request in this file goes through it.
OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    NoRedirect(),
)


def http(method: str, url: str, headers: Dict[str, str], body: Optional[bytes] = None) -> Any:
    """One request through OPENER; the parsed JSON body (or the text when
    it is not JSON, as an OData $count is). HTTP errors and connection
    failures become ReadError with the status, never the body."""
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        raise ReadError("HTTP %d" % error.code, error.code)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise ReadError("not reachable (%s)" % type(error).__name__)
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.strip()


class LiveSource:
    """Live reads: every call is an HTTPS request through OPENER."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def ifs(self, name: str, method: str, url: str, headers: Dict[str, str], body: Optional[bytes] = None) -> Any:
        return http(method, url, headers, body)

    def pso(self, name: str, method: str, url: str, headers: Dict[str, str], body: Optional[bytes] = None) -> Any:
        return http(method, url, headers, body)

    def is_live(self) -> bool:
        return True


class FixtureSource:
    """Canned answers from a directory (clock.json, ifs-cloud.json and
    pso.json, keyed by read name), so the whole run, sign-in included,
    can be exercised with no network. A missing key is a refusal, a
    missing pso.json means no PSO is configured, and the session close
    is accepted silently."""

    def __init__(self, directory: str):
        self.ifs_data = self._load(directory, "ifs-cloud.json") or {}
        self.pso_data = self._load(directory, "pso.json")
        clock = self._load(directory, "clock.json") or {}
        self.fixed_now = parse_time(clock.get("now", "2026-01-01T00:00:00Z"))

    @staticmethod
    def _load(directory: str, file: str) -> Optional[Any]:
        path = os.path.join(directory, file)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def now(self) -> datetime:
        return self.fixed_now

    def ifs(self, name: str, method: str, url: str, headers: Dict[str, str], body: Optional[bytes] = None) -> Any:
        if name not in self.ifs_data:
            raise ReadError("HTTP 403", 403)
        return self.ifs_data[name]

    def pso(self, name: str, method: str, url: str, headers: Dict[str, str], body: Optional[bytes] = None) -> Any:
        if name == "session_close":
            return None
        if self.pso_data is None or name not in self.pso_data:
            raise ReadError("HTTP 403", 403)
        return self.pso_data[name]

    def is_live(self) -> bool:
        return False


def attempt(errors: List[str], system: str, label: str, read: Callable[[], Any]) -> Any:
    """A read whose failure costs only its own figure. The reason (the
    read's name and the status, never content) is kept for the heartbeat
    and logged; an answer of an unexpected shape counts the same way."""
    try:
        return read()
    except ReadError as error:
        errors.append("%s: %s" % (label, error))
        log("%s %s not read (%s)." % (system, label, error))
    except Exception as error:
        errors.append("%s: unexpected answer (%s)" % (label, type(error).__name__))
        log("%s %s not read (unexpected answer, %s)." % (system, label, type(error).__name__))
    return None


# ----------------------------------------------------------------------
# IFS Cloud
# ----------------------------------------------------------------------

class IfsReader:
    """The reads a Stashless pull sync makes, through the OData projections
    the STASHLESS_READ permission set grants, and nothing more."""

    def __init__(self, source: Any, url: str, client_id: str, realm: Optional[str], zone: Optional[str]):
        self.source = source
        self.base = url
        self.client_id = client_id
        self.realm = realm
        self.zone = zone
        self.token: Optional[str] = None
        self.errors: List[str] = []

    # -- sign-in and plumbing ---------------------------------------------

    def sign_in(self, secret: str) -> None:
        """Client credentials against the environment's own IAM realm. The
        secret is used here once and kept nowhere."""
        realm = self.realm or ("fixture" if not self.source.is_live() else self.discover_realm())
        url = "%s/auth/realms/%s/protocol/openid-connect/token" % (self.base, realm)
        form = {"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": secret, "scope": "openid"}
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        try:
            answer = self.source.ifs("token", "POST", url, headers, urllib.parse.urlencode(form).encode())
        except ReadError as error:
            if error.status in (400, 401, 403):
                raise ReadError("IAM rejected the client credentials", error.status)
            raise ReadError("IAM %s" % error)
        token = answer.get("access_token") if isinstance(answer, dict) else None
        if not token:
            raise ReadError("IAM returned no token")
        self.token = token

    def discover_realm(self) -> str:
        """The realm named by the environment's own login challenge: the web
        root answers 401 with a WWW-Authenticate header. Through OPENER,
        so a redirect there is refused like everywhere else."""
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
        return self.source.ifs(name, "GET", url, {"Authorization": "Bearer %s" % self.token, "Accept": accept})

    def rows(self, name: str, projection: str, entity_set: str, query: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        """The "value" rows of one answer, dicts only."""
        answer = self.read(name, projection, entity_set, query)
        value = answer.get("value", []) if isinstance(answer, dict) else []
        return [row for row in value if isinstance(row, dict)]

    def count(self, name: str, projection: str, entity_set: str, filter_: str) -> int:
        """An OData $count, which IFS answers with a bare integer."""
        answer = self.read(name, projection, entity_set + "/$count", {"$filter": filter_}, accept="text/plain")
        return int(str(answer).strip())

    def paged(self, name: str, projection: str, entity_set: str, query: Dict[str, str]) -> List[Dict[str, Any]]:
        """$top/$skip pages, in order, to the cap. A short page ends it."""
        rows: List[Dict[str, Any]] = []
        skip = 0
        while len(rows) < CAP:
            page = self.rows(name, projection, entity_set, {**query, "$top": str(PAGE), "$skip": str(skip)})
            rows.extend(page)
            if len(page) < PAGE:
                return rows
            skip += PAGE
        log("IFS %s stopped at the %d row cap." % (entity_set, CAP))
        return rows[:CAP]

    def literal(self, at: datetime) -> str:
        return filter_literal(at, bool(self.zone), self.zone)

    def time(self, value: Any) -> Optional[datetime]:
        return parse_time(str(value), bool(self.zone), self.zone) if value else None

    def attempt(self, label: str, read: Callable[[], Any]) -> Any:
        return attempt(self.errors, "IFS", label, read)

    # -- the reads ---------------------------------------------------------

    def release(self) -> Optional[Dict[str, Any]]:
        """FNDBAS from ModuleSet (never the max module version: a customer's
        own layer shows as a module with an older version) plus the two
        FrameworkServices system parameters the About dialog reads."""
        modules = self.attempt("ModuleSet", lambda: self.rows("modules", "InstalledComponentsHandling", "ModuleSet")) or []
        platform = next((row for row in modules if row.get("Module") == "FNDBAS"), None)
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
        """GetSystemParameter answers {"value": "..."}; some releases wrap
        the value once more as a JSON string {"Value": "..."}."""
        answer = self.read(name, "FrameworkServices", "GetSystemParameter(ParameterKey='%s')" % key)
        value = answer.get("value") if isinstance(answer, dict) else None
        if isinstance(value, str) and value.strip().startswith("{"):
            value = json.loads(value).get("Value")
        return value.strip() if isinstance(value, str) and value.strip() else None

    def health(self, now: datetime) -> Dict[str, Any]:
        """Counts only. A figure that could not be read is left out."""
        day_ago = now - timedelta(days=1)
        week_ago = now - timedelta(days=7)
        health: Dict[str, Any] = {
            "server_alerts_last_24h": self.attempt("ServerLogs", lambda: self.server_alerts("server_alerts_last_24h", "DateCreated ge " + self.literal(day_ago))),
            "server_alerts_unchecked": self.attempt("ServerLogs", lambda: self.server_alerts("server_alerts_unchecked", "Checked eq false")),
            "logins_last_7_days": self.attempt("LoginEvents", lambda: self.login_count(week_ago)),
            "last_login_at": self.attempt("LoginEvents", self.last_login_at),
            "full_user_licenses": self.attempt("FndLicenses", self.full_user_licenses),
            "pso_batch_queue_jobs": self.attempt("BatchQueues", self.pso_batch_queues),
            "pso_batch_queues_configured": self.attempt("PARALLEL_BATCH_QUEUES", self.configured_batch_queues),
            "active_scheduling_datasets": self.attempt("PsoSchedulingDatasetEntities", self.active_scheduling_datasets),
        }
        jobs = self.attempt("DeferredJobStatuses", lambda: self.job_status_counts(day_ago))
        if jobs:
            health["background_job_errors"], health["background_job_warnings"] = jobs
        return {key: value for key, value in health.items() if value is not None}

    def server_alerts(self, name: str, filter_: str) -> int:
        return self.count(name, "ServerAlertLogHandling", "ServerLogs", filter_)

    def login_count(self, since: datetime) -> int:
        return self.count("logins_count", "LoginAuditEvents", "LoginEvents", "EventType eq 'LOGIN' and EventTimeUtc ge " + self.literal(since))

    def last_login_at(self) -> Optional[str]:
        """The newest login's time: one row, one field."""
        query = {"$filter": "EventType eq 'LOGIN'", "$orderby": "EventTimeUtc desc", "$top": "1", "$select": "EventTimeUtc"}
        rows = self.rows("logins_last", "LoginAuditEvents", "LoginEvents", query)
        return rfc3339(self.time(rows[0].get("EventTimeUtc"))) if rows else None

    def full_user_licenses(self) -> Optional[int]:
        """NUMBER_OF_FULL_USERS from the licence rows. The licence identifiers
        sit on the same rows and are read here and dropped, on purpose."""
        rows = self.rows("licenses", "LicenseMetricHandling", "FndLicenses", {"$select": "Parameter,Value"})
        for row in rows:
            if row.get("Parameter") == "NUMBER_OF_FULL_USERS":
                value = str(row.get("Value", "")).strip()
                return int(value) if value.isdigit() else None
        return None

    def job_status_counts(self, since: datetime) -> Optional[Tuple[int, int]]:
        """Distinct jobs with an Error or Warning status line in the last 24
        hours, from DeferredJobStatuses (job id and status type only). The
        timestamp property differs between releases; a 400 tries the next.
        No rows at all means the user cannot see status lines: unknown."""
        rows = None
        for field in JOB_STATUS_TIME_FIELDS:
            try:
                query = {"$select": "JobId,StatusType", "$filter": "%s ge %s" % (field, self.literal(since))}
                rows = self.paged("job_statuses", "BackgroundJobsHandling", "DeferredJobStatuses", query)
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
        """Batch queues whose name mentions Scheduling Optimization; no rows,
        or rows without a name field, is unknown rather than zero."""
        rows = self.rows("batch_queues", "BackgroundJobsHandling", "BatchQueues")
        if not rows:
            return None
        fields = [field for field in BATCH_QUEUE_NAME_FIELDS if field in rows[0]]
        if not fields:
            return None
        return sum(1 for row in rows if any("scheduling optimization" in str(row.get(field, "")).lower() for field in fields))

    def configured_batch_queues(self) -> Optional[int]:
        value = self.system_parameter("parallel_batch_queues", "PARALLEL_BATCH_QUEUES")
        return int(value) if value and value.isdigit() else None

    def active_scheduling_datasets(self) -> int:
        """Datasets that are enabled with a process type other than None."""
        rows = self.rows("scheduling_datasets", "PsoIntegrationDatasets", "PsoSchedulingDatasetEntities")
        if any("Active" not in row or "ProcessType" not in row for row in rows):
            raise ReadError("rows lack Active or ProcessType")
        return sum(1 for row in rows if enabled(row["Active"]) and str(row["ProcessType"]).strip().lower() not in ("", "none"))

    def messages(self, since: datetime) -> List[Dict[str, Any]]:
        """Exactly the five timing fields of ApplicationMessageSet."""
        query = {"$select": MESSAGE_FIELDS, "$filter": "CreatedDate ge " + self.literal(since), "$orderby": "CreatedDate asc"}
        return self.paged("messages", "ApplicationMessagesHandling", "ApplicationMessageSet", query)


def enabled(value: Any) -> bool:
    """IFS sends a flag as a boolean or as its own TRUE/Yes strings."""
    return value is True or (isinstance(value, str) and value.strip().lower() in ("true", "yes"))


# ----------------------------------------------------------------------
# Message timing aggregation (the server applies the same rules again)
# ----------------------------------------------------------------------

def normalise_receiver(receiver: str) -> str:
    """Cut at the first dash followed by a space, digits become #, any
    character outside the lane charset becomes _, whitespace collapses,
    60 characters: the server's rule, applied here too so two receivers
    that land on one lane never reach the server as duplicates."""
    text = re.split(r"-\s", receiver, maxsplit=1)[0]
    text = re.sub(r"\d+", "#", text)
    text = LANE_CHARSET.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip(" -\t")
    text = text[:60].rstrip(" -")
    return text or "(none)"


def normalise_queue(queue: str) -> str:
    text = LANE_CHARSET.sub("_", queue.strip())
    return (text or "(none)")[:60]


def percentile(sorted_values: List[float], fraction: float) -> float:
    """Nearest rank, three decimals."""
    if not sorted_values:
        return 0.0
    index = max(0, math.ceil(fraction * len(sorted_values)) - 1)
    return rounded(sorted_values[min(index, len(sorted_values) - 1)], 3)


def aggregate(messages: List[Dict[str, Any]], time: Callable[[Any], Optional[datetime]]) -> List[Dict[str, Any]]:
    """Hourly buckets per queue and receiver (created hour, UTC): Finished
    messages are timed (state minus created), Failed ones only counted,
    every other state ignored. Sorted by hour, queue, receiver."""
    groups: Dict[Tuple[datetime, str, str], Dict[str, Any]] = {}
    for row in messages:
        state = str(row.get("StateDb", "")).strip().lower()
        created = time(row.get("CreatedDate"))
        if state not in ("finished", "failed") or created is None:
            continue
        hour = created.replace(minute=0, second=0, microsecond=0)
        key = (hour, normalise_queue(str(row.get("Queue", ""))), normalise_receiver(str(row.get("Receiver", ""))))
        group = groups.setdefault(key, {"seconds": [], "failed": 0})
        if state == "failed":
            group["failed"] += 1
            continue
        finished = time(row.get("StateDate"))
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


# ----------------------------------------------------------------------
# PSO gateway
# ----------------------------------------------------------------------

class PsoReader:
    """A session with a PSO user, four reads, the session closed again
    whatever happened. Event messages are counted, never kept."""

    def __init__(self, source: Any, url: str, account_id: str, user_name: str):
        self.source = source
        self.base = url + GATEWAY_PATH
        self.account_id = account_id
        self.user_name = user_name
        self.token: Optional[str] = None
        self.errors: List[str] = []

    def open_session(self, password: str) -> None:
        """POST the multipart form the gateway documents; the password is
        used here once and kept nowhere."""
        boundary = "stashless-collector"
        parts = []
        for name, value in (("accountId", self.account_id), ("userName", self.user_name), ("password", password)):
            parts.append("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, name, value))
        body = ("".join(parts) + "--%s--\r\n" % boundary).encode()
        headers = {"Content-Type": "multipart/form-data; boundary=" + boundary, "Accept": "application/json"}
        try:
            answer = self.source.pso("session", "POST", self.base + "/api/v1/scheduling/session", headers, body)
        except ReadError as error:
            if error.status in (400, 401, 403):
                raise ReadError("the gateway rejected the account, user name or password", error.status)
            raise
        token = answer.get("SessionToken") if isinstance(answer, dict) else None
        if not token:
            raise ReadError("the gateway returned no session token")
        self.token = str(token)

    def close_session(self) -> None:
        if self.token is None:
            return
        try:
            self.source.pso("session_close", "DELETE", self.base + "/api/v1/scheduling/session", {"apikey": self.token})
        except ReadError:
            log("PSO session could not be closed; it expires on its own.")

    def get(self, name: str, path: str, query: Optional[Dict[str, str]] = None) -> Any:
        url = self.base + "/" + path + ("?" + urllib.parse.urlencode(query) if query else "")
        return self.source.pso(name, "GET", url, {"apikey": self.token or "", "Accept": "application/json"})

    def attempt(self, label: str, read: Callable[[], Any]) -> Any:
        return attempt(self.errors, "PSO", label, read)

    def section(self, now: datetime) -> Dict[str, Any]:
        """The PSO figures; each read costs only its own."""
        section: Dict[str, Any] = {}
        status = self.attempt("systemstatus", lambda: self.get("systemstatus", "api/v1/scheduling/systemstatus"))
        if isinstance(status, dict):
            section.update(self.status_figures(status))
        errors = self.attempt("events", lambda: self.error_events(now - timedelta(days=1)))
        if errors is not None:
            section["errors_last_24h"] = errors
        section.update(self.attempt("installations", self.installation) or {})
        section.update(self.attempt("usage", lambda: self.usage_totals(now)) or {})
        return {key: value for key, value in section.items() if value is not None}

    @staticmethod
    def status_figures(status: Dict[str, Any]) -> Dict[str, Any]:
        def at(key: str) -> Optional[str]:
            return rfc3339(parse_time(str(status[key]))) if status.get(key) else None

        successful = status.get("LastSystemTestSuccessful")
        return {
            "recent_error_count": int(status.get("RecentErrorCount") or 0),
            "recent_warning_count": int(status.get("RecentWarningCount") or 0),
            "last_status_at": at("LastStatusTime"),
            "last_system_test_at": at("LastSystemTestTime"),
            "last_system_test_successful": bool(successful) if successful is not None else None,
            "dse_capability": decimal(status.get("TotalDSECapability")),
            "live_dataset_footprint": decimal(status.get("TotalLiveDatasetFootprint")),
            "waiting_dataset_footprint": decimal(status.get("TotalWaitingDatasetFootprint")),
        }

    def error_events(self, since: datetime) -> int:
        """Error events in the last day, counted client side too in case the
        gateway ignores earliestTime. The message text is never kept."""
        query = {"minimumSeverity": "E", "earliestTime": rfc3339(since) or "", "maximumRows": "500"}
        answer = self.get("events", "api/v1/scheduling/events", query)
        events = answer.get("Events", []) if isinstance(answer, dict) else []
        return sum(1 for event in events if isinstance(event, dict) and event.get("EventTime") and parse_time(str(event["EventTime"])) >= since)

    def installation(self) -> Dict[str, Any]:
        """Version and state of the first installation; FORBIDDEN and 404
        are normal for an integration user and cost only these two."""
        answer = self.get("installations", "api/v1/system/installations")
        first = answer[0] if isinstance(answer, list) and answer else answer if isinstance(answer, dict) else None
        if not isinstance(first, dict) or not first.get("Version"):
            return {}
        figures = {"version": str(first["Version"])}
        if first.get("State"):
            figures["installation_state"] = str(first["State"])
        return figures

    def usage_totals(self, now: datetime) -> Dict[str, int]:
        """The organisation totals: the latest row per usage type with no
        dataset, rounded to whole numbers."""
        window = {
            "minimumDateTime": (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S"),
            "maximumDateTime": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"),
        }
        answer = self.get("usage", "api/v1/scheduling/usage", window)
        rows = answer.get("ScheduleDataUsages", []) if isinstance(answer, dict) else []
        latest: Dict[int, Tuple[str, int]] = {}
        for row in rows:
            if not isinstance(row, dict) or row.get("DatasetId") is not None:
                continue
            if row.get("ScheduleDataUsageType") is None or row.get("Value") is None:
                continue
            kind = int(row["ScheduleDataUsageType"])
            stamp = str(row.get("DatetimeStamp") or "")
            if kind in USAGE_TYPES and (kind not in latest or stamp >= latest[kind][0]):
                latest[kind] = (stamp, int(rounded(float(row["Value"]), 0)))
        return {USAGE_TYPES[kind]: value for kind, (_, value) in latest.items()}


# ----------------------------------------------------------------------
# Settings and secrets
# ----------------------------------------------------------------------

def key_vault_secret(vault_url: str, name: str) -> str:
    """One secret through the managed identity: the App Service and
    Automation endpoint when present, IMDS otherwise. Both go through
    OPENER (no proxy: the identity endpoint is link local). The value is
    returned to the caller and never logged."""
    if os.environ.get("IDENTITY_ENDPOINT") and os.environ.get("IDENTITY_HEADER"):
        query = urllib.parse.urlencode({"resource": IDENTITY_RESOURCE, "api-version": "2019-08-01"})
        url = "%s?%s" % (os.environ["IDENTITY_ENDPOINT"], query)
        headers = {"X-IDENTITY-HEADER": os.environ["IDENTITY_HEADER"]}
    else:
        query = urllib.parse.urlencode({"resource": IDENTITY_RESOURCE, "api-version": "2018-02-01"})
        url = "http://169.254.169.254/metadata/identity/oauth2/token?" + query
        headers = {"Metadata": "true"}
    token = http("GET", url, headers)["access_token"]
    secret_url = "%s/secrets/%s?api-version=7.4" % (require_https("KEY_VAULT_URL", vault_url), urllib.parse.quote(name))
    return http("GET", secret_url, {"Authorization": "Bearer " + token})["value"]


class Settings:
    """Flags win over environment variables; a secret may be given by name
    instead of value and is then read from Key Vault."""

    def __init__(self, args: argparse.Namespace):
        self.args = args

    def get(self, flag: str, env: str) -> Optional[str]:
        value = getattr(self.args, flag, None) or os.environ.get(env)
        return value.strip() if isinstance(value, str) and value.strip() else None

    def secret(self, flag: str, env: str) -> Optional[str]:
        """The value itself, or the Key Vault secret named by the *_NAME
        variable or *-name flag. A vault or identity failure is a ReadError
        naming the variable, never the secret name or the URL."""
        direct = self.get(flag, env)
        if direct:
            return direct
        name = self.get(flag + "_name", env + "_NAME")
        vault = self.get("key_vault_url", "KEY_VAULT_URL")
        if not (name and vault):
            return None
        try:
            return key_vault_secret(vault, name)
        except ReadError as error:
            raise ReadError("Key Vault read for %s_NAME: %s" % (env, error), error.status)
        except Exception as error:
            raise ReadError("Key Vault read for %s_NAME: %s" % (env, type(error).__name__))


def brief(errors: List[str]) -> Optional[str]:
    """The first reasons, printable ASCII, 120 characters: the heartbeat's
    last_error, shown in the sync history and never stored elsewhere."""
    if not errors:
        return None
    return re.sub(r"[^ -~]", "?", "; ".join(errors))[:120]


def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read an IFS Cloud environment and report telemetry to Stashless.")
    parser.add_argument("--endpoint", help="STASHLESS_ENDPOINT")
    parser.add_argument("--token", help="STASHLESS_TOKEN; for a one-off test only, it shows in the process list")
    parser.add_argument("--token-name", dest="token_name", help="STASHLESS_TOKEN_NAME, the secret's name in Key Vault")
    parser.add_argument("--ifs-url", dest="ifs_url", help="IFS_URL")
    parser.add_argument("--ifs-client-id", dest="ifs_client_id", help="IFS_CLIENT_ID")
    parser.add_argument("--ifs-client-secret", dest="ifs_client_secret", help="IFS_CLIENT_SECRET; for a one-off test only")
    parser.add_argument("--ifs-client-secret-name", dest="ifs_client_secret_name", help="IFS_CLIENT_SECRET_NAME")
    parser.add_argument("--ifs-realm", dest="ifs_realm", help="IFS_REALM")
    parser.add_argument("--ifs-server-timezone", dest="ifs_server_timezone", help="IFS_SERVER_TIMEZONE")
    parser.add_argument("--pso-url", dest="pso_url", help="PSO_URL")
    parser.add_argument("--pso-account-id", dest="pso_account_id", help="PSO_ACCOUNT_ID")
    parser.add_argument("--pso-user-name", dest="pso_user_name", help="PSO_USER_NAME")
    parser.add_argument("--pso-password", dest="pso_password", help="PSO_PASSWORD; for a one-off test only")
    parser.add_argument("--pso-password-name", dest="pso_password_name", help="PSO_PASSWORD_NAME")
    parser.add_argument("--key-vault-url", dest="key_vault_url", help="KEY_VAULT_URL")
    parser.add_argument("--implementation", choices=["python", "docker"], default=None, help="what the heartbeat says reported")
    parser.add_argument("--fixtures", help="Directory of canned answers; no network is used")
    parser.add_argument("--out", help="Write the payload to this file instead of posting")
    parser.add_argument("--print", action="store_true", help="Print the payload instead of posting")
    return parser.parse_args(argv)


# ----------------------------------------------------------------------
# The run: collect from IFS, collect from PSO, deliver
# ----------------------------------------------------------------------

def collect_ifs(settings: Settings, source: Any, now: datetime, payload: Dict[str, Any]) -> bool:
    """Signs in first, so a wrong secret or an unreachable IAM is one clear
    error; after that each read only costs its own figure. Returns whether
    IFS could be read at all; the heartbeat is written either way."""
    url = settings.get("ifs_url", "IFS_URL") or ("" if source.is_live() else "https://fixture.invalid")
    client_id = settings.get("ifs_client_id", "IFS_CLIENT_ID") or ""
    ifs = IfsReader(source, url, client_id, settings.get("ifs_realm", "IFS_REALM"), settings.get("ifs_server_timezone", "IFS_SERVER_TIMEZONE"))
    try:
        ifs.base = require_https("IFS_URL", url)
        ifs.sign_in(settings.secret("ifs_client_secret", "IFS_CLIENT_SECRET") or "")
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
        return True
    except ReadError as error:
        payload["sources"]["ifs"] = {"last_read_at": None, "last_error": brief(["sign-in: %s" % error] + ifs.errors)}
        log("IFS could not be read: %s" % error)
    except Exception as error:
        # The heartbeat still goes out, naming the kind of failure only.
        payload["sources"]["ifs"] = {"last_read_at": None, "last_error": brief(["sign-in: unexpected failure (%s)" % type(error).__name__] + ifs.errors)}
        log("IFS could not be read: unexpected failure (%s)." % type(error).__name__)
    return False


def collect_pso(settings: Settings, source: Any, now: datetime, payload: Dict[str, Any]) -> None:
    """The PSO section, when PSO is configured (or the fixtures carry it)."""
    url = settings.get("pso_url", "PSO_URL") or ("" if source.is_live() else "https://fixture.invalid")
    if not url or (not source.is_live() and source.pso_data is None):
        return
    account_id = settings.get("pso_account_id", "PSO_ACCOUNT_ID") or ""
    user_name = settings.get("pso_user_name", "PSO_USER_NAME") or ""
    pso = PsoReader(source, url, account_id, user_name)
    try:
        pso.base = require_https("PSO_URL", url) + GATEWAY_PATH
        pso.open_session(settings.secret("pso_password", "PSO_PASSWORD") or "")
        try:
            payload["pso"] = pso.section(now)
        finally:
            pso.close_session()
        payload["sources"]["pso"] = {"last_read_at": rfc3339(source.now()), "last_error": brief(pso.errors)}
        log("PSO read: %d figures." % len(payload["pso"]))
    except ReadError as error:
        payload["sources"]["pso"] = {"last_read_at": None, "last_error": brief(["session: %s" % error] + pso.errors)}
        log("PSO could not be read: %s" % error)
    except Exception as error:
        payload["sources"]["pso"] = {"last_read_at": None, "last_error": brief(["session: unexpected failure (%s)" % type(error).__name__] + pso.errors)}
        log("PSO could not be read: unexpected failure (%s)." % type(error).__name__)


def deliver(settings: Settings, payload: Dict[str, Any], ifs_ok: bool) -> int:
    """Writes, prints or posts the report. The exit code says whether IFS
    was read and the report accepted; the heartbeat goes out either way."""
    body = json.dumps(payload, indent=2)
    if settings.args.out:
        with open(settings.args.out, "w", encoding="utf-8") as handle:
            handle.write(body + "\n")
        log("Payload written to %s." % settings.args.out)
        return 0 if ifs_ok else 1
    if settings.args.print:
        print(body)
        return 0 if ifs_ok else 1

    endpoint = settings.get("endpoint", "STASHLESS_ENDPOINT")
    try:
        token = settings.secret("token", "STASHLESS_TOKEN")
    except ReadError as error:
        log("The collector token could not be read: %s" % error)
        return 1
    if not endpoint or not token:
        log("STASHLESS_ENDPOINT and STASHLESS_TOKEN (or STASHLESS_TOKEN_NAME with KEY_VAULT_URL) are needed to post.")
        return 1
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "stashless-collector-python/" + VERSION,
    }
    try:
        answer = http("POST", require_https("STASHLESS_ENDPOINT", endpoint), headers, body.encode())
    except ReadError as error:
        log("Stashless refused the report: %s." % error)
        return 1
    # Stashless's own answer is the one response ever logged: it summarises what was accepted, never a value of yours.
    log("Stashless answered 202: %s" % json.dumps(answer)[:300])
    return 0 if ifs_ok else 1


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    settings = Settings(args)
    implementation = args.implementation or os.environ.get("STASHLESS_COLLECTOR_IMPLEMENTATION", "python").strip().lower()
    if implementation not in ("python", "docker"):
        log("STASHLESS_COLLECTOR_IMPLEMENTATION must be python or docker; reporting as python.")
        implementation = "python"

    source: Any = FixtureSource(args.fixtures) if args.fixtures else LiveSource()
    now = source.now()
    payload: Dict[str, Any] = {
        "schema_version": 1,
        "collected_at": rfc3339(now),
        "collector": {"implementation": implementation, "version": VERSION},
        "sources": {},
    }
    ifs_ok = collect_ifs(settings, source, now, payload)
    collect_pso(settings, source, now, payload)
    return deliver(settings, payload, ifs_ok)


if __name__ == "__main__":
    sys.exit(main())
