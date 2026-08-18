from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .review_cli import update_manifest
from .subtitles import (
    SubtitleCue,
    SubtitleReviewError,
    read_srt,
    write_plain_text,
    write_srt,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def choose_candidate(job_dir: Path, candidate_name: str = "") -> Path:
    if candidate_name:
        candidate = job_dir / candidate_name
    elif (job_dir / "subtitles.ai.srt").exists():
        candidate = job_dir / "subtitles.ai.srt"
    else:
        candidate = job_dir / "subtitles.srt"
    if not candidate.exists():
        raise SubtitleReviewError(f"Review candidate was not found: {candidate}")
    return candidate


def load_review_pair(
    job_dir: Path,
    candidate_name: str = "",
) -> tuple[list[SubtitleCue], list[SubtitleCue], Path]:
    original_path = job_dir / "subtitles.srt"
    candidate_path = choose_candidate(job_dir, candidate_name)
    original = read_srt(original_path)
    candidate = read_srt(candidate_path)
    original_timing = [(cue.index, cue.start, cue.end) for cue in original]
    candidate_timing = [(cue.index, cue.start, cue.end) for cue in candidate]
    if original_timing != candidate_timing:
        raise SubtitleReviewError(
            "The candidate changed cue IDs or timestamps. Review cannot continue safely."
        )
    return original, candidate, candidate_path


def save_review_draft(
    job_dir: Path,
    cues: list[SubtitleCue],
    candidate_path: Path,
) -> Path:
    # Never edit the original Whisper/platform subtitle in place.
    if candidate_path.name == "subtitles.srt":
        candidate_path = job_dir / "subtitles.review.srt"
    write_srt(candidate_path, cues)
    transcript_name = (
        "transcript.ai.txt"
        if candidate_path.name == "subtitles.ai.srt"
        else "transcript.review.txt"
    )
    write_plain_text(job_dir / transcript_name, cues)
    update_manifest(
        job_dir,
        {
            "stage": "WAITING_SUBTITLE_REVIEW",
            "subtitle_review": {
                "status": "PENDING",
                "draft": str(candidate_path),
                "last_saved_at": utc_now(),
            },
        },
    )
    return candidate_path


def approve_review(
    job_dir: Path,
    cues: list[SubtitleCue],
    candidate_path: Path,
) -> Path:
    candidate_path = save_review_draft(job_dir, cues, candidate_path)
    approved_srt = job_dir / "subtitles.approved.srt"
    write_srt(approved_srt, cues)
    write_plain_text(job_dir / "transcript.approved.txt", cues)
    update_manifest(
        job_dir,
        {
            "stage": "SRT_APPROVED",
            "subtitle_review": {
                "status": "APPROVED",
                "source": str(candidate_path),
                "approved_srt": str(approved_srt),
                "approved_at": utc_now(),
            },
        },
    )
    return approved_srt


def reject_review(
    job_dir: Path,
    cues: list[SubtitleCue],
    candidate_path: Path,
) -> Path:
    candidate_path = save_review_draft(job_dir, cues, candidate_path)
    update_manifest(
        job_dir,
        {
            "stage": "WAITING_SUBTITLE_REVIEW",
            "subtitle_review": {
                "status": "REJECTED",
                "rejected_draft": str(candidate_path),
                "rejected_at": utc_now(),
            },
        },
    )
    return candidate_path


class SubtitleReviewWindow:
    def __init__(
        self,
        job_dir: Path,
        original: list[SubtitleCue],
        candidate: list[SubtitleCue],
        candidate_path: Path,
    ) -> None:
        import tkinter as tk
        from tkinter import messagebox, ttk

        self.tk = tk
        self.messagebox = messagebox
        self.ttk = ttk
        self.job_dir = job_dir
        self.original = original
        self.candidate = list(candidate)
        self.candidate_path = candidate_path
        self.current_position: int | None = None
        self.changed_positions = [
            position
            for position, (left, right) in enumerate(zip(original, candidate))
            if left.text != right.text
        ]

        self.root = tk.Tk()
        self.root.title("Video Content Agent - 字幕人工审核")
        self.root.geometry("1120x720")
        self.root.minsize(900, 600)
        self.root.protocol("WM_DELETE_WINDOW", self.close_without_decision)

        self.status = tk.StringVar()
        self._build_layout()
        self._populate_list()
        self.cue_list.selection_set(0)
        self.cue_list.activate(0)
        self._load_position(0)

    def _build_layout(self) -> None:
        tk = self.tk
        ttk = self.ttk
        header = ttk.Frame(self.root, padding=12)
        header.pack(fill="x")
        ttk.Label(
            header,
            text="字幕人工审核",
            font=("Microsoft YaHei UI", 16, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            header,
            text=(
                "左侧带 ★ 的条目是 AI 修改项。逐条核对后，可批准、拒绝或保存稍后继续。"
            ),
            font=("Microsoft YaHei UI", 10),
        ).pack(anchor="w", pady=(4, 0))
        ttk.Label(header, textvariable=self.status).pack(anchor="w", pady=(4, 0))

        content = ttk.Panedwindow(self.root, orient="horizontal")
        content.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        list_frame = ttk.Frame(content)
        self.cue_list = tk.Listbox(
            list_frame,
            width=34,
            exportselection=False,
            font=("Consolas", 10),
        )
        list_scroll = ttk.Scrollbar(
            list_frame,
            orient="vertical",
            command=self.cue_list.yview,
        )
        self.cue_list.configure(yscrollcommand=list_scroll.set)
        self.cue_list.pack(side="left", fill="both", expand=True)
        list_scroll.pack(side="right", fill="y")
        self.cue_list.bind("<<ListboxSelect>>", self.on_select)
        content.add(list_frame, weight=1)

        editor = ttk.Frame(content, padding=(12, 0, 0, 0))
        ttk.Label(
            editor,
            text="原始字幕（只读）",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="w")
        self.original_text = tk.Text(
            editor,
            height=7,
            wrap="word",
            font=("Microsoft YaHei UI", 14),
            background="#f2f2f2",
        )
        self.original_text.pack(fill="x", pady=(4, 14))
        self.original_text.configure(state="disabled")

        ttk.Label(
            editor,
            text="审核稿（可以直接修改）",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="w")
        self.candidate_text = tk.Text(
            editor,
            height=9,
            wrap="word",
            font=("Microsoft YaHei UI", 14),
        )
        self.candidate_text.pack(fill="both", expand=True, pady=(4, 12))

        navigation = ttk.Frame(editor)
        navigation.pack(fill="x")
        ttk.Button(navigation, text="上一条", command=self.previous).pack(side="left")
        ttk.Button(navigation, text="下一条", command=self.next).pack(side="left", padx=6)
        ttk.Button(
            navigation,
            text="下一个 AI 修改项",
            command=self.next_changed,
        ).pack(side="left")
        ttk.Button(navigation, text="播放视频", command=self.open_video).pack(side="right")
        content.add(editor, weight=3)

        actions = ttk.Frame(self.root, padding=(12, 4, 12, 12))
        actions.pack(fill="x")
        ttk.Button(
            actions,
            text="保存草稿，稍后处理",
            command=self.save_only,
        ).pack(side="left")
        ttk.Button(
            actions,
            text="拒绝 AI 草稿",
            command=self.reject,
        ).pack(side="right")
        ttk.Button(
            actions,
            text="批准并进入下一步",
            command=self.approve,
        ).pack(side="right", padx=8)

    def _populate_list(self) -> None:
        changed = set(self.changed_positions)
        self.cue_list.delete(0, self.tk.END)
        for position, cue in enumerate(self.candidate):
            marker = "★" if position in changed else " "
            self.cue_list.insert(
                self.tk.END,
                f"{marker} {cue.index:03d}  {cue.start[:8]}  {cue.text[:12]}",
            )

    def _save_current_to_memory(self) -> None:
        if self.current_position is None:
            return
        text = self.candidate_text.get("1.0", "end-1c").strip()
        if text:
            self.candidate[self.current_position] = replace(
                self.candidate[self.current_position],
                text=text,
            )

    def _load_position(self, position: int) -> None:
        self._save_current_to_memory()
        self.current_position = position
        original = self.original[position]
        candidate = self.candidate[position]

        self.original_text.configure(state="normal")
        self.original_text.delete("1.0", self.tk.END)
        self.original_text.insert("1.0", original.text)
        self.original_text.configure(state="disabled")

        self.candidate_text.delete("1.0", self.tk.END)
        self.candidate_text.insert("1.0", candidate.text)
        self.status.set(
            f"第 {position + 1}/{len(self.candidate)} 条 · "
            f"AI 修改 {len(self.changed_positions)} 条 · {candidate.start} → {candidate.end}"
        )

    def select_position(self, position: int) -> None:
        position = max(0, min(position, len(self.candidate) - 1))
        self.cue_list.selection_clear(0, self.tk.END)
        self.cue_list.selection_set(position)
        self.cue_list.activate(position)
        self.cue_list.see(position)
        self._load_position(position)

    def on_select(self, _event: object) -> None:
        selection = self.cue_list.curselection()
        if selection and selection[0] != self.current_position:
            self._load_position(selection[0])

    def previous(self) -> None:
        self.select_position((self.current_position or 0) - 1)

    def next(self) -> None:
        self.select_position((self.current_position or 0) + 1)

    def next_changed(self) -> None:
        current = self.current_position or 0
        for position in self.changed_positions:
            if position > current:
                self.select_position(position)
                return
        if self.changed_positions:
            self.select_position(self.changed_positions[0])

    def open_video(self) -> None:
        video = self.job_dir / "video.mp4"
        if not video.exists():
            self.messagebox.showerror("无法播放", f"找不到视频：\n{video}")
            return
        try:
            os.startfile(video)  # type: ignore[attr-defined]
        except OSError as exc:
            self.messagebox.showerror("无法播放", str(exc))

    def save_only(self) -> None:
        self._save_current_to_memory()
        self.candidate_path = save_review_draft(
            self.job_dir,
            self.candidate,
            self.candidate_path,
        )
        self.messagebox.showinfo(
            "已保存",
            f"草稿已保存：\n{self.candidate_path}\n\n任务仍等待审核。",
        )

    def approve(self) -> None:
        self._save_current_to_memory()
        if not self.messagebox.askyesno(
            "确认批准",
            "确认已经核对字幕，并批准它进入全文整理阶段吗？",
        ):
            return
        approved = approve_review(
            self.job_dir,
            self.candidate,
            self.candidate_path,
        )
        self.messagebox.showinfo(
            "审核通过",
            f"已生成最终字幕：\n{approved}\n\n任务状态：SRT_APPROVED",
        )
        self.root.destroy()

    def reject(self) -> None:
        self._save_current_to_memory()
        if not self.messagebox.askyesno(
            "确认拒绝",
            "拒绝后会保留草稿，但任务不会进入全文整理。是否继续？",
        ):
            return
        rejected = reject_review(
            self.job_dir,
            self.candidate,
            self.candidate_path,
        )
        self.messagebox.showinfo(
            "已拒绝",
            f"草稿已保留：\n{rejected}\n\n你可以修改后再次打开审核窗口。",
        )
        self.root.destroy()

    def close_without_decision(self) -> None:
        self._save_current_to_memory()
        if self.messagebox.askyesno(
            "稍后处理",
            "关闭窗口且不作决定吗？未保存的当前修改不会写入文件。",
        ):
            self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Open the subtitle approval window.")
    parser.add_argument("--job-dir", required=True, help="Existing job directory")
    parser.add_argument(
        "--candidate",
        default="",
        help="Candidate SRT filename; defaults to subtitles.ai.srt when available",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate the review data without opening a window",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    job_dir = Path(args.job_dir).resolve()
    try:
        original, candidate, candidate_path = load_review_pair(job_dir, args.candidate)
        changed = sum(left.text != right.text for left, right in zip(original, candidate))
        if args.check:
            print(
                f"Review data OK: {len(candidate)} cues, {changed} changed, "
                f"candidate={candidate_path.name}"
            )
            return 0
        window = SubtitleReviewWindow(job_dir, original, candidate, candidate_path)
        window.run()
    except SubtitleReviewError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: Could not open the review window: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
