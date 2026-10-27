<#
.SYNOPSIS
Stashless reference collector, PowerShell edition (plan 16, phase 4).

.DESCRIPTION
Runs inside the customer's estate (an Azure Automation runbook, a scheduled
task, a Function) and reads one IFS Cloud environment, and optionally the
PSO gateway paired with it, with credentials the customer holds. It shapes
the version 1 telemetry payload and posts it to Stashless with a collector
token. Counts, timings and version strings only: it selects no message
subject, text or body, drops the licence identifiers it cannot avoid
reading, and never logs a value it read. Every judgement stays on the
server.

The contract is docs/telemetry/aggregation.md; the schema is
docs/telemetry/schema-v1.json (served at /telemetry/schema/v1). The
Python edition produces the same payload from the same fixtures, which
the conformance test checks.

.PARAMETER Endpoint
https://stashless.example.com/api/telemetry/v1 (or STASHLESS_ENDPOINT).

.PARAMETER Token
The collector token (or STASHLESS_TOKEN; or STASHLESS_TOKEN_NAME in Key Vault).

.PARAMETER IfsUrl
https://customer.ifs.cloud (or IFS_URL).

.PARAMETER IfsClientId
The read-only IAM client with the STASHLESS_READ permission set (or IFS_CLIENT_ID).

.PARAMETER IfsClientSecret
Its secret (or IFS_CLIENT_SECRET; or IFS_CLIENT_SECRET_NAME in Key Vault).

.PARAMETER IfsRealm
Optional; discovered from the login challenge when blank (or IFS_REALM).

.PARAMETER IfsServerTimeZone
Optional IANA zone when IFS labels server-local time as UTC (or IFS_SERVER_TIMEZONE).

.PARAMETER PsoUrl
Optional; the PSO host, the gateway path is appended (or PSO_URL), with
PsoAccountId, PsoUserName and PsoPassword (or PSO_PASSWORD_NAME in Key Vault).

.PARAMETER KeyVaultUrl
Optional; secrets named *_NAME are read with the managed identity (or KEY_VAULT_URL).

.PARAMETER IfsClientSecretName
The Key Vault secret holding the IAM secret (or IFS_CLIENT_SECRET_NAME); likewise
PsoPasswordName (PSO_PASSWORD_NAME) and TokenName (STASHLESS_TOKEN_NAME). An Azure
Automation schedule passes these as runbook parameters, since Automation variables
are not visible to the script.

.PARAMETER Fixtures
A directory of canned answers instead of IFS and PSO, for the conformance test.

.PARAMETER Out
Write the payload to this file instead of posting.

.PARAMETER Print
Print the payload instead of posting.

