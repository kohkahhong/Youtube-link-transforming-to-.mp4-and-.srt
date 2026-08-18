[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$JobDir,

    [string]$Candidate = '',

    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not [System.IO.Path]::IsPathRooted($JobDir)) {
    $jobCandidate = Join-Path (Join-Path $scriptDirectory 'jobs') $JobDir
    if (Test-Path -LiteralPath $jobCandidate -PathType Container) {
        $JobDir = $jobCandidate
    }
}
$JobDir = [System.IO.Path]::GetFullPath($JobDir)
if (-not (Test-Path -LiteralPath $JobDir -PathType Container)) {
    throw "Job directory was not found: $JobDir"
}

$originalPath = Join-Path $JobDir 'subtitles.srt'
if (-not (Test-Path -LiteralPath $originalPath -PathType Leaf)) {
    throw "Original subtitle was not found: $originalPath"
}
if ($Candidate) {
    $candidatePath = Join-Path $JobDir $Candidate
}
elseif (Test-Path -LiteralPath (Join-Path $JobDir 'subtitles.ai.srt') -PathType Leaf) {
    $candidatePath = Join-Path $JobDir 'subtitles.ai.srt'
}
else {
    $candidatePath = $originalPath
}
if (-not (Test-Path -LiteralPath $candidatePath -PathType Leaf)) {
    throw "Review candidate was not found: $candidatePath"
}
$candidateName = Split-Path -Leaf $candidatePath
$reportPath = Join-Path $JobDir 'correction-report.md'
$videoPath = Join-Path $JobDir 'video.mp4'

if ($CheckOnly) {
    "Review window data OK: candidate=$candidateName"
    exit 0
}
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw 'Python launcher "py" was not found.'
}
Set-Location -LiteralPath $scriptDirectory

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$form = New-Object System.Windows.Forms.Form
$form.Text = 'Video Content Agent - 字幕人工审批'
$form.StartPosition = 'CenterScreen'
$form.Size = New-Object System.Drawing.Size(760, 520)
$form.MinimumSize = New-Object System.Drawing.Size(700, 480)
$form.TopMost = $true

$title = New-Object System.Windows.Forms.Label
$title.Text = '字幕已经准备好，请决定是否批准'
$title.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 16, [System.Drawing.FontStyle]::Bold)
$title.AutoSize = $true
$title.Location = New-Object System.Drawing.Point(24, 22)
$form.Controls.Add($title)

$status = New-Object System.Windows.Forms.Label
$status.Text = "任务：$(Split-Path -Leaf $JobDir)`r`n审核稿：$candidateName"
$status.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 10)
$status.AutoSize = $true
$status.Location = New-Object System.Drawing.Point(27, 65)
$form.Controls.Add($status)

$instructions = New-Object System.Windows.Forms.TextBox
$instructions.Multiline = $true
$instructions.ReadOnly = $true
$instructions.ScrollBars = 'Vertical'
$instructions.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 10)
$instructions.Location = New-Object System.Drawing.Point(28, 112)
$instructions.Size = New-Object System.Drawing.Size(685, 185)
$instructions.Anchor = 'Top,Left,Right'
$instructions.Text = @"
审核建议：

1. 点击“打开审核字幕”，在记事本中检查或修改并保存。
2. 点击“查看纠错报告”，重点核对地名、人名、年份、数字和专业名词。
3. 可以点击“播放视频”对照声音和画面。
4. 确认无误后点击“批准并进入下一步”。

批准会生成 subtitles.approved.srt；拒绝只会记录决定，不会删除原始字幕或 AI 草稿。
"@
$form.Controls.Add($instructions)

function Add-ActionButton {
    param(
        [string]$Text,
        [int]$X,
        [int]$Y,
        [int]$Width,
        [scriptblock]$OnClick
    )
    $button = New-Object System.Windows.Forms.Button
    $button.Text = $Text
    $button.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 10)
    $button.Location = New-Object System.Drawing.Point($X, $Y)
    $button.Size = New-Object System.Drawing.Size($Width, 38)
    $button.Add_Click($OnClick)
    $form.Controls.Add($button)
    return $button
}

