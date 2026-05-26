[CmdletBinding(DefaultParameterSetName = "ByThumbprint")]
Param(
  [Parameter(ParameterSetName = "ByThumbprint", Mandatory = $true)]
  [string]$Thumbprint,

  [Parameter(ParameterSetName = "BySubjectRegex", Mandatory = $true)]
  [string]$SubjectRegex,

  [string]$OutFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Normalize-Thumbprint {
  param([string]$Value)
  if (-not $Value) { return "" }
  return (($Value -replace "[^0-9a-fA-F]", "").ToUpperInvariant())
}

function Get-RootCandidates {
  param(
    [System.Security.Cryptography.X509Certificates.X509Certificate2[]]$Certs
  )

  $out = @()
  foreach ($c in $Certs) {
    if (-not $c) { continue }

    $isCa = $false
    foreach ($ext in $c.Extensions) {
      if ($ext -is [System.Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]) {
        if ($ext.CertificateAuthority) { $isCa = $true }
      }
    }

    if (-not $isCa) { continue }
    if ($c.Subject -ne $c.Issuer) { continue } # Prefer true roots over intermediates in Root/CA stores.
    if ($c.NotAfter -le (Get-Date)) { continue }
    $out += $c
  }

  return $out
}

function Find-RootCert {
  $stores = @(
    "Cert:\CurrentUser\Root",
    "Cert:\LocalMachine\Root",
    "Cert:\CurrentUser\CA",
    "Cert:\LocalMachine\CA"
  )
  $matchesByThumb = @{}

  foreach ($store in $stores) {
    if (-not (Test-Path $store)) { continue }
    $certs = @()
    try {
      $certs = @(Get-ChildItem -Path $store -ErrorAction Stop)
    } catch {
      Write-Warning ("Could not enumerate {0}: {1}" -f $store, $_.Exception.Message)
      continue
    }
    $certs = Get-RootCandidates -Certs $certs

    foreach ($c in $certs) {
      if ($PSCmdlet.ParameterSetName -eq "ByThumbprint") {
        $want = Normalize-Thumbprint -Value $Thumbprint
        $have = Normalize-Thumbprint -Value $c.Thumbprint
        if ($have -and ($have -eq $want)) {
          $matchesByThumb[$c.Thumbprint.ToUpperInvariant()] = $c
        }
      } else {
        if ($c.Subject -match $SubjectRegex) {
          if ($c.Thumbprint) {
            $matchesByThumb[$c.Thumbprint.ToUpperInvariant()] = $c
          }
        }
      }
    }
  }

  $unique = @($matchesByThumb.Values)

  if ($unique.Count -eq 0) {
    throw "No matching root certificate found. Try specifying -Thumbprint or loosen -SubjectRegex."
  }

  if ($unique.Count -gt 1) {
    $lines = $unique | ForEach-Object { "$($_.Thumbprint)  $($_.Subject)" }
    $msg = "Multiple matching root certificates found:`n" + ($lines -join "`n") + "`nRefine -SubjectRegex or pass -Thumbprint."
    throw $msg
  }

  return $unique[0]
}

function To-Pem {
  param([byte[]]$DerBytes)

  $b64 = [Convert]::ToBase64String($DerBytes)
  $sb = New-Object System.Text.StringBuilder
  [void]$sb.AppendLine("-----BEGIN CERTIFICATE-----")
  for ($i = 0; $i -lt $b64.Length; $i += 64) {
    $len = [Math]::Min(64, $b64.Length - $i)
    [void]$sb.AppendLine($b64.Substring($i, $len))
  }
  [void]$sb.AppendLine("-----END CERTIFICATE-----")
  return $sb.ToString()
}

function Find-GitCaBundle {
  $gitExe = $null
  try {
    $gitExe = (Get-Command git -ErrorAction Stop).Source
  } catch {
    $gitExe = $null
  }

  if ($gitExe) {
    $gitBin = Split-Path -Parent $gitExe
    $gitRoot = Split-Path -Parent $gitBin
    $rel = @(
      "usr\ssl\certs\ca-bundle.crt",
      "mingw64\ssl\certs\ca-bundle.crt",
      "mingw32\ssl\certs\ca-bundle.crt"
    )
    foreach ($r in $rel) {
      $p = Join-Path $gitRoot $r
      if (Test-Path $p) { return $p }
    }
  }

  $candidates = @(
    "C:\Program Files\Git\usr\ssl\certs\ca-bundle.crt",
    "C:\Program Files\Git\mingw64\ssl\certs\ca-bundle.crt",
    "C:\Program Files (x86)\Git\usr\ssl\certs\ca-bundle.crt",
    "C:\Program Files (x86)\Git\mingw64\ssl\certs\ca-bundle.crt"
  )

  foreach ($p in $candidates) {
    if (Test-Path $p) { return $p }
  }

  throw "Unable to find Git-for-Windows ca-bundle.crt. Install Git for Windows (or ensure git.exe is on PATH)."
}

$cert = Find-RootCert
$pem = To-Pem -DerBytes $cert.RawData

if (-not $OutFile -or $OutFile.Trim() -eq "") {
  $stamp = (Get-Date).ToString("yyyyMMdd_HHmmss")
  $OutFile = Join-Path $env:TEMP ("git_ca_bundle_plus_mitm_{0}.crt" -f $stamp)
}

$caBundle = Find-GitCaBundle

Copy-Item -LiteralPath $caBundle -Destination $OutFile -Force
Add-Content -LiteralPath $OutFile -Value "`n"
Add-Content -LiteralPath $OutFile -Value $pem

Write-Output $OutFile
