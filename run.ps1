[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^https?://')]
    [string]$Url,

    [string]$OutputRoot = '',

    [string]$Language = 'auto',

    [ValidateSet('tiny', 'base', 'small', 'medium', 'turbo', 'large')]
    [string]$WhisperModel = 'base',

    [switch]$OpenReviewWindow,

    [ValidateSet('', 'edge', 'chrome', 'firefox')]
    [string]$CookiesFromBrowser = ''
)

$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $OutputRoot) {
    $OutputRoot = Join-Path $scriptDirectory 'jobs'
}

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw 'Python launcher "py" was not found.'
}

if ($CookiesFromBrowser) {
    $browserProcessNames = @{
        edge = 'msedge'
        chrome = 'chrome'
        firefox = 'firefox'
    }
    $browserProcessName = $browserProcessNames[$CookiesFromBrowser]
    $runningBrowser = Get-Process -Name $browserProcessName -ErrorAction SilentlyContinue
    if ($runningBrowser) {
        throw "Close every $CookiesFromBrowser window and make sure no $browserProcessName.exe process remains in Task Manager before reading browser cookies. Closing only the Douyin tab is not enough."
    }
}

$arguments = @(
    '-m', 'video_agent.cli',
    '--url', $Url,
    '--output-root', $OutputRoot,
    '--language', $Language,
    '--whisper-model', $WhisperModel
)

if ($CookiesFromBrowser) {
    $arguments += @('--cookies-from-browser', $CookiesFromBrowser)
}
if ($OpenReviewWindow) {
    $arguments += '--interactive-review'
}

Push-Location $scriptDirectory
try {
    & py @arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
