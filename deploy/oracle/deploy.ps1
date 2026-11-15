<#
Deploy (or update) Process Annotation on your Oracle Cloud free server.

  powershell -ExecutionPolicy Bypass -File deploy\oracle\deploy.ps1 -Ip 129.146.12.34 -Key C:\Users\you\Downloads\ssh-key.key

  -WithData     also copy your local studies and station templates (the first team created gets them)
  -AccessCode   access code people need to create a team (default: keeps the one from last time, else a new one)

Your Gemini key and settings come from the local .env file; they are copied to the server, never printed.
The link is https://<ip-with-dashes>.sslip.io (free HTTPS certificate, no domain needed).
#>
param(
    [Parameter(Mandatory = $true)][string]$Ip,
    [Parameter(Mandatory = $true)][string]$Key,
    [string]$User = "ubuntu",
    [string]$AccessCode = "",
    [switch]$WithData
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$app = (Resolve-Path (Join-Path $here "..\..")).Path
$domain = ($Ip.Trim() -replace '\.', '-') + ".sslip.io"
$codeFile = Join-Path $here ".access-code"
if (-not $AccessCode) {
    if (Test-Path $codeFile) { $AccessCode = (Get-Content $codeFile -Raw).Trim() }
    else { $AccessCode = "pa-" + (Get-Random -Minimum 1000 -Maximum 9999) }
}
Set-Content -Path $codeFile -Value $AccessCode -Encoding ascii

if (-not (Test-Path $Key)) { throw "SSH key not found: $Key" }
# OpenSSH refuses a private key that other Windows users can read
icacls $Key /inheritance:r /grant:r "$($env:USERNAME):(R)" | Out-Null
$ssh = @("-i", $Key, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20")
$target = "$User@$Ip"

if (git -C $app status --porcelain) { Write-Warning "Uncommitted changes are NOT deployed (only the last commit is)." }
$tar = Join-Path $env:TEMP "process-annotation.tar"
git -C $app archive --format=tar -o $tar HEAD

# settings for the server: Gemini + app settings from the local .env, plus the server's own
$keep = Get-Content (Join-Path $app ".env") | Where-Object {
    $_ -match '^\s*(GEMINI_API_KEY|GOOGLE_API_KEY|WORKSTUDY_[A-Z_]+)\s*=' -and
    $_ -notmatch '^\s*WORKSTUDY_(AUTH|DATA|SIGNUP_CODE|SEED_FIRST_TEAM|NO_BROWSER|ALIGN_FPS)\s*='
}
$lines = @($keep) + @("WORKSTUDY_AUTH=1", "WORKSTUDY_SIGNUP_CODE=$AccessCode", "WORKSTUDY_MONTHLY_ANALYSES=30",
                      "WORKSTUDY_SEED_FIRST_TEAM=1", "DOMAIN=$domain")
$envFile = Join-Path $env:TEMP "process-annotation.env"
Set-Content -Path $envFile -Value $lines -Encoding ascii

try {
    Write-Host ">> sending the app to $Ip"
    scp @ssh $tar $envFile (Join-Path $here "setup.sh") "${target}:/tmp/"
    if ($LASTEXITCODE) { throw "Could not copy files to the server (check the IP, the key and that the server is running)." }
    if ($WithData) {
        Write-Host ">> sending your studies and templates (this can take a while)"
        ssh @ssh $target "rm -rf /tmp/pa-data && mkdir -p /tmp/pa-data"
        scp -r @ssh (Join-Path $app "data\studies") (Join-Path $app "data\stations") "${target}:/tmp/pa-data/"
        if ($LASTEXITCODE) { throw "Copying the studies failed." }
    }
    ssh @ssh $target "sed -i 's/\r$//' /tmp/setup.sh && bash /tmp/setup.sh && rm -f /tmp/setup.sh"
    if ($LASTEXITCODE) { throw "Setup on the server failed (see the output above)." }
}
finally {
    Remove-Item $envFile, $tar -ErrorAction SilentlyContinue   # the env file holds your API key
}

Write-Host ">> waiting for the HTTPS certificate"
$url = "https://$domain"
$ok = $false
for ($i = 0; $i -lt 30 -and -not $ok; $i++) {
    try { $r = Invoke-WebRequest "$url/api/config" -UseBasicParsing -TimeoutSec 10; $ok = $r.StatusCode -eq 200 } catch { Start-Sleep -Seconds 10 }
}
Write-Host ""
if ($ok) { Write-Host "Process Annotation is live:  $url" -ForegroundColor Green }
else { Write-Host "Started, but $url does not answer yet. Check the Oracle security list allows TCP 80 and 443, then open the link in a few minutes." -ForegroundColor Yellow }
Write-Host "Access code to create a team: $AccessCode"
Write-Host "Create your account first: the first team gets the copied studies."
