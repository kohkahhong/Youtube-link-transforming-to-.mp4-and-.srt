from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse
from uuid import uuid4

from .subtitles import create_review_artifacts


SUPPORTED_BROWSERS = {"edge", "chrome", "firefox"}


class PipelineError(RuntimeError):
    """Raised when a required pipeline stage cannot complete."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def detect_platform(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PipelineError("The input must be a valid HTTP or HTTPS URL.")

    host = parsed.hostname.lower().removeprefix("www.")
    if host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com"):
        return "youtube"
    if host == "douyin.com" or host.endswith(".douyin.com") or host.endswith(".iesdouyin.com"):
        return "douyin"
    raise PipelineError(f"Unsupported video platform: {host}")


def require_command(name: str) -> str:
    resolved = shutil.which(name)
    if not resolved:
        raise PipelineError(f"Required command is not installed or not on PATH: {name}")
    return resolved


def append_cookie_args(
    command: list[str],
    browser: str | None = None,
    cookie_file: Path | None = None,
) -> list[str]:
    if browser and cookie_file:
        raise PipelineError("Use either browser cookies or a cookie file, not both.")
    if cookie_file:
        return [*command, "--cookies", str(cookie_file)]
    if not browser:
        return command
    if browser not in SUPPORTED_BROWSERS:
        raise PipelineError(f"Unsupported browser for cookies: {browser}")
    return [*command, "--cookies-from-browser", browser]


def build_video_command(
    yt_dlp: str,
    url: str,
    output_template: Path,
    browser: str | None = None,
    cookie_file: Path | None = None,
) -> list[str]:
    command = [
        yt_dlp,
        "--no-playlist",
        "-f",
        "bv*+ba/b",
        "--merge-output-format",
        "mp4",
        "--remux-video",
        "mp4",
        "-o",
        str(output_template),
    ]
    command = append_cookie_args(command, browser, cookie_file)
    return [*command, "--", url]


def build_subtitle_command(
    yt_dlp: str,
    url: str,
    output_template: Path,
    browser: str | None = None,
    cookie_file: Path | None = None,
) -> list[str]:
    command = [
        yt_dlp,
        "--no-playlist",
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs",
        "all,-live_chat",
        "--convert-subs",
        "srt",
        "-o",
        str(output_template),
    ]
    command = append_cookie_args(command, browser, cookie_file)
    return [*command, "--", url]


def run_logged(command: list[str], log_path: Path, label: str) -> CommandResult:
    lines: list[str] = []
    lock = threading.Lock()
    with log_path.open("a", encoding="utf-8") as log_file:
        child_environment = os.environ.copy()
        child_environment["PYTHONIOENCODING"] = "utf-8"
        child_environment["PYTHONUTF8"] = "1"
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_environment,
        )
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.rstrip("\r\n")
            lines.append(line)
            with lock:
                print(f"[{label}] {line}", flush=True)
                log_file.write(line + "\n")
                log_file.flush()
        returncode = process.wait()
    return CommandResult(returncode=returncode, output="\n".join(lines))


def choose_subtitle(candidates: Iterable[Path], language: str = "auto") -> Path | None:
    files = sorted(candidates, key=lambda path: path.name.lower())
    if not files:
        return None

    normalized_language = language.lower().replace("_", "-")
    if normalized_language != "auto":
        exact = [path for path in files if f".{normalized_language}." in path.name.lower()]
        if exact:
            return exact[0]
        prefix = [
            path
            for path in files
            if f".{normalized_language.split('-')[0]}" in path.name.lower()
        ]
        if prefix:
            return prefix[0]

    preferences = ("zh-hans", "zh-cn", ".zh.", ".zh-", ".en.", ".en-")
    for preference in preferences:
        for path in files:
            if preference in path.name.lower():
                return path
    return files[0]


class MediaPipeline:
    def __init__(
        self,
        url: str,
        output_root: Path,
        language: str = "auto",
        cookies_from_browser: str | None = None,
        whisper_model: str = "base",
    ) -> None:
        self.url = url
        self.platform = detect_platform(url)
        self.output_root = output_root.resolve()
        self.language = language
        self.cookies_from_browser = cookies_from_browser or None
        self.whisper_model = whisper_model
        self.cookie_file: Path | None = None
        self.job_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
        self.job_dir = self.output_root / self.job_id
        self.manifest_path = self.job_dir / "manifest.json"
        self.yt_dlp = ""
        self.ffmpeg = ""
        self.manifest: dict[str, object] = {
            "job_id": self.job_id,
            "source_url": self.url,
            "source_platform": self.platform,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "stage": "RECEIVED",
            "branches": {
                "video": {"status": "PENDING", "path": None, "error": None},
                "subtitles": {
                    "status": "PENDING",
                    "path": None,
                    "source": None,
                    "error": None,
                },
            },
        }

    def write_manifest(self) -> None:
        self.manifest["updated_at"] = utc_now()
        self.job_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def prepare(self) -> None:
        self.yt_dlp = require_command("yt-dlp")
        self.ffmpeg = require_command("ffmpeg")
        self.job_dir.mkdir(parents=True, exist_ok=False)
        self.manifest["stage"] = "PROCESSING_MEDIA"
        self.write_manifest()

    def snapshot_browser_cookies(self) -> None:
        if not self.cookies_from_browser:
            return
        cookie_file = self.job_dir / "browser-cookies.txt"
        command = [
            self.yt_dlp,
            "--cookies-from-browser",
            self.cookies_from_browser,
            "--cookies",
            str(cookie_file),
            "--skip-download",
            "--no-playlist",
            "--",
            self.url,
        ]
        result = run_logged(command, self.job_dir / "cookies.log", "COOKIES")
        normalized_output = result.output.lower()
        if "failed to decrypt with dpapi" in normalized_output:
            if cookie_file.exists():
                cookie_file.unlink()
            raise PipelineError(
                "The browser cookie database was found, but Windows DPAPI could not "
                "decrypt its cookies. This is a Chromium cookie encryption issue, not "
                "a browser-lock issue. Use a dedicated Firefox session for Douyin and "
                "rerun with --cookies-from-browser firefox."
            )
        if "could not copy" in normalized_output and "cookie database" in normalized_output:
            if cookie_file.exists():
                cookie_file.unlink()
            raise PipelineError(
                "The browser cookie database is still locked. Close every window for "
                "that browser and make sure its background process is no longer running."
            )
        if not cookie_file.exists() or cookie_file.stat().st_size == 0:
            raise PipelineError(
                "Could not create a browser cookie snapshot. Check cookies.log for the "
                "browser-specific extraction error."
            )
        self.cookie_file = cookie_file
        if result.returncode != 0:
            cookie_branch = self.manifest.setdefault("cookie_snapshot", {})
            cookie_branch["warning"] = (
                f"Cookie snapshot command exited with code {result.returncode}, "
                "but a cookie file was created and will be tried."
            )

    def cleanup_cookie_snapshot(self) -> None:
        if self.cookie_file and self.cookie_file.exists():
            self.cookie_file.unlink()

    def download_video(self) -> Path:
        output_template = self.job_dir / "video.%(ext)s"
        command = build_video_command(
            self.yt_dlp,
            self.url,
            output_template,
            cookie_file=self.cookie_file,
        )
        result = run_logged(command, self.job_dir / "video.log", "VIDEO")
        if result.returncode != 0:
            raise PipelineError(f"Video download failed with exit code {result.returncode}")
        video_path = self.job_dir / "video.mp4"
        if not video_path.exists() or video_path.stat().st_size == 0:
            raise PipelineError("yt-dlp finished but video.mp4 was not created.")
        return video_path

    def download_platform_subtitles(self) -> Path | None:
        output_template = self.job_dir / "platform.%(ext)s"
        command = build_subtitle_command(
            self.yt_dlp,
            self.url,
            output_template,
            cookie_file=self.cookie_file,
        )
        result = run_logged(command, self.job_dir / "subtitles.log", "SUBTITLE")
        candidates = list(self.job_dir.glob("platform*.srt"))
        selected = choose_subtitle(candidates, self.language)
        if selected:
            return selected
        if result.returncode != 0:
            subtitle_branch = self.manifest["branches"]["subtitles"]  # type: ignore[index]
            subtitle_branch["probe_error"] = (
                f"Platform subtitle probe exited with code {result.returncode}; "
                "the pipeline will try Whisper."
            )
        return None

    def transcribe_with_whisper(self, video_path: Path) -> Path:
        if importlib.util.find_spec("whisper") is None:
            raise PipelineError(
                "No platform subtitle was found and openai-whisper is not installed. "
                "Run: py -m pip install -U openai-whisper"
            )
        command = [
            sys.executable,
            "-m",
            "whisper",
            str(video_path),
            "--model",
            self.whisper_model,
            "--task",
            "transcribe",
            "--output_format",
            "srt",
            "--output_dir",
            str(self.job_dir),
        ]
        if self.language.lower() != "auto":
            command.extend(["--language", self.language])
        result = run_logged(command, self.job_dir / "subtitles.log", "WHISPER")
        if result.returncode != 0:
            raise PipelineError(f"Whisper failed with exit code {result.returncode}")
        whisper_srt = self.job_dir / "video.srt"
        if not whisper_srt.exists() or whisper_srt.stat().st_size == 0:
            raise PipelineError("Whisper finished but video.srt was not created.")
        return whisper_srt

    def run(self) -> dict[str, object]:
        self.prepare()
        try:
            self.snapshot_browser_cookies()
        except Exception:
            self.cleanup_cookie_snapshot()
            raise
        video_path: Path | None = None
        platform_subtitle: Path | None = None
        video_error: str | None = None
        subtitle_probe_error: str | None = None

        with ThreadPoolExecutor(max_workers=2) as executor:
            video_future = executor.submit(self.download_video)
            subtitle_future = executor.submit(self.download_platform_subtitles)
            try:
                video_path = video_future.result()
            except Exception as exc:  # Preserve the subtitle branch and logs.
                video_error = str(exc)
            try:
                platform_subtitle = subtitle_future.result()
            except Exception as exc:  # Whisper may still recover after video download.
                subtitle_probe_error = str(exc)

        branches = self.manifest["branches"]  # type: ignore[assignment]
        video_branch = branches["video"]
        subtitle_branch = branches["subtitles"]

        if video_path:
            video_branch.update({"status": "READY", "path": str(video_path)})
        else:
            video_branch.update({"status": "FAILED", "error": video_error})

        canonical_srt = self.job_dir / "subtitles.srt"
        if platform_subtitle:
            shutil.copy2(platform_subtitle, canonical_srt)
            subtitle_branch.update(
                {
                    "status": "READY",
                    "path": str(canonical_srt),
                    "source": "platform",
                    "selected_platform_file": str(platform_subtitle),
                }
            )
        elif video_path:
            self.manifest["stage"] = "TRANSCRIBING_WITH_WHISPER"
            self.write_manifest()
            try:
                whisper_srt = self.transcribe_with_whisper(video_path)
                shutil.copy2(whisper_srt, canonical_srt)
                subtitle_branch.update(
                    {
                        "status": "READY",
                        "path": str(canonical_srt),
                        "source": f"whisper-{self.whisper_model}",
                        "platform_subtitle": "not_found",
                        "platform_subtitle_note": (
                            "No separate subtitle track was exposed by the platform. "
                            "Visible captions may be burned into the video pixels, so "
                            "the audio was transcribed with Whisper."
                        ),
                    }
                )
            except Exception as exc:
                subtitle_branch.update({"status": "FAILED", "error": str(exc)})
        else:
            subtitle_branch.update(
                {
                    "status": "FAILED",
                    "error": subtitle_probe_error
                    or "No platform subtitle was found and the MP4 branch failed, so ASR could not run.",
                }
            )

        if subtitle_probe_error:
            subtitle_branch["probe_error"] = subtitle_probe_error

        if video_branch["status"] == "READY" and subtitle_branch["status"] == "READY":
            create_review_artifacts(self.job_dir, canonical_srt)
            subtitle_branch["plain_text_path"] = str(self.job_dir / "transcript.txt")
            subtitle_branch["review_guide_path"] = str(self.job_dir / "subtitle-review.md")
            self.manifest["stage"] = "WAITING_SUBTITLE_REVIEW"
            self.manifest["completed_at"] = utc_now()
        else:
            self.manifest["stage"] = "PARTIAL_FAILURE"

        self.write_manifest()
        self.cleanup_cookie_snapshot()
        return self.manifest