.NOTES
Exit code 0 when the report was posted and accepted (or written); 1 when
IFS could not be signed in to or the post was refused. PowerShell 7.
#>
[CmdletBinding()]
param(
    [string] $Endpoint,
    [string] $Token,
    [string] $IfsUrl,
    [string] $IfsClientId,
    [string] $IfsClientSecret,
    [string] $IfsRealm,
    [string] $IfsServerTimeZone,
    [string] $PsoUrl,
    [string] $PsoAccountId,
    [string] $PsoUserName,
    [string] $PsoPassword,
    [string] $KeyVaultUrl,
    [string] $IfsClientSecretName,
    [string] $PsoPasswordName,
    [string] $TokenName,
    [string] $Fixtures,
    [string] $Out,
    [switch] $Print
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:Version = '1.0.0'
$script:TimeoutSeconds = 15
$script:Page = 5000
$script:Cap = 50000
$script:MessageFields = 'Queue,StateDb,Receiver,CreatedDate,StateDate'
$script:JobStatusTimeFields = @('Timestamp', 'StatusTimestamp', 'StatusDate', 'CreatedDate', 'DateCreated')
$script:BatchQueueNameFields = @('Description', 'Name', 'QueueName', 'QueueDescription')
$script:GatewayPath = '/IFSSchedulingRESTfulGateway'
$script:UsageTypes = @{ 0 = 'resources_count'; 1 = 'activities_count'; 2 = 'schedule_window_days'; 3 = 'appointment_window_days'; 4 = 'datasets_count' }

function Write-Log([string] $Message) {
    # Progress to stderr: names reads and counts, never values read.
    [Console]::Error.WriteLine($Message)
}

function Get-Setting([string] $Value, [string] $EnvName) {
    if (-not [string]::IsNullOrWhiteSpace($Value)) { return $Value.Trim() }
    $fromEnv = [Environment]::GetEnvironmentVariable($EnvName)
    if (-not [string]::IsNullOrWhiteSpace($fromEnv)) { return $fromEnv.Trim() }
    return $null
}

# ----------------------------------------------------------------------
# A read that did not succeed: 401/403 is a refusal, anything else unreachable.
# ----------------------------------------------------------------------
class ReadError : System.Exception {
    [int] $Status
    ReadError([string] $message, [int] $status) : base($message) { $this.Status = $status }
}

function New-ReadError([string] $Message, [int] $Status = 0) { return [ReadError]::new($Message, $Status) }

# ----------------------------------------------------------------------
# Time
# ----------------------------------------------------------------------

function ConvertTo-UtcTime([string] $Value, [bool] $ServerLocal = $false, [string] $Zone = $null) {
    # RFC 3339 (Z or offset, any fraction) to a UTC DateTime. With a server
    # zone, the offset IFS wrote is ignored and the wall clock is read in
    # that zone, since IFS labels local time as UTC.
    $text = $Value.Trim()
    if ($ServerLocal -and $Zone) {
        $text = [regex]::Replace($text, '(Z|[+-]\d{2}:\d{2})$', '')
        $wall = [DateTime]::Parse($text, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AdjustToUniversal -bor [Globalization.DateTimeStyles]::AssumeUniversal)
        $wall = [DateTime]::SpecifyKind($wall, [DateTimeKind]::Unspecified)
        return [TimeZoneInfo]::ConvertTimeToUtc($wall, [TimeZoneInfo]::FindSystemTimeZoneById($Zone))
    }
    return [DateTimeOffset]::Parse($text, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal).UtcDateTime
}

function Get-FilterLiteral([DateTime] $At, [bool] $ServerLocal, [string] $Zone) {
    # A moment as IFS compares it: wall clock with a Z for a server-local property.
    if ($ServerLocal -and $Zone) {
        $local = [TimeZoneInfo]::ConvertTimeFromUtc($At, [TimeZoneInfo]::FindSystemTimeZoneById($Zone))
        return $local.ToString('yyyy-MM-ddTHH:mm:ss', [Globalization.CultureInfo]::InvariantCulture) + 'Z'
    }
    return $At.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ', [Globalization.CultureInfo]::InvariantCulture)
}

function Format-Rfc3339($At) {
    if ($null -eq $At) { return $null }
    return ([DateTime] $At).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ', [Globalization.CultureInfo]::InvariantCulture)
}

function Get-Rounded([double] $Value, [int] $Decimals) {
    return [math]::Round($Value, $Decimals, [MidpointRounding]::AwayFromZero)
}

# ----------------------------------------------------------------------
# Sources: live HTTPS or canned fixtures, the same shape
# ----------------------------------------------------------------------

$script:FixtureData = $null

function Initialize-Fixtures([string] $Directory) {
    $load = {
        param($File)
        $path = Join-Path $Directory $File
        if (Test-Path $path) { return Get-Content -Raw -Path $path | ConvertFrom-Json -AsHashtable -DateKind String -Depth 20 }
        return $null
    }
    $clock = & $load 'clock.json'
    $script:FixtureData = @{
        ifs = (& $load 'ifs-cloud.json')
        pso = (& $load 'pso.json')
        now = (ConvertTo-UtcTime ($(if ($clock -and $clock.now) { $clock.now } else { '2026-01-01T00:00:00Z' })))
    }
    if ($null -eq $script:FixtureData.ifs) { $script:FixtureData.ifs = @{} }
}

function Get-Now {
    if ($script:FixtureData) { return $script:FixtureData.now }
    return [DateTime]::UtcNow
}

function Invoke-Http([string] $Method, [string] $Url, [hashtable] $Headers, $Body = $null, [string] $ContentType = $null, [hashtable] $Form = $null) {
    # One request, no redirects, a fixed timeout; the parsed body, or the
    # text when it is not JSON (an OData $count answers a bare integer).
    $arguments = @{ Method = $Method; Uri = $Url; Headers = $Headers; TimeoutSec = $script:TimeoutSeconds; MaximumRedirection = 0; SkipHttpErrorCheck = $true; ErrorAction = 'Stop' }
    if ($null -ne $Form) { $arguments.Form = $Form }
    elseif ($null -ne $Body) { $arguments.Body = $Body; $arguments.ContentType = $ContentType }
    try {
        $response = Invoke-WebRequest @arguments
    } catch {
        throw (New-ReadError "not reachable ($($_.Exception.GetType().Name))")
    }
    if ($response.StatusCode -ge 400) { throw (New-ReadError "HTTP $($response.StatusCode)" $response.StatusCode) }
    $content = [string] $response.Content
    if ([string]::IsNullOrWhiteSpace($content)) { return $null }
    try { return $content | ConvertFrom-Json -AsHashtable -DateKind String -Depth 20 } catch { return $content.Trim() }
}

function Get-Rows($Answer) {
    # The "value" rows of an OData answer; an empty body or a different shape is no rows.
    if ($Answer -is [hashtable] -and $Answer.ContainsKey('value') -and $null -ne $Answer.value) { return @($Answer.value) }
    return @()
}

function Read-Ifs([string] $Name, [string] $Url, [hashtable] $Headers) {
    if ($script:FixtureData) {
        if (-not $script:FixtureData.ifs.ContainsKey($Name)) { throw (New-ReadError 'HTTP 403' 403) }
        return $script:FixtureData.ifs[$Name]
    }
    return Invoke-Http 'GET' $Url $Headers
}

function Read-Pso([string] $Name, [string] $Method, [string] $Url, [hashtable] $Headers, [hashtable] $Form = $null) {
    if ($script:FixtureData) {
        if ($null -eq $script:FixtureData.pso -or -not $script:FixtureData.pso.ContainsKey($Name)) { throw (New-ReadError 'HTTP 403' 403) }
        return $script:FixtureData.pso[$Name]
    }
    return Invoke-Http $Method $Url $Headers -Form $Form
}

# ----------------------------------------------------------------------
# IFS Cloud: the reads a Stashless pull sync makes, and nothing more
# ----------------------------------------------------------------------

$script:Ifs = @{ Base = ''; ClientId = ''; Secret = ''; Realm = $null; Zone = $null; Token = $null; Errors = [System.Collections.Generic.List[string]]::new() }

function Get-IfsRealm {
    # The realm named by the environment's own login challenge.
    try {
        $response = Invoke-WebRequest -Uri "$($script:Ifs.Base)/main/ifsapplications/web/" -TimeoutSec $script:TimeoutSeconds -SkipHttpErrorCheck -ErrorAction Stop
    } catch {
        throw (New-ReadError "could not reach the environment to discover its realm ($($_.Exception.GetType().Name))")
    }
    $challenge = [string] $response.Headers['WWW-Authenticate']
    $match = [regex]::Match($challenge, '/auth/realms/([^/"\s]+)/?')
    if (-not $match.Success) { throw (New-ReadError 'the environment presented no IFS IAM login challenge') }
    return $match.Groups[1].Value
}

function Connect-Ifs {
    # Client credentials against the environment's own IAM realm.
    if ($script:FixtureData) {
        $answer = Read-Ifs 'token' '' @{}
        $script:Ifs.Token = [string] $answer.access_token
        return
    }
    $realm = if ($script:Ifs.Realm) { $script:Ifs.Realm } else { Get-IfsRealm }
    $url = "$($script:Ifs.Base)/auth/realms/$realm/protocol/openid-connect/token"
    $body = 'grant_type=client_credentials&client_id=' + [Uri]::EscapeDataString($script:Ifs.ClientId) + '&client_secret=' + [Uri]::EscapeDataString($script:Ifs.Secret) + '&scope=openid'
    try {
        $answer = Invoke-Http 'POST' $url @{ Accept = 'application/json' } $body 'application/x-www-form-urlencoded'
    } catch [ReadError] {
        if ($_.Exception.Status -in 400, 401, 403) { throw (New-ReadError 'IAM rejected the client credentials' $_.Exception.Status) }
        throw (New-ReadError "IAM $($_.Exception.Message)")
    }
    if ($null -eq $answer -or -not $answer.access_token) { throw (New-ReadError 'IAM returned no token') }
    $script:Ifs.Token = [string] $answer.access_token
}

function Read-Projection([string] $Name, [string] $Projection, [string] $EntitySet, [hashtable] $Query = $null, [string] $Accept = 'application/json') {
    # The OData option names stay raw ($select, never %24select); only the
    # values are percent-encoded, which is what IFS Cloud accepts.
    $url = "$($script:Ifs.Base)/main/ifsapplications/projection/v1/$Projection.svc/$EntitySet"
    if ($Query -and $Query.Count -gt 0) {
        $parts = foreach ($option in $Query.Keys) {
            $encoded = [Uri]::EscapeDataString([string] $Query[$option]).Replace('%27', "'").Replace('%28', '(').Replace('%29', ')').Replace('%2C', ',').Replace('%3A', ':')
            "$option=$encoded"
        }
        $url += '?' + ($parts -join '&')
    }
    return Read-Ifs $Name $url @{ Authorization = "Bearer $($script:Ifs.Token)"; Accept = $Accept }
}

function Read-Count([string] $Name, [string] $Projection, [string] $EntitySet, [string] $Filter) {
    $answer = Read-Projection $Name $Projection "$EntitySet/`$count" @{ '$filter' = $Filter } 'text/plain'
    return [int] ([string] $answer).Trim()
}

function Read-Pages([string] $Name, [string] $Projection, [string] $EntitySet, [hashtable] $Query) {
    # $top/$skip pages to the cap; the fixtures answer one page.
    $rows = [System.Collections.Generic.List[object]]::new()
    $skip = 0
    while ($rows.Count -lt $script:Cap) {
        $pageQuery = $Query.Clone(); $pageQuery['$top'] = [string] $script:Page; $pageQuery['$skip'] = [string] $skip
        $answer = Read-Projection $Name $Projection $EntitySet $pageQuery
        $page = Get-Rows $answer
        foreach ($row in $page) { if ($row -is [hashtable]) { $rows.Add($row) } }
        if ($page.Count -lt $script:Page -or $script:FixtureData) { return $rows }
        $skip += $script:Page
    }
    Write-Log "IFS $EntitySet stopped at the $($script:Cap) row cap."
    return $rows
}

function Get-IfsLiteral([DateTime] $At) { return Get-FilterLiteral $At ([bool] $script:Ifs.Zone) $script:Ifs.Zone }

function Get-IfsTime($Value) {
    if ($null -eq $Value -or [string]::IsNullOrWhiteSpace([string] $Value)) { return $null }
    return ConvertTo-UtcTime ([string] $Value) ([bool] $script:Ifs.Zone) $script:Ifs.Zone
}

function Invoke-IfsAttempt([string] $Label, [scriptblock] $Read) {
    # A read whose failure costs only its own figure; the reason is kept for
    # the heartbeat (status and name, never content).
    try { return (& $Read) } catch [ReadError] {
        $script:Ifs.Errors.Add("${Label}: $($_.Exception.Message)")
        Write-Log "IFS $Label not read ($($_.Exception.Message))."
        return $null
    } catch {
        # An unexpected shape costs this figure only, never the report.
        $script:Ifs.Errors.Add("${Label}: unexpected answer ($($_.Exception.GetType().Name))")
        Write-Log "IFS $Label not read (unexpected answer, $($_.Exception.GetType().Name))."
        return $null
    }
}

function Get-SystemParameter([string] $Name, [string] $Key) {
    $answer = Read-Projection $Name 'FrameworkServices' "GetSystemParameter(ParameterKey='$Key')"
    $value = if ($answer -is [hashtable] -and $answer.ContainsKey('value')) { $answer.value } else { $null }
    if ($value -is [string] -and $value.Trim().StartsWith('{')) { $value = ($value | ConvertFrom-Json -AsHashtable -DateKind String).Value }
    if ($value -is [string] -and -not [string]::IsNullOrWhiteSpace($value)) { return $value.Trim() }
    return $null
}

function Get-IfsRelease {
    # FNDBAS from ModuleSet (never the max module version) plus the two
    # FrameworkServices system parameters the About dialog reads.
    $modules = Invoke-IfsAttempt 'ModuleSet' { Get-Rows (Read-Projection 'modules' 'InstalledComponentsHandling' 'ModuleSet') }
    $platform = $null
    foreach ($row in @($modules)) { if ($row -is [hashtable] -and $row.Module -eq 'FNDBAS') { $platform = $row; break } }
    if ($null -eq $platform -or [string]::IsNullOrWhiteSpace([string] $platform.Version)) { return $null }
    $release = [ordered]@{ base = ([string] $platform.Version).Trim() }
    $serviceUpdate = Invoke-IfsAttempt 'SERVICE_UPDATE' { Get-SystemParameter 'service_update' 'SERVICE_UPDATE' }
    $frameworkUpdate = Invoke-IfsAttempt 'MT_SERVICE_UPDATE' { Get-SystemParameter 'framework_update' 'MT_SERVICE_UPDATE' }
    if ($serviceUpdate) { $release.service_update = $serviceUpdate }
    if ($frameworkUpdate) { $release.framework_update = $frameworkUpdate }
    if ($platform.ContainsKey('RegDate') -and $platform.RegDate) { $release.base_registered_at = Format-Rfc3339 (ConvertTo-UtcTime ([string] $platform.RegDate)) }
    return $release
}

function Get-JobStatusCounts([DateTime] $Since) {
    # Distinct jobs with an Error or Warning status line in the last 24 hours
    # (job id and status type only). The timestamp property differs between
    # releases; a 400 tries the next.
    $rows = $null
    foreach ($field in $script:JobStatusTimeFields) {
        try {
            $rows = Read-Pages 'job_statuses' 'BackgroundJobsHandling' 'DeferredJobStatuses' @{ '$select' = 'JobId,StatusType'; '$filter' = "$field ge $(Get-IfsLiteral $Since)" }
            break
        } catch [ReadError] {
            if ($_.Exception.Status -ne 400) { throw }
        }
    }
    if ($null -eq $rows) { throw (New-ReadError 'no known timestamp property' 400) }
    if ($rows.Count -eq 0) { return $null }
    $jobs = @{ error = [System.Collections.Generic.HashSet[string]]::new(); warning = [System.Collections.Generic.HashSet[string]]::new() }
    foreach ($row in $rows) {
        $kind = ([string] $row.StatusType).Trim().ToLowerInvariant()
        if ($jobs.ContainsKey($kind) -and $row.ContainsKey('JobId') -and $null -ne $row.JobId) { [void] $jobs[$kind].Add([string] $row.JobId) }
    }
    return @($jobs.error.Count, $jobs.warning.Count)
}

function Test-Enabled($Value) {
    return ($Value -is [bool] -and $Value) -or ($Value -is [string] -and $Value.Trim().ToLowerInvariant() -in 'true', 'yes')
}

function Get-PsoBatchQueues {
    $rows = @(foreach ($row in (Get-Rows (Read-Projection 'batch_queues' 'BackgroundJobsHandling' 'BatchQueues'))) { if ($row -is [hashtable]) { $row } })
    if ($rows.Count -eq 0) { return $null }
    $fields = @($script:BatchQueueNameFields | Where-Object { $rows[0].ContainsKey($_) })
    if ($fields.Count -eq 0) { return $null }
    $count = 0
    foreach ($row in $rows) {
        foreach ($field in $fields) {
            if (([string] $row[$field]).ToLowerInvariant().Contains('scheduling optimization')) { $count++; break }
        }
    }
    return $count
}

function Get-ActiveSchedulingDatasets {
    $rows = @(foreach ($row in (Get-Rows (Read-Projection 'scheduling_datasets' 'PsoIntegrationDatasets' 'PsoSchedulingDatasetEntities'))) { if ($row -is [hashtable]) { $row } })
    foreach ($row in $rows) { if (-not ($row.ContainsKey('Active') -and $row.ContainsKey('ProcessType'))) { throw (New-ReadError 'rows lack Active or ProcessType') } }
    $count = 0
    foreach ($row in $rows) {
        if ((Test-Enabled $row.Active) -and (([string] $row.ProcessType).Trim().ToLowerInvariant() -notin '', 'none')) { $count++ }
    }
    return $count
}

function Get-IfsHealth([DateTime] $Now) {
    # Counts only. The licence identifiers are on the same rows as the user
    # count and are dropped here, on purpose.
    $dayAgo = $Now.AddDays(-1); $weekAgo = $Now.AddDays(-7)
    $health = [ordered]@{}
    $health.server_alerts_last_24h = Invoke-IfsAttempt 'ServerLogs' { Read-Count 'server_alerts_last_24h' 'ServerAlertLogHandling' 'ServerLogs' "DateCreated ge $(Get-IfsLiteral $dayAgo)" }
    $health.server_alerts_unchecked = Invoke-IfsAttempt 'ServerLogs' { Read-Count 'server_alerts_unchecked' 'ServerAlertLogHandling' 'ServerLogs' 'Checked eq false' }
    $jobs = Invoke-IfsAttempt 'DeferredJobStatuses' { Get-JobStatusCounts $dayAgo }
    if ($null -ne $jobs) { $health.background_job_errors = [int] $jobs[0]; $health.background_job_warnings = [int] $jobs[1] }
    $health.logins_last_7_days = Invoke-IfsAttempt 'LoginEvents' { Read-Count 'logins_count' 'LoginAuditEvents' 'LoginEvents' "EventType eq 'LOGIN' and EventTimeUtc ge $(Get-IfsLiteral $weekAgo)" }
    $lastLogin = Invoke-IfsAttempt 'LoginEvents' {
        $answer = Read-Projection 'logins_last' 'LoginAuditEvents' 'LoginEvents' @{ '$filter' = "EventType eq 'LOGIN'"; '$orderby' = 'EventTimeUtc desc'; '$top' = '1'; '$select' = 'EventTimeUtc' }
        $first = Get-Rows $answer | Select-Object -First 1
        if ($first -is [hashtable] -and $first.ContainsKey('EventTimeUtc')) { $first.EventTimeUtc } else { $null }
    }
    $health.last_login_at = if ($lastLogin) { Format-Rfc3339 (Get-IfsTime $lastLogin) } else { $null }
    $licenses = Invoke-IfsAttempt 'FndLicenses' { Get-Rows (Read-Projection 'licenses' 'LicenseMetricHandling' 'FndLicenses' @{ '$select' = 'Parameter,Value' }) }
    $users = $null
    foreach ($row in @($licenses)) { if ($row -is [hashtable] -and $row.Parameter -eq 'NUMBER_OF_FULL_USERS') { $users = [string] $row.Value } }
    $health.full_user_licenses = if ($users -and $users.Trim() -match '^\d+$') { [int] $users.Trim() } else { $null }
    $health.pso_batch_queue_jobs = Invoke-IfsAttempt 'BatchQueues' { Get-PsoBatchQueues }
    $configured = Invoke-IfsAttempt 'PARALLEL_BATCH_QUEUES' { Get-SystemParameter 'parallel_batch_queues' 'PARALLEL_BATCH_QUEUES' }
    $health.pso_batch_queues_configured = if ($configured -and $configured -match '^\d+$') { [int] $configured } else { $null }
    $health.active_scheduling_datasets = Invoke-IfsAttempt 'PsoSchedulingDatasetEntities' { Get-ActiveSchedulingDatasets }
    $result = [ordered]@{}
    foreach ($key in $health.Keys) { if ($null -ne $health[$key]) { $result[$key] = $health[$key] } }
    return $result
}

function Get-IfsMessages([DateTime] $Since) {
    # Exactly the five timing fields of ApplicationMessageSet.
    return Read-Pages 'messages' 'ApplicationMessagesHandling' 'ApplicationMessageSet' @{ '$select' = $script:MessageFields; '$filter' = "CreatedDate ge $(Get-IfsLiteral $Since)"; '$orderby' = 'CreatedDate asc' }
}

# ----------------------------------------------------------------------
# Message timing aggregation (the server applies the same rules again)
# ----------------------------------------------------------------------

function ConvertTo-ReceiverName([string] $Receiver) {
    # Cut at the first dash followed by a space, digits become #, whitespace
    # collapses, 60 characters: the server's rule, applied here too so two
    # receivers that land on one lane never reach the server as duplicates.
    # A character the server's lane charset refuses becomes an underscore,
    # since one odd receiver must not cost the whole report.
    $text = [regex]::Split($Receiver, '-\s', 'None', [TimeSpan]::FromSeconds(1))[0]
    $text = [regex]::Replace($text, '\d+', '#')
    $text = [regex]::Replace($text, '[^A-Za-z0-9_#.()/ -]', '_')
    $text = [regex]::Replace($text, '\s+', ' ').Trim(' ', '-', "`t")
    if ($text.Length -gt 60) { $text = $text.Substring(0, 60) }
    $text = $text.TrimEnd(' ', '-')
    if ($text -eq '') { return '(none)' }
    return $text
}

function ConvertTo-QueueName([string] $Queue) {
    $text = [regex]::Replace($Queue.Trim(), '[^A-Za-z0-9_#.()/ -]', '_')
    if ($text -eq '') { $text = '(none)' }
    if ($text.Length -gt 60) { $text = $text.Substring(0, 60) }
    return $text
}

function Get-Percentile([double[]] $Sorted, [double] $Fraction) {
    # Nearest rank.
    if ($Sorted.Count -eq 0) { return 0.0 }
    $index = [math]::Max(0, [int] [math]::Ceiling($Fraction * $Sorted.Count) - 1)
    return Get-Rounded $Sorted[[math]::Min($index, $Sorted.Count - 1)] 3
}

function Get-MessageBuckets($Messages) {
    # Hourly buckets per queue and receiver (created hour, UTC): Finished
    # messages are timed (state minus created), Failed ones only counted,
    # every other state ignored.
    $groups = @{}
    foreach ($row in @($Messages)) {
        $state = ([string] $row.StateDb).Trim().ToLowerInvariant()
        $created = Get-IfsTime $row.CreatedDate
        if ($state -notin 'finished', 'failed' -or $null -eq $created) { continue }
        $hour = [DateTime]::new($created.Year, $created.Month, $created.Day, $created.Hour, 0, 0, [DateTimeKind]::Utc)
        $queue = ConvertTo-QueueName ([string] $row.Queue)
        $receiver = ConvertTo-ReceiverName ([string] $row.Receiver)
        $key = (Format-Rfc3339 $hour) + "`t" + $queue + "`t" + $receiver
        if (-not $groups.ContainsKey($key)) { $groups[$key] = @{ hour = $hour; queue = $queue; receiver = $receiver; seconds = [System.Collections.Generic.List[double]]::new(); failed = 0 } }
        if ($state -eq 'failed') { $groups[$key].failed++ }
        elseif ($row.ContainsKey('StateDate') -and $row.StateDate) {
            $finished = Get-IfsTime $row.StateDate
            if ($null -ne $finished) { $groups[$key].seconds.Add([math]::Max(0.0, ($finished - $created).TotalSeconds)) }
        }
    }
    $keys = [System.Collections.Generic.List[string]]::new([string[]] @($groups.Keys))
    $keys.Sort([System.StringComparer]::Ordinal)
    $buckets = foreach ($key in $keys) {
        $group = $groups[$key]
        $seconds = [double[]] @($group.seconds | Sort-Object)
        $sum = 0.0; foreach ($value in $seconds) { $sum += $value }
        [ordered]@{
            bucket_start   = Format-Rfc3339 $group.hour
            queue          = $group.queue
            receiver       = $group.receiver
            finished_count = $seconds.Count
            failed_count   = $group.failed
            avg_seconds    = $(if ($seconds.Count -gt 0) { Get-Rounded ($sum / $seconds.Count) 3 } else { 0.0 })
            p95_seconds    = Get-Percentile $seconds 0.95
            max_seconds    = $(if ($seconds.Count -gt 0) { Get-Rounded $seconds[-1] 3 } else { 0.0 })
        }
    }
    return @($buckets)
}

# ----------------------------------------------------------------------
# PSO gateway: a session, three reads, the session closed again
# ----------------------------------------------------------------------

$script:Pso = @{ Base = ''; AccountId = ''; UserName = ''; Password = ''; Token = $null; Errors = [System.Collections.Generic.List[string]]::new() }

function Connect-Pso {
    try {
        $answer = Read-Pso 'session' 'POST' "$($script:Pso.Base)/api/v1/scheduling/session" @{ Accept = 'application/json' } @{ accountId = $script:Pso.AccountId; userName = $script:Pso.UserName; password = $script:Pso.Password }
    } catch [ReadError] {
        if ($_.Exception.Status -in 400, 401, 403) { throw (New-ReadError 'the gateway rejected the account, user name or password' $_.Exception.Status) }
        throw
    }
    if ($null -eq $answer -or -not $answer.SessionToken) { throw (New-ReadError 'the gateway returned no session token') }
    $script:Pso.Token = [string] $answer.SessionToken
}

function Disconnect-Pso {
    if ($null -eq $script:Pso.Token -or $script:FixtureData) { return }
    try { [void] (Read-Pso 'session_close' 'DELETE' "$($script:Pso.Base)/api/v1/scheduling/session" @{ apikey = $script:Pso.Token }) } catch { Write-Log 'PSO session could not be closed; it expires on its own.' }
}

function Read-Gateway([string] $Name, [string] $Path, [hashtable] $Query = $null) {
    $url = "$($script:Pso.Base)/$Path"
    if ($Query) { $url += '?' + (($Query.Keys | ForEach-Object { "$_=" + [Uri]::EscapeDataString([string] $Query[$_]) }) -join '&') }
    return Read-Pso $Name 'GET' $url @{ apikey = [string] $script:Pso.Token; Accept = 'application/json' }
}

function Invoke-PsoAttempt([string] $Label, [scriptblock] $Read) {
    try { return (& $Read) } catch [ReadError] {
        $script:Pso.Errors.Add("${Label}: $($_.Exception.Message)")
        Write-Log "PSO $Label not read ($($_.Exception.Message))."
        return $null
    } catch {
        $script:Pso.Errors.Add("${Label}: unexpected answer ($($_.Exception.GetType().Name))")
        Write-Log "PSO $Label not read (unexpected answer, $($_.Exception.GetType().Name))."
        return $null
    }
}

function Get-Decimal($Value) {
    if ($null -eq $Value) { return $null }
    try { return Get-Rounded ([double] $Value) 2 } catch { return $null }
}

function Get-PsoSection([DateTime] $Now) {
    $since = $Now.AddDays(-1)
    $status = Invoke-PsoAttempt 'systemstatus' { Read-Gateway 'systemstatus' 'api/v1/scheduling/systemstatus' }
    $section = [ordered]@{}
    if ($status -is [hashtable]) {
    $get = { param($Key) if ($status.ContainsKey($Key)) { $status[$Key] } else { $null } }
    $section = [ordered]@{
        recent_error_count          = [int] $(if (& $get 'RecentErrorCount') { & $get 'RecentErrorCount' } else { 0 })
        recent_warning_count        = [int] $(if (& $get 'RecentWarningCount') { & $get 'RecentWarningCount' } else { 0 })
        last_status_at              = $(if (& $get 'LastStatusTime') { Format-Rfc3339 (ConvertTo-UtcTime ([string] (& $get 'LastStatusTime'))) } else { $null })
        last_system_test_at         = $(if (& $get 'LastSystemTestTime') { Format-Rfc3339 (ConvertTo-UtcTime ([string] (& $get 'LastSystemTestTime'))) } else { $null })
        last_system_test_successful = $(if ($null -ne (& $get 'LastSystemTestSuccessful')) { [bool] (& $get 'LastSystemTestSuccessful') } else { $null })
        dse_capability              = Get-Decimal (& $get 'TotalDSECapability')
        live_dataset_footprint      = Get-Decimal (& $get 'TotalLiveDatasetFootprint')
        waiting_dataset_footprint   = Get-Decimal (& $get 'TotalWaitingDatasetFootprint')
    }
    }
    $events = Invoke-PsoAttempt 'events' { $answer = Read-Gateway 'events' 'api/v1/scheduling/events' @{ minimumSeverity = 'E'; earliestTime = (Format-Rfc3339 $since); maximumRows = '500' }; if ($answer -and $answer.ContainsKey('Events')) { @($answer.Events) } else { @() } }
    if ($null -ne $events) {
        $count = 0
        foreach ($event in @($events)) { if ($event -is [hashtable] -and $event.ContainsKey('EventTime') -and $event.EventTime -and (ConvertTo-UtcTime ([string] $event.EventTime)) -ge $since) { $count++ } }
        $section.errors_last_24h = $count
    }
    $installations = Invoke-PsoAttempt 'installations' { Read-Gateway 'installations' 'api/v1/system/installations' }
    $first = if ($installations -is [array]) { $installations | Select-Object -First 1 } elseif ($installations -is [hashtable]) { $installations } else { $null }
    if ($first -is [hashtable] -and $first.ContainsKey('Version') -and $first.Version) {
        $section.version = [string] $first.Version
        if ($first.ContainsKey('State') -and $first.State) { $section.installation_state = [string] $first.State }
    }
    $usage = Invoke-PsoAttempt 'usage' { $answer = Read-Gateway 'usage' 'api/v1/scheduling/usage' @{ minimumDateTime = $Now.AddDays(-7).ToString('yyyy-MM-ddTHH:mm:ss'); maximumDateTime = $Now.AddDays(1).ToString('yyyy-MM-ddTHH:mm:ss') }; if ($answer -and $answer.ContainsKey('ScheduleDataUsages')) { @($answer.ScheduleDataUsages) } else { @() } }
    $latest = @{}
    foreach ($row in @($usage)) {
        if ($row -isnot [hashtable] -or ($row.ContainsKey('DatasetId') -and $null -ne $row.DatasetId) -or -not $row.ContainsKey('ScheduleDataUsageType') -or -not $row.ContainsKey('Value') -or $null -eq $row.Value) { continue }
        $kind = [int] $row.ScheduleDataUsageType
        $stamp = [string] $(if ($row.ContainsKey('DatetimeStamp')) { $row.DatetimeStamp } else { '' })
        if ($script:UsageTypes.ContainsKey($kind) -and (-not $latest.ContainsKey($kind) -or [string]::CompareOrdinal($stamp, $latest[$kind][0]) -ge 0)) {
            $latest[$kind] = @($stamp, [int] [math]::Round([double] $row.Value, 0, [MidpointRounding]::AwayFromZero))
        }
    }
    foreach ($kind in $latest.Keys) { $section[$script:UsageTypes[$kind]] = $latest[$kind][1] }
    $result = [ordered]@{}
    foreach ($key in $section.Keys) { if ($null -ne $section[$key]) { $result[$key] = $section[$key] } }
    return $result
}

# ----------------------------------------------------------------------
# Secrets through the managed identity (Azure)
# ----------------------------------------------------------------------

function Get-KeyVaultSecret([string] $VaultUrl, [string] $Name) {
    # One secret with the managed identity: the App Service and Automation
    # endpoint when present, IMDS otherwise. The value is returned and never logged.
    $resource = 'https://vault.azure.net'
    if ($env:IDENTITY_ENDPOINT -and $env:IDENTITY_HEADER) {
        $tokenAnswer = Invoke-RestMethod -Uri "$($env:IDENTITY_ENDPOINT)?resource=$([Uri]::EscapeDataString($resource))&api-version=2019-08-01" -Headers @{ 'X-IDENTITY-HEADER' = $env:IDENTITY_HEADER } -TimeoutSec $script:TimeoutSeconds -NoProxy
    } else {
        $tokenAnswer = Invoke-RestMethod -Uri "http://169.254.169.254/metadata/identity/oauth2/token?resource=$([Uri]::EscapeDataString($resource))&api-version=2018-02-01" -Headers @{ Metadata = 'true' } -TimeoutSec $script:TimeoutSeconds -NoProxy
    }
    $secret = Invoke-RestMethod -Uri "$($VaultUrl.TrimEnd('/'))/secrets/$([Uri]::EscapeDataString($Name))?api-version=7.4" -Headers @{ Authorization = "Bearer $($tokenAnswer.access_token)" } -TimeoutSec $script:TimeoutSeconds -MaximumRedirection 0
    return [string] $secret.value
}

function Get-SecretSetting([string] $Value, [string] $EnvName, [string] $VaultUrl, [string] $NameParameter = $null) {
    $direct = Get-Setting $Value $EnvName
    if ($direct) { return $direct }
    $name = Get-Setting $NameParameter "${EnvName}_NAME"
    if (-not ($name -and $VaultUrl)) { return $null }
    # A vault or identity failure names the variable, never the secret name or the URL.
    try { return Get-KeyVaultSecret $VaultUrl $name } catch {
        $status = 0
        try { $status = [int] $_.Exception.Response.StatusCode } catch { $status = 0 }
        if ($status -gt 0) { throw (New-ReadError "Key Vault read for ${EnvName}_NAME: HTTP $status" $status) }
        throw (New-ReadError "Key Vault read for ${EnvName}_NAME: $($_.Exception.GetType().Name)")
    }
}

function Get-Brief([string[]] $Errors) {
    # The first reasons, printable ASCII, 120 characters: the heartbeat's last_error.
    if ($Errors.Count -eq 0) { return $null }
    $text = [regex]::Replace(($Errors -join '; '), '[^ -~]', '?')
    if ($text.Length -gt 120) { $text = $text.Substring(0, 120) }
    return $text
}

# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

if ($Fixtures) { Initialize-Fixtures $Fixtures }
$vaultUrl = Get-Setting $KeyVaultUrl 'KEY_VAULT_URL'
$now = Get-Now
$payload = [ordered]@{
    schema_version = 1
    collected_at   = Format-Rfc3339 $now
    collector      = [ordered]@{ implementation = 'powershell'; version = $script:Version }
    sources        = [ordered]@{}
}
$failed = $false

# IFS Cloud: sign in first, so a wrong secret or an unreachable IAM is one
# clear error; after that each read only costs its own figure.
$script:Ifs.Base = ($(if (Get-Setting $IfsUrl 'IFS_URL') { Get-Setting $IfsUrl 'IFS_URL' } else { 'https://fixture.invalid' })).TrimEnd('/')
$script:Ifs.ClientId = [string] (Get-Setting $IfsClientId 'IFS_CLIENT_ID')
$script:Ifs.Realm = Get-Setting $IfsRealm 'IFS_REALM'
$script:Ifs.Zone = Get-Setting $IfsServerTimeZone 'IFS_SERVER_TIMEZONE'
try {
    $script:Ifs.Secret = [string] (Get-SecretSetting $IfsClientSecret 'IFS_CLIENT_SECRET' $vaultUrl $IfsClientSecretName)
    Connect-Ifs
    $release = Get-IfsRelease
    if ($release) { $payload.release = $release }
    $health = Get-IfsHealth $now
    if ($health.Count -gt 0) { $payload.health = $health }
    # Two hours back, floored to the hour: the server replaces every
    # reported hour whole, so the oldest bucket must be read complete.
    $windowStart = $now.AddHours(-2); $windowStart = [DateTime]::new($windowStart.Year, $windowStart.Month, $windowStart.Day, $windowStart.Hour, 0, 0, [DateTimeKind]::Utc)
    $messages = Invoke-IfsAttempt 'ApplicationMessageSet' { Get-IfsMessages $windowStart }
    if ($null -ne $messages) { $payload.message_timings = @(Get-MessageBuckets $messages) }
    $payload.sources.ifs = [ordered]@{ last_read_at = (Format-Rfc3339 (Get-Now)); last_error = (Get-Brief $script:Ifs.Errors) }
    Write-Log "IFS read: release $(if ($release) { 'yes' } else { 'no' }), $($health.Count) health figures, $(if ($payload.Contains('message_timings')) { $payload.message_timings.Count } else { 0 }) timing buckets."
} catch [ReadError] {
    $failed = $true
    $payload.sources.ifs = [ordered]@{ last_read_at = $null; last_error = (Get-Brief (@("sign-in: $($_.Exception.Message)") + @($script:Ifs.Errors))) }
    Write-Log "IFS could not be read: $($_.Exception.Message)"
} catch {
    # The heartbeat still goes out.
    $failed = $true
    $payload.sources.ifs = [ordered]@{ last_read_at = $null; last_error = (Get-Brief (@("sign-in: unexpected failure ($($_.Exception.GetType().Name))") + @($script:Ifs.Errors))) }
    Write-Log "IFS could not be read: unexpected failure ($($_.Exception.GetType().Name))."
}

# PSO, when configured (or when the fixtures carry it).
$psoHost = Get-Setting $PsoUrl 'PSO_URL'
if ($psoHost -or ($script:FixtureData -and $null -ne $script:FixtureData.pso)) {
    $script:Pso.Base = ($(if ($psoHost) { $psoHost } else { 'https://fixture.invalid' })).TrimEnd('/') + $script:GatewayPath
    $script:Pso.AccountId = [string] (Get-Setting $PsoAccountId 'PSO_ACCOUNT_ID')
    $script:Pso.UserName = [string] (Get-Setting $PsoUserName 'PSO_USER_NAME')
    try {
        $script:Pso.Password = [string] (Get-SecretSetting $PsoPassword 'PSO_PASSWORD' $vaultUrl $PsoPasswordName)
        Connect-Pso
        try { $payload.pso = Get-PsoSection $now } finally { Disconnect-Pso }
        $payload.sources.pso = [ordered]@{ last_read_at = (Format-Rfc3339 (Get-Now)); last_error = (Get-Brief $script:Pso.Errors) }
        Write-Log "PSO read: $($payload.pso.Count) figures."
    } catch [ReadError] {
        $payload.sources.pso = [ordered]@{ last_read_at = $null; last_error = (Get-Brief (@("session: $($_.Exception.Message)") + @($script:Pso.Errors))) }
        Write-Log "PSO could not be read: $($_.Exception.Message)"
    } catch {
        $payload.sources.pso = [ordered]@{ last_read_at = $null; last_error = (Get-Brief (@("session: unexpected failure ($($_.Exception.GetType().Name))") + @($script:Pso.Errors))) }
        Write-Log "PSO could not be read: unexpected failure ($($_.Exception.GetType().Name))."
    }
}

$body = $payload | ConvertTo-Json -Depth 10
if ($Out) {
    Set-Content -Path $Out -Value $body -Encoding utf8
    Write-Log "Payload written to $Out."
    exit $(if ($failed) { 1 } else { 0 })
}
if ($Print) {
    Write-Output $body
    exit $(if ($failed) { 1 } else { 0 })
}

$endpointUrl = Get-Setting $Endpoint 'STASHLESS_ENDPOINT'
try { $tokenValue = Get-SecretSetting $Token 'STASHLESS_TOKEN' $vaultUrl $TokenName } catch { Write-Log "The collector token could not be read: $($_.Exception.Message)"; exit 1 }
if (-not $endpointUrl -or -not $tokenValue) {
    Write-Log 'STASHLESS_ENDPOINT and STASHLESS_TOKEN (or STASHLESS_TOKEN_NAME with KEY_VAULT_URL) are needed to post.'
    exit 1
}
try {
    $response = Invoke-WebRequest -Method Post -Uri $endpointUrl -Headers @{ Authorization = "Bearer $tokenValue"; Accept = 'application/json'; 'User-Agent' = "stashless-collector-powershell/$($script:Version)" } -ContentType 'application/json' -Body $body -TimeoutSec $script:TimeoutSeconds -MaximumRedirection 0 -SkipHttpErrorCheck -ErrorAction Stop
} catch {
    Write-Log "Stashless could not be reached ($($_.Exception.GetType().Name))."
    exit 1
}
$answerText = ([string] $response.Content)
if ($answerText.Length -gt 300) { $answerText = $answerText.Substring(0, 300) }
if ($response.StatusCode -eq 202) {
    Write-Log "Stashless answered 202: $answerText"
    exit $(if ($failed) { 1 } else { 0 })
}
Write-Log "Stashless refused the report with HTTP $($response.StatusCode): $answerText"
exit 1
