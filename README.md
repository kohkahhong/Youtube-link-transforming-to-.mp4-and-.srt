# Video Content Agent

私人使用的视频内容处理项目。第一阶段接收 YouTube 或抖音链接，同时启动：

- MP4 分支：下载音视频并合并或封装为 `video.mp4`。
- 字幕分支：优先提取平台提供的独立字幕轨并转换为 SRT；没有字幕轨时，等待 MP4 完成后使用本地 Whisper 生成 `subtitles.srt`。

每个任务创建独立目录，并生成 `manifest.json` 记录两个分支的状态和输出。

## 当前要求

- Windows PowerShell
- Python（使用 `py` 启动）
- `yt-dlp`
- FFmpeg
- `openai-whisper`（仅在没有平台字幕时需要）
- OpenAI API 密钥（仅在选择 AI 字幕纠错时需要）

## 运行

在项目目录中执行：

```powershell
.\run.ps1 -Url "YOUTUBE_OR_DOUYIN_URL"
```

可选参数：

```powershell
.\run.ps1 `
  -Url "URL" `
  -Language "zh" `
  -WhisperModel "base" `
  -CookiesFromBrowser "edge"
```

`base` 速度较快；`small` 通常更准确，但在 CPU 上更慢。需要提高转写准确率时可改为：

```powershell
.\run.ps1 -Url "URL" -Language "zh" -WhisperModel "small"
```

### Windows 浏览器 Cookie

Chrome 或 Edge 可能因为浏览器进程锁定、或 Chromium 的 Windows DPAPI
加密而无法由 `yt-dlp` 读取 Cookie。若日志出现
`Failed to decrypt with DPAPI`，请使用一个专门用于抖音的 Firefox 会话：

1. 用 Firefox 打开抖音链接并完成验证。
2. 关闭全部 Firefox 窗口，确认没有 `firefox.exe` 进程。
3. 使用 `-CookiesFromBrowser "firefox"` 重新运行。

不要将浏览器 Cookie 文件提交到 Git 仓库或分享给他人。

任务输出位于 `jobs/<任务编号>/`：

```text
video.mp4
subtitles.srt
transcript.txt
subtitle-review.md
manifest.json
video.log
subtitles.log
platform.*.srt（平台存在多个字幕时保留）
```

完成后流程停在 `WAITING_SUBTITLE_REVIEW`，不会直接生成全文或推文。

## 为什么画面有字幕，平台却显示无字幕

画面底部的文字可能已经直接绘制进视频像素，称为“烧录字幕”或“硬字幕”。
`yt-dlp` 只能直接下载平台接口暴露的独立字幕轨，不能把视频像素自动当成 SRT。
因此，平台字幕探测结果为“无字幕”时，程序会使用 Whisper 从声音转写；这属于正常回退，
不代表画面中没有文字。视觉 OCR 是独立能力，当前第一版没有把它作为默认步骤。

## 字幕审核与 AI 纠错

为已有任务生成 Windows 可正常打开的纯文本和审核说明：

```powershell
.\review.ps1 -JobDir "20260815-163627-68ed99"
```

输出：

```text
transcript.txt          无时间轴纯文本，UTF-8 with BOM
subtitle-review.md      审核说明
```

可选 AI 纠错（不会覆盖原字幕）：

```powershell
.\review.ps1 `
  -JobDir "20260815-163627-68ed99" `
  -UseAI `
  -Context "河南、台风暴雨、气象地理科普"
```

若当前 PowerShell 没有 `OPENAI_API_KEY`，脚本会隐藏输入并仅在本次运行期间使用。
AI 纠错输出：

```text
subtitles.ai.srt
transcript.ai.txt
correction-report.md
```

AI 可能误改专有名词和数字，必须对照视频抽查。确认无误后，把 AI 草稿标记为审核版：

```powershell
.\review.ps1 `
  -JobDir "20260815-163627-68ed99" `
  -InputSrt "subtitles.ai.srt" `
  -Approve
```

此时生成 `subtitles.approved.srt` 和 `transcript.approved.txt`，任务状态变为
`SRT_APPROVED`，然后才能进入全文整理。

### Windows 人工审核窗口

当前已有 AI 草稿的任务，可以直接打开逐条审核窗口：

```powershell
.\review.ps1 -JobDir "20260815-163627-68ed99" -Interactive
```

窗口提供：

- 分别打开原始字幕、AI 审核稿和纠错报告；
- 在记事本中修改并保存审核稿；
- 调用系统播放器打开 `video.mp4`；
- “保存草稿，稍后处理”；
- “批准并进入下一步”；
- “拒绝 AI 草稿”。

若当前 Python 的 Tk 图形组件完整，还可以使用高级逐条对比编辑器：

```powershell
py -m video_agent.review_gui --job-dir "jobs\20260815-163627-68ed99"
```

也可以让新任务在 MP4 和 SRT 完成后自动弹出审核窗口：

```powershell
.\run.ps1 `
  -Url "URL" `
  -Language "zh" `
  -CookiesFromBrowser "firefox" `
  -OpenReviewWindow
```

此窗口是项目自己的内容审核界面。Codex 内置的批准框属于沙箱、网络和外部副作用
等工具权限审批，不能由普通 PowerShell 脚本拿来审核字幕内容。

## 编码说明

- `subtitles.srt` 是正式字幕。
- `transcript.txt` 是可阅读的纯文本字幕。
- `subtitles.log` 是诊断日志，不是字幕成品。
- 新任务会统一使用 UTF-8；`transcript.txt` 使用 UTF-8 BOM，避免旧版 Windows
  编辑器误判为 GBK 后出现“锟斤拷”。
