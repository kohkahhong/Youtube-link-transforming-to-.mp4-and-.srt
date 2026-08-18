from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable


SRT_TIMING = re.compile(
    r"^(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+"
    r"(?P<end>\d{2}:\d{2}:\d{2},\d{3})(?:\s+.*)?$"
)


class SubtitleReviewError(RuntimeError):
    """Raised when a subtitle review artifact cannot be created safely."""


@dataclass(frozen=True)
class SubtitleCue:
    index: int
    start: str
    end: str
    text: str


@dataclass(frozen=True)
class SubtitleCorrection:
    index: int
    original: str
    corrected: str
    changed: bool
    reason: str
    confidence: str


def parse_srt_text(content: str) -> list[SubtitleCue]:
    normalized = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n{2,}", normalized.strip())
    cues: list[SubtitleCue] = []
    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 3:
            raise SubtitleReviewError(f"Invalid SRT block: {block!r}")
        try:
            index = int(lines[0].strip())
        except ValueError as exc:
            raise SubtitleReviewError(f"Invalid SRT cue number: {lines[0]!r}") from exc
        timing = SRT_TIMING.match(lines[1].strip())
        if not timing:
            raise SubtitleReviewError(f"Invalid SRT timing line: {lines[1]!r}")
        text = "\n".join(line.strip() for line in lines[2:] if line.strip()).strip()
        if not text:
            raise SubtitleReviewError(f"SRT cue {index} has no text.")
        cues.append(
            SubtitleCue(
                index=index,
                start=timing.group("start"),
                end=timing.group("end"),
                text=text,
            )
        )
    if not cues:
        raise SubtitleReviewError("The SRT file contains no subtitle cues.")
    return cues


def read_srt(path: Path) -> list[SubtitleCue]:
    try:
        return parse_srt_text(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise SubtitleReviewError(f"Subtitle file was not found: {path}") from exc
    except UnicodeDecodeError as exc:
        raise SubtitleReviewError(
            f"Subtitle file is not UTF-8. Re-save it as UTF-8 first: {path}"
        ) from exc


def render_srt(cues: Iterable[SubtitleCue]) -> str:
    blocks = [
        f"{cue.index}\n{cue.start} --> {cue.end}\n{cue.text}"
        for cue in cues
    ]
    return "\n\n".join(blocks) + "\n"


def render_plain_text(cues: Iterable[SubtitleCue]) -> str:
    return "\n".join(cue.text.replace("\n", " ") for cue in cues) + "\n"


def write_srt(path: Path, cues: Iterable[SubtitleCue]) -> None:
    # UTF-8 with BOM is broadly compatible with Windows editors and media players.
    path.write_text(render_srt(cues), encoding="utf-8-sig")


def write_plain_text(path: Path, cues: Iterable[SubtitleCue]) -> None:
    # The BOM prevents legacy Windows programs from guessing GBK for Chinese text.
    path.write_text(render_plain_text(cues), encoding="utf-8-sig")


def write_review_guide(path: Path, source_name: str) -> None:
    path.write_text(
        "# 字幕审核\n\n"
        f"原始字幕：`{source_name}`\n\n"
        "请重点检查：人名、地名、年份、数字、专业名词、同音字和断句。\n\n"
        "- `subtitles.srt`：原始字幕，不覆盖。\n"
        "- `transcript.txt`：无时间轴纯文本，UTF-8 with BOM。\n"
        "- `subtitles.ai.srt`：可选 AI 纠错草稿。\n"
        "- `subtitles.approved.srt`：人工确认后的最终字幕。\n\n"
        "画面中直接绘制的字幕属于烧录字幕，不是平台提供的独立字幕轨；"
        "下载器无法像普通 SRT 一样直接提取。Whisper 负责从声音转写，"
        "AI 纠错负责结合上下文修正常见同音字，但最终仍需人工抽查。\n",
        encoding="utf-8-sig",
    )


def create_review_artifacts(job_dir: Path, source_srt: Path) -> list[SubtitleCue]:
    cues = read_srt(source_srt)
    write_plain_text(job_dir / "transcript.txt", cues)
    write_review_guide(job_dir / "subtitle-review.md", source_srt.name)
    return cues


def _structured_output_text(payload: dict[str, object]) -> str:
    for item in payload.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    return text
    raise SubtitleReviewError("The AI response did not contain output text.")


def _correction_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "cues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "text": {"type": "string"},
                        "changed": {"type": "boolean"},
                        "reason": {"type": "string"},
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                        },
                    },
                    "required": ["id", "text", "changed", "reason", "confidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["cues"],
        "additionalProperties": False,
    }


