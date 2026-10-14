<#
.SYNOPSIS
    Connectivity control check: DNS / TCP / (best-effort) HTTPS. No model calls.

.DESCRIPTION
    Purpose: decide whether the chatgpt.com / api.openai.com connection failures
    seen inside the agent sandbox are environment-specific, or simply this
    machine/network.

    It only performs network-layer probes:
      1. DNS resolution
      2. TCP connect on 443 (with explicit timeout)
      3. HTTPS HEAD (best effort)
    It does NOT start Codex, does NOT call any model, and does NOT read any
    credential.

    IMPORTANT CAVEAT about the HTTPS column
    ---------------------------------------
    On this machine the shell's TLS stack (schannel) is known to be broken: even
    pypi.org, which is plainly reachable, fails the HTTPS probe with
    "The underlying connection was closed". So an HTTPS FAIL here is NOT
    evidence about the target: read the DNS and TCP columns, and use
    tools/net_check.py (Python's OpenSSL works) for a trustworthy TLS check.

    NOTE: this file is deliberately ASCII-only. Windows PowerShell 5.1 reads
    .ps1 files as ANSI unless they carry a UTF-8 BOM, which garbles non-ASCII
    text and can even break parsing.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\net_check.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\net_check.ps1 -OutJson var\net_check.json
#>
[CmdletBinding()]
param(
    [string[]]$Hosts = @('chatgpt.com', 'api.openai.com', 'pypi.org'),
    [int]$Port = 443,
    [int]$TimeoutMs = 8000,
    [string]$OutJson = ''
)

$ErrorActionPreference = 'Continue'
$results = @()

function Get-DnsInfo {
    param([string]$Name)
    $ips = @()
    $err = ''
    try {
        $ips = @(Resolve-DnsName -Name $Name -Type A -ErrorAction Stop |
                 Where-Object { $_.IPAddress } |
                 Select-Object -ExpandProperty IPAddress)
    } catch {
        $err = $_.Exception.Message
    }
    if (-not $ips -and -not $err) { $err = 'no A records returned' }
    return [pscustomobject]@{ Ips = $ips; Error = $err }
}

function Test-TcpConnect {
    param([string]$Name, [int]$P)
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $ok = $false
    $err = ''
    $client = $null
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect($Name, $P, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne($TimeoutMs)) {
            $client.EndConnect($iar)
            $ok = $true
        } else {
            $err = "timeout after ${TimeoutMs}ms"
        }
    } catch {
        $err = $_.Exception.Message
    } finally {
        if ($client) { try { $client.Close() } catch { } }
    }
    $sw.Stop()
    return [pscustomobject]@{ Ok = $ok; Error = $err; Ms = $sw.ElapsedMilliseconds }
}

function Test-HttpsHead {
    param([string]$Name)
    $status = $null
    $err = ''
    try {
        $r = Invoke-WebRequest -Uri "https://$Name/" -Method Head -TimeoutSec 15 `
                               -UseBasicParsing -ErrorAction Stop
        $status = [int]$r.StatusCode
    } catch {
        $err = $_.Exception.Message
        # Collapse the very common local-TLS failure into one clear token.
        if ($err -match 'underlying connection|schannel|SEC_E|closed') {
            $err = 'LOCAL_TLS_STACK_FAILURE (expected here; not evidence about the target)'
        }
    }
    return [pscustomobject]@{ Status = $status; Error = $err }
}

Write-Host ('=' * 78)
Write-Host 'Connectivity control check (no model calls)'
Write-Host ("time   : {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
Write-Host ("targets: {0}  port {1}  timeout {2}ms" -f ($Hosts -join ', '), $Port, $TimeoutMs)
Write-Host ('=' * 78)

foreach ($h in $Hosts) {
    Write-Host ''
    Write-Host ("--- {0} ---" -f $h)

    $dns = Get-DnsInfo -Name $h
    if ($dns.Ips.Count -gt 0) {
        Write-Host ("  DNS  : OK    {0}" -f ($dns.Ips -join ', '))
    } else {
        Write-Host ("  DNS  : FAIL  {0}" -f $dns.Error)
    }

    $tcp = Test-TcpConnect -Name $h -P $Port
    if ($tcp.Ok) {
        Write-Host ("  TCP  : OK    {0}:{1} ({2}ms)" -f $h, $Port, $tcp.Ms)
    } else {
        Write-Host ("  TCP  : FAIL  {0} ({1}ms)" -f $tcp.Error, $tcp.Ms)
    }

    $https = Test-HttpsHead -Name $h
    if ($https.Status) {
        Write-Host ("  HTTPS: OK    status={0}" -f $https.Status)
    } else {
        Write-Host ("  HTTPS: FAIL  {0}" -f $https.Error)
    }

    if (-not $dns.Ips.Count) { $verdict = 'DNS_FAIL' }
    elseif (-not $tcp.Ok)    { $verdict = 'TCP_FAIL' }
    elseif (-not $https.Status) { $verdict = 'TLS_UNRELIABLE_HERE' }
    else                     { $verdict = 'REACHABLE' }

    $results += [pscustomobject]@{
        host        = $h
        dns_ips     = ($dns.Ips -join ';')
        dns_error   = $dns.Error
        tcp_ok      = $tcp.Ok
        tcp_error   = $tcp.Error
        tcp_ms      = $tcp.Ms
        https_code  = $https.Status
        https_error = $https.Error
        verdict     = $verdict
    }
}

Write-Host ''
Write-Host ('=' * 78)
Write-Host 'Summary'
Write-Host ('=' * 78)
$results | Format-Table host, dns_ips, tcp_ok, tcp_ms, https_code, verdict -AutoSize

if ($OutJson) {
    $results | ConvertTo-Json -Depth 4 | Set-Content -Path $OutJson -Encoding UTF8
    Write-Host ("written: {0}" -f $OutJson)
}

Write-Host ''
Write-Host 'How to read this:'
Write-Host '  pypi.org also fails DNS/TCP -> the whole machine/network is down;'
Write-Host '                                 unrelated to this project.'
Write-Host '  pypi.org OK but OpenAI hosts fail -> target-specific problem.'
Write-Host '  DNS error or a surprising IP -> suspect DNS policy / poisoning.'
Write-Host '  The HTTPS column is unreliable on this machine (see the caveat at the'
Write-Host '  top of this script); trust tools/net_check.py for TLS.'
Write-Host ''
Write-Host 'This script does not start Codex and does not call any model.'

exit 0
