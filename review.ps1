[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$JobDir,

    [string]$InputSrt = 'subtitles.srt',

    [switch]$UseAI,

    [switch]$Approve,

    [switch]$Interactive,

    [string]$Model = 'gpt-5.4-mini',

    [string]$Context = ''
)

$ErrorActionPreference = 'Stop'
if ($Approve -and $Interactive) {
    throw 'Use either -Approve for command-line approval or -Interactive for the review window, not both.'
}
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not [System.IO.Path]::IsPathRooted($JobDir)) {
    $candidate = Join-Path (Join-Path $scriptDirectory 'jobs') $JobDir
    if (Test-Path -LiteralPath $candidate -PathType Container) {
        $JobDir = $candidate
    }
}
if (-not (Test-Path -LiteralPath $JobDir -PathType Container)) {
    throw "Job directory was not found: $JobDir"
}
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw 'Python launcher "py" was not found.'
}

$arguments = @(
    '-m', 'video_agent.review_cli',
    '--job-dir', $JobDir,
    '--input-srt', $InputSrt,
    '--model', $Model
)
if ($UseAI) {
    $arguments += '--ai'
}
if ($Approve) {
    $arguments += '--approve'
}
if ($Context) {
    $arguments += @('--context', $Context)
}

$temporaryApiKey = $false
if ($UseAI -and -not $env:OPENAI_API_KEY) {
    $secureApiKey = Read-Host 'Paste your OpenAI API key (input is hidden)' -AsSecureString
    $apiKeyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureApiKey)
    try {
        $env:OPENAI_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($apiKeyPointer)
        $temporaryApiKey = $true
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($apiKeyPointer)
    }
}

Push-Location $scriptDirectory
try {
    & py @arguments
    $reviewExitCode = $LASTEXITCODE
    if ($reviewExitCode -ne 0) {
        exit $reviewExitCode
    }
    if ($Interactive) {
        $windowScript = Join-Path $scriptDirectory 'review-window.ps1'
        $guiArguments = @(
            '-NoProfile',
            '-ExecutionPolicy', 'Bypass',
            '-Sta',
            '-File', $windowScript,
            '-JobDir', $JobDir
        )
        if ($PSBoundParameters.ContainsKey('InputSrt') -and -not $UseAI) {
            $guiArguments += @('-Candidate', $InputSrt)
        }
        & powershell.exe @guiArguments
        exit $LASTEXITCODE
    }
    exit $reviewExitCode
}
finally {
    Pop-Location
    if ($temporaryApiKey) {
        Remove-Item Env:\OPENAI_API_KEY
    }
}