def _call_openai_correction(
    cues: list[SubtitleCue],
    *,
    api_key: str,
    model: str,
    context: str,
    timeout: int = 180,
) -> list[SubtitleCorrection]:
    source = [{"id": cue.index, "text": cue.text} for cue in cues]
    request_payload = {
        "model": model,
        "store": False,
        "instructions": (
            "You are a meticulous Chinese subtitle proofreader. Correct only clear ASR "
            "errors using the full local context: homophones, malformed words, place names, "
            "people, dates, numbers, technical terms, and punctuation. Preserve the speaker's "
            "meaning, tone, cue IDs, cue count, and language. Do not summarize, add facts, or "
            "rewrite for style. If uncertain, keep the original wording and mark confidence low."
        ),
        "input": json.dumps(
            {
                "topic_context": context,
                "subtitle_cues": source,
            },
            ensure_ascii=False,
        ),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "subtitle_corrections",
                "strict": True,
                "schema": _correction_schema(),
            }
        },
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "video-content-agent/0.2",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SubtitleReviewError(
            f"OpenAI API returned HTTP {exc.code}: {body[:500]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise SubtitleReviewError(f"Could not reach the OpenAI API: {exc.reason}") from exc

    parsed = json.loads(_structured_output_text(response_payload))
    returned = parsed.get("cues")
    if not isinstance(returned, list):
        raise SubtitleReviewError("The AI response is missing its cues array.")

    by_id = {cue.index: cue for cue in cues}
    if {item.get("id") for item in returned if isinstance(item, dict)} != set(by_id):
        raise SubtitleReviewError(
            "The AI changed or omitted subtitle cue IDs; no corrected file was written."
        )

    corrections: list[SubtitleCorrection] = []
    for item in returned:
        if not isinstance(item, dict):
            raise SubtitleReviewError("The AI returned an invalid subtitle cue.")
        cue = by_id[item["id"]]
        corrected = str(item["text"]).strip()
        if not corrected:
            raise SubtitleReviewError(f"The AI returned empty text for cue {cue.index}.")
        corrections.append(
            SubtitleCorrection(
                index=cue.index,
                original=cue.text,
                corrected=corrected,
                changed=corrected != cue.text,
                reason=str(item["reason"]).strip(),
                confidence=str(item["confidence"]),
            )
        )
    return sorted(corrections, key=lambda correction: correction.index)


def correct_with_openai(
    cues: list[SubtitleCue],
    *,
    model: str,
    context: str = "",
    api_key: str | None = None,
    chunk_size: int = 80,
) -> tuple[list[SubtitleCue], list[SubtitleCorrection]]:
    key = api_key or os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise SubtitleReviewError(
            "OPENAI_API_KEY is not set. Create the review files without -UseAI, or set "
            "the key in the current PowerShell session before requesting AI correction."
        )
    all_corrections: list[SubtitleCorrection] = []
    for offset in range(0, len(cues), chunk_size):
        chunk = cues[offset : offset + chunk_size]
        all_corrections.extend(
            _call_openai_correction(
                chunk,
                api_key=key,
                model=model,
                context=context,
            )
        )
    corrected_by_id = {item.index: item.corrected for item in all_corrections}
    corrected_cues = [
        replace(cue, text=corrected_by_id[cue.index])
        for cue in cues
    ]
    return corrected_cues, all_corrections


def write_correction_report(
    path: Path,
    corrections: Iterable[SubtitleCorrection],
    *,
    model: str,
) -> None:
    changed = [item for item in corrections if item.changed]
    lines = [
        "# AI 字幕纠错报告",
        "",
        f"模型：`{model}`",
        "",
        "AI 版本是待审核草稿，不会覆盖原始字幕。低置信度修改务必对照视频。",
        "",
        f"共修改 {len(changed)} 条字幕。",
        "",
    ]
    for item in changed:
        lines.extend(
            [
                f"## 第 {item.index} 条（{item.confidence}）",
                "",
                f"- 原文：{item.original}",
                f"- 建议：{item.corrected}",
                f"- 原因：{item.reason or '语境纠错'}",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8-sig")
