from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .pipeline import MediaPipeline, PipelineError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate MP4 and SRT files from a YouTube or Douyin URL."
    )
    parser.add_argument("--url", required=True, help="YouTube or Douyin URL")
    parser.add_argument(
        "--output-root",
        default="jobs",
        help="Directory that will contain per-job output folders",
    )
    parser.add_argument(
        "--language",
        default="auto",
        help="Preferred subtitle/Whisper language, for example auto, zh, or en",
    )
    parser.add_argument(
        "--cookies-from-browser",
        choices=("edge", "chrome", "firefox"),
        default=None,
        help="Use login cookies from a supported local browser",
    )
    parser.add_argument(
        "--whisper-model",
        choices=("tiny", "base", "small", "medium", "turbo", "large"),
        default="base",
        help="Whisper model used when the platform has no separate subtitle track",
    )
    parser.add_argument(
        "--interactive-review",
        action="store_true",
        help="Open the local subtitle review window after MP4 and SRT are ready",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        pipeline = MediaPipeline(
            url=args.url,
            output_root=Path(args.output_root),
            language=args.language,
            cookies_from_browser=args.cookies_from_browser,
            whisper_model=args.whisper_model,
        )
        manifest = pipeline.run()
    except PipelineError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"UNEXPECTED ERROR: {exc}", file=sys.stderr)
        return 3

    print("\n=== TASK RESULT ===")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    if manifest.get("stage") == "WAITING_SUBTITLE_REVIEW":
        print("\nMP4 and SRT are ready. The next required action is subtitle review.")
        if args.interactive_review:
            job_dir = Path(args.output_root).resolve() / str(manifest["job_id"])
            review_script = Path(__file__).resolve().parents[1] / "review-window.ps1"
            subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Sta",
                    "-File",
                    str(review_script),
                    "-JobDir",
                    str(job_dir),
                ],
                check=False,
            )
        return 0
    print("\nThe job finished with a partial failure. Check manifest.json and log files.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
