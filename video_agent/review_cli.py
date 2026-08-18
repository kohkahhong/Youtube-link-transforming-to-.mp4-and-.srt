from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .subtitles import (
    SubtitleReviewError,
    correct_with_openai,
    create_review_artifacts,
    read_srt,
    write_correction_report,
    write_plain_text,
    write_srt,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create, AI-correct, or approve subtitle review artifacts."
    )
    parser.add_argument("--job-dir", required=True, help="Existing job output directory")
    parser.add_argument(
        "--input-srt",
        default="subtitles.srt",
        help="SRT filename inside the job directory",
    )
    parser.add_argument("--ai", action="store_true", help="Run optional AI correction")
    parser.add_argument("--approve", action="store_true", help="Approve the selected SRT")
    parser.add_argument("--reject", action="store_true", help="Reject the selected SRT draft")
    parser.add_argument("--model", default="gpt-5.4-mini", help="OpenAI correction model")
    parser.add_argument("--context", default="", help="Topic or proper-noun context")
    return parser


def update_manifest(job_dir: Path, updates: dict[str, object]) -> None:
    manifest_path = job_dir / "manifest.json"
    if not manifest_path.exists():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    manifest.update(updates)
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    args = build_parser().parse_args()
    if args.approve and args.reject:
        print("ERROR: Use either --approve or --reject, not both.", file=sys.stderr)
        return 2
    job_dir = Path(args.job_dir).resolve()
    source_srt = job_dir / args.input_srt
    try:
        cues = create_review_artifacts(job_dir, source_srt)
        if args.ai:
            corrected_cues, corrections = correct_with_openai(
                cues,
                model=args.model,
                context=args.context,
            )
            ai_srt = job_dir / "subtitles.ai.srt"
            write_srt(ai_srt, corrected_cues)
            write_plain_text(job_dir / "transcript.ai.txt", corrected_cues)
            write_correction_report(
                job_dir / "correction-report.md",
                corrections,
                model=args.model,
            )
            update_manifest(
                job_dir,
                {
                    "stage": "WAITING_AI_SUBTITLE_REVIEW",
                    "subtitle_review": {
                        "status": "PENDING",
                        "ai_draft": str(ai_srt),
                        "model": args.model,
                    },
                },
            )
            print(f"AI correction draft: {ai_srt}")
            print("Review it against the video before approving it.")

        if args.approve:
            approved_source = source_srt
            approved_cues = read_srt(approved_source)
            approved_srt = job_dir / "subtitles.approved.srt"
            write_srt(approved_srt, approved_cues)
            write_plain_text(job_dir / "transcript.approved.txt", approved_cues)
            update_manifest(
                job_dir,
                {
                    "stage": "SRT_APPROVED",
                    "subtitle_review": {
                        "status": "APPROVED",
                        "source": str(approved_source),
                        "approved_srt": str(approved_srt),
                    },
                },
            )
            print(f"Approved subtitle: {approved_srt}")

        if args.reject:
            update_manifest(
                job_dir,
                {
                    "stage": "WAITING_SUBTITLE_REVIEW",
                    "subtitle_review": {
                        "status": "REJECTED",
                        "rejected_draft": str(source_srt),
                        "rejected_at": datetime.now(timezone.utc).isoformat(),
                    },
                },
            )
            print(f"Rejected subtitle draft retained at: {source_srt}")

        if not args.ai and not args.approve and not args.reject:
            update_manifest(
                job_dir,
                {
                    "stage": "WAITING_SUBTITLE_REVIEW",
                    "subtitle_review": {
                        "status": "PENDING",
                        "source": str(source_srt),
                        "plain_text": str(job_dir / "transcript.txt"),
                    },
                },
            )
            print(f"Review files created in: {job_dir}")
            print("Open transcript.txt or subtitle-review.md next.")
    except SubtitleReviewError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
