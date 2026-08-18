import json
import tempfile
import unittest
from pathlib import Path

from video_agent.pipeline import (
    PipelineError,
    build_subtitle_command,
    build_video_command,
    choose_subtitle,
    detect_platform,
)
from video_agent.review_gui import approve_review, load_review_pair, reject_review
from video_agent.subtitles import (
    SubtitleCue,
    SubtitleReviewError,
    _structured_output_text,
    parse_srt_text,
    render_plain_text,
    render_srt,
    write_plain_text,
    write_srt,
)


class PlatformDetectionTests(unittest.TestCase):
    def test_youtube_urls(self) -> None:
        self.assertEqual(detect_platform("https://youtu.be/abc"), "youtube")
        self.assertEqual(
            detect_platform("https://www.youtube.com/watch?v=abc"), "youtube"
        )

    def test_douyin_urls(self) -> None:
        self.assertEqual(detect_platform("https://v.douyin.com/abc/"), "douyin")
        self.assertEqual(
            detect_platform("https://www.douyin.com/video/123"), "douyin"
        )

    def test_rejects_other_platforms(self) -> None:
        with self.assertRaises(PipelineError):
            detect_platform("https://example.com/video")


class CommandBuilderTests(unittest.TestCase):
    def test_video_command_has_mp4_and_url_separator(self) -> None:
        command = build_video_command(
            "yt-dlp", "https://youtu.be/abc", Path("video.%(ext)s")
        )
        self.assertIn("--remux-video", command)
        self.assertEqual(command[-2:], ["--", "https://youtu.be/abc"])

    def test_subtitle_command_uses_browser_cookie_when_requested(self) -> None:
        command = build_subtitle_command(
            "yt-dlp",
            "https://v.douyin.com/abc/",
            Path("platform.%(ext)s"),
            "edge",
        )
        self.assertIn("--write-auto-subs", command)
        self.assertIn("--cookies-from-browser", command)
        self.assertIn("edge", command)

    def test_video_command_can_use_cookie_snapshot(self) -> None:
        command = build_video_command(
            "yt-dlp",
            "https://v.douyin.com/abc/",
            Path("video.%(ext)s"),
            cookie_file=Path("browser-cookies.txt"),
        )
        self.assertIn("--cookies", command)
        self.assertIn("browser-cookies.txt", command)
        self.assertNotIn("--cookies-from-browser", command)


class SubtitleSelectionTests(unittest.TestCase):
    def test_prefers_chinese_in_auto_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            english = root / "platform.en.srt"
            chinese = root / "platform.zh-Hans.srt"
            english.write_text("en", encoding="utf-8")
            chinese.write_text("zh", encoding="utf-8")
            self.assertEqual(choose_subtitle([english, chinese]), chinese)

    def test_respects_explicit_language(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            english = root / "platform.en.srt"
            chinese = root / "platform.zh.srt"
            english.write_text("en", encoding="utf-8")
            chinese.write_text("zh", encoding="utf-8")
            self.assertEqual(choose_subtitle([chinese, english], "en"), english)


class SubtitleReviewTests(unittest.TestCase):
    SAMPLE = (
        "1\n00:00:00,000 --> 00:00:01,000\n河南明明不靠海\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\n台风在海边登陆\n"
    )

    def test_parse_and_render_srt(self) -> None:
        cues = parse_srt_text(self.SAMPLE)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[1].text, "台风在海边登陆")
        self.assertEqual(render_srt(cues), self.SAMPLE)

    def test_plain_text_uses_only_spoken_lines(self) -> None:
        cues = parse_srt_text(self.SAMPLE)
        self.assertEqual(
            render_plain_text(cues),
            "河南明明不靠海\n台风在海边登陆\n",
        )

    def test_plain_text_file_has_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "transcript.txt"
            write_plain_text(
                path,
                [SubtitleCue(1, "00:00:00,000", "00:00:01,000", "河南")],
            )
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertEqual(path.read_text(encoding="utf-8-sig"), "河南\n")

    def test_extracts_structured_response_text(self) -> None:
        payload = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": '{"cues":[]}'}],
                }
            ]
        }
        self.assertEqual(_structured_output_text(payload), '{"cues":[]}')

    def test_rejects_invalid_srt(self) -> None:
        with self.assertRaises(SubtitleReviewError):
            parse_srt_text("not an srt")

    def test_interactive_approval_writes_approved_files_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            original = parse_srt_text(self.SAMPLE)
            candidate = [
                original[0],
                SubtitleCue(
                    2,
                    original[1].start,
                    original[1].end,
                    "台风在海边登陆了",
                ),
            ]
            write_srt(job / "subtitles.srt", original)
            write_srt(job / "subtitles.ai.srt", candidate)
            (job / "manifest.json").write_text("{}", encoding="utf-8")

            loaded_original, loaded_candidate, candidate_path = load_review_pair(job)
            approved = approve_review(job, loaded_candidate, candidate_path)

            self.assertEqual(len(loaded_original), 2)
            self.assertTrue(approved.exists())
            self.assertTrue((job / "transcript.approved.txt").exists())
            manifest = json.loads((job / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["stage"], "SRT_APPROVED")
            self.assertEqual(manifest["subtitle_review"]["status"], "APPROVED")

    def test_interactive_rejection_keeps_draft_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            cues = parse_srt_text(self.SAMPLE)
            write_srt(job / "subtitles.srt", cues)
            write_srt(job / "subtitles.ai.srt", cues)
            (job / "manifest.json").write_text("{}", encoding="utf-8")

            _, candidate, candidate_path = load_review_pair(job)
            rejected = reject_review(job, candidate, candidate_path)

            self.assertTrue(rejected.exists())
            manifest = json.loads((job / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["stage"], "WAITING_SUBTITLE_REVIEW")
            self.assertEqual(manifest["subtitle_review"]["status"], "REJECTED")


if __name__ == "__main__":
    unittest.main()