$openSubtitle = Add-ActionButton -Text '打开审核字幕' -X 28 -Y 320 -Width 155 -OnClick {
    Start-Process -FilePath 'notepad.exe' -ArgumentList @($candidatePath)
}
$openOriginal = Add-ActionButton -Text '打开原始字幕' -X 195 -Y 320 -Width 155 -OnClick {
    Start-Process -FilePath 'notepad.exe' -ArgumentList @($originalPath)
}
$openReport = Add-ActionButton -Text '查看纠错报告' -X 362 -Y 320 -Width 155 -OnClick {
    if (Test-Path -LiteralPath $reportPath -PathType Leaf) {
        Start-Process -FilePath 'notepad.exe' -ArgumentList @($reportPath)
    }
    else {
        [System.Windows.Forms.MessageBox]::Show('当前任务没有纠错报告。', '提示') | Out-Null
    }
}
$openVideo = Add-ActionButton -Text '播放视频' -X 529 -Y 320 -Width 155 -OnClick {
    if (Test-Path -LiteralPath $videoPath -PathType Leaf) {
        Start-Process -FilePath $videoPath
    }
    else {
        [System.Windows.Forms.MessageBox]::Show('当前任务没有 video.mp4。', '提示') | Out-Null
    }
}

$laterButton = Add-ActionButton -Text '稍后处理' -X 28 -Y 404 -Width 150 -OnClick {
    $form.Tag = 'PENDING'
    $form.Close()
}
$rejectButton = Add-ActionButton -Text '拒绝 AI 草稿' -X 370 -Y 404 -Width 155 -OnClick {
    $answer = [System.Windows.Forms.MessageBox]::Show(
        '拒绝后会保留草稿，任务不会进入全文整理。确定拒绝吗？',
        '确认拒绝',
        [System.Windows.Forms.MessageBoxButtons]::YesNo,
        [System.Windows.Forms.MessageBoxIcon]::Warning
    )
    if ($answer -ne [System.Windows.Forms.DialogResult]::Yes) {
        return
    }
    & py -m video_agent.review_cli --job-dir $JobDir --input-srt $candidateName --reject
    if ($LASTEXITCODE -eq 0) {
        [System.Windows.Forms.MessageBox]::Show('已拒绝并保留草稿。', '已完成') | Out-Null
        $form.Tag = 'REJECTED'
        $form.Close()
    }
    else {
        [System.Windows.Forms.MessageBox]::Show('记录拒绝状态失败，请查看 PowerShell 输出。', '错误') | Out-Null
    }
}
$rejectButton.BackColor = [System.Drawing.Color]::MistyRose

$approveButton = Add-ActionButton -Text '批准并进入下一步' -X 537 -Y 404 -Width 175 -OnClick {
    $answer = [System.Windows.Forms.MessageBox]::Show(
        '请先保存记事本中的修改。确定批准当前字幕吗？',
        '确认批准',
        [System.Windows.Forms.MessageBoxButtons]::YesNo,
        [System.Windows.Forms.MessageBoxIcon]::Question
    )
    if ($answer -ne [System.Windows.Forms.DialogResult]::Yes) {
        return
    }
    & py -m video_agent.review_cli --job-dir $JobDir --input-srt $candidateName --approve
    if ($LASTEXITCODE -eq 0) {
        [System.Windows.Forms.MessageBox]::Show(
            '审核通过，已生成 subtitles.approved.srt。任务状态为 SRT_APPROVED。',
            '审核通过'
        ) | Out-Null
        $form.Tag = 'APPROVED'
        $form.Close()
    }
    else {
        [System.Windows.Forms.MessageBox]::Show('批准失败，请查看 PowerShell 输出。', '错误') | Out-Null
    }
}
$approveButton.BackColor = [System.Drawing.Color]::Honeydew
$form.AcceptButton = $approveButton
$form.CancelButton = $laterButton

[void]$form.ShowDialog()
if ($form.Tag) {
    "Review decision: $($form.Tag)"
}
else {
    'Review decision: PENDING'
}
