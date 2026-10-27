"""Batch audio window extraction and local faster-whisper transcription.

Every goal, every pre-match window and every post-game window of the whole
minibattle is cut with ffmpeg (direct executable, no shell) and transcribed in
one Whisper session.  The opening 3-minute roll-call is transcribed first so
the roster module can resolve controller numbers, then the remaining windows
are transcribed with roster-aware hotwords using the same loaded model.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import config

log = logging.getLogger("goal3.audio")


class AudioProcessingError(RuntimeError):
    pass


@dataclass
class AudioWindow:
    key: str
    kind: str
    start: float
    end: float
    match_num: int | None = None
    goal_index: int | None = None
    path: Path | None = None
    transcript: dict | None = None

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def _safe_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:120]


def _check_ffmpeg() -> str:
    binary = config.FFMPEG_BIN
    if Path(binary).exists():
        return binary
    found = shutil.which(binary)
    if not found:
        raise AudioProcessingError(
            "ffmpeg was not found. Put ffmpeg.exe in PATH or set GOAL3_FFMPEG "
            "to its absolute path."
        )
    return found


def probe_duration(audio_path: str | Path) -> float:
    """Return media duration with ffprobe, falling back to 0.0."""
    binary = config.FFPROBE_BIN
    if not Path(binary).exists():
        found = shutil.which(binary)
        if not found:
            return 0.0
        binary = found
    command = [
        binary,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ffprobe failed: %s", exc)
        return 0.0
    if result.returncode != 0:
        log.warning("ffprobe returned %s: %s", result.returncode, result.stderr.strip())
        return 0.0
    try:
        return max(0.0, float(result.stdout.strip().splitlines()[0]))
    except (ValueError, IndexError):
        return 0.0


class AudioSlicer:
    """Cut and transcribe all audio windows of one minibattle."""

    def __init__(
        self,
        audio_path: str | Path,
        video_id: str,
        video_duration: float = 0.0,
    ):
        self.audio_path = Path(audio_path)
        if not self.audio_path.exists():
            raise AudioProcessingError(f"Audio file not found: {self.audio_path}")
        self.video_id = video_id
        self.video_duration = float(video_duration or 0.0)
        if self.video_duration <= 0:
            self.video_duration = probe_duration(self.audio_path)
        self.ffmpeg = _check_ffmpeg()
        self.work_dir = config.TEMP_DIR / f"{_safe_key(video_id)}_audio_{int(time.time())}"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.windows: list[AudioWindow] = []
        self._model = None
        self._model_loaded_at: float | None = None

    # ------------------------------------------------------------------
    # Window construction
    # ------------------------------------------------------------------
    def build_windows(
        self,
        matches: Sequence[dict],
        opening_start_sec: float = 0.0,
    ) -> list[AudioWindow]:
        windows: list[AudioWindow] = []
        if self.video_duration <= 0:
            self.video_duration = probe_duration(self.audio_path)
        opening_start = max(0.0, float(opening_start_sec or 0.0))
        if self.video_duration > 0:
            opening_start = min(opening_start, max(0.0, self.video_duration - 1.0))
        opening_end = opening_start + config.OPENING_AUDIO_SEC
        if self.video_duration > 0:
            opening_end = min(opening_end, self.video_duration)
        windows.append(AudioWindow("opening", "opening", opening_start, opening_end))
        for match in matches:
            num = int(match["match_num_in_minibattle"])
            start = float(match.get("start_video_sec", 0))
            end = float(match.get("end_video_sec", start))
            pre_start = max(0.0, start + config.PREMATCH_WINDOW[0])
            pre_end = min(start + config.PREMATCH_WINDOW[1], start)
            if pre_end > pre_start:
                windows.append(
                    AudioWindow(f"m{num}_pre", "prematch", pre_start, pre_end, match_num=num)
                )
            ordered_goals = sorted(
                match.get("goals") or [],
                key=lambda goal: (float(goal.get("video_sec", 0)), goal.get("scoring_side", "")),
            )
            for goal_index, goal in enumerate(ordered_goals):
                goal_sec = float(goal.get("video_sec", start))
                g_start = max(0.0, goal_sec + config.GOAL_WINDOW[0])
                g_end = goal_sec + config.GOAL_WINDOW[1]
                if self.video_duration > 0:
                    g_end = min(g_end, self.video_duration)
                if g_end > g_start:
                    windows.append(
                        AudioWindow(
                            f"m{num}_g{goal_index}",
                            "goal",
                            g_start,
                            g_end,
                            match_num=num,
                            goal_index=goal_index,
                        )
                    )
            post_start = max(0.0, end + config.POSTGAME_WINDOW[0])
            post_end = end + config.POSTGAME_WINDOW[1]
            if self.video_duration > 0:
                post_end = min(post_end, self.video_duration)
            if post_end > post_start:
                windows.append(
                    AudioWindow(f"m{num}_post", "postgame", post_start, post_end, match_num=num)
                )
        self.windows = windows
        log.info("Built %d audio windows for minibattle %s", len(windows), self.video_id)
        return windows

    # ------------------------------------------------------------------
    # Cutting
    # ------------------------------------------------------------------
    def cut_window(self, window: AudioWindow) -> Path:
        output = self.work_dir / f"{window.key}.wav"
        output.parent.mkdir(parents=True, exist_ok=True)
        command = [
            self.ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-ss", f"{window.start:.3f}",
            "-i", str(self.audio_path),
            "-t", f"{window.duration:.3f}",
            "-vn",
            "-ac", str(config.AUDIO_CHANNELS),
            "-ar", str(config.AUDIO_SAMPLE_RATE),
            "-c:a", "pcm_s16le",
            str(output),
        ]
        log.debug("ffmpeg cut %s: %.1f-%.1f", window.key, window.start, window.end)
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AudioProcessingError(f"ffmpeg could not cut {window.key}: {exc}") from exc
        if result.returncode != 0:
            raise AudioProcessingError(
                f"ffmpeg failed for {window.key} (rc={result.returncode}): {result.stderr.strip()}"
            )
        if not output.exists() or output.stat().st_size == 0:
            raise AudioProcessingError(f"ffmpeg produced an empty file for {window.key}")
        window.path = output
        return output

    def cut_all(self, windows: Sequence[AudioWindow] | None = None) -> None:
        targets = list(windows if windows is not None else self.windows)
        for index, window in enumerate(targets, start=1):
            if window.path is None:
                try:
                    self.cut_window(window)
                except AudioProcessingError as exc:
                    # A single broken window must not lose the whole
                    # minibattle; it becomes an empty transcript and forces
                    # needs_review in the worker's verification gate.
                    log.error("Audio window %s could not be cut: %s", window.key, exc)
                    window.transcript = self._empty_transcript(window, error=str(exc))
            if index % 10 == 0 or index == len(targets):
                log.info("Audio cut %d/%d", index, len(targets))

    # ------------------------------------------------------------------
    # Whisper
    # ------------------------------------------------------------------
    def _load_model(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise AudioProcessingError(
                "faster-whisper is not installed. Run: pip install -r requirements.txt"
            ) from exc
        log.info(
            "Loading faster-whisper model=%s device=%s compute=%s",
            config.WHISPER_MODEL,
            config.WHISPER_DEVICE,
            config.WHISPER_COMPUTE_TYPE,
        )
        started = time.time()
        try:
            kwargs = {
                "device": config.WHISPER_DEVICE,
                "compute_type": config.WHISPER_COMPUTE_TYPE,
            }
            if getattr(config, "WHISPER_LOCAL_ONLY", False):
                kwargs["local_files_only"] = True
            self._model = WhisperModel(config.WHISPER_MODEL, **kwargs)
        except Exception as exc:
            raise AudioProcessingError(f"Could not load faster-whisper model: {exc}") from exc
        self._model_loaded_at = time.time()
        log.info("Whisper model loaded in %.1fs", time.time() - started)
        return self._model

    @staticmethod
    def _build_prompt(
        roster_players: Sequence[dict] | None = None,
        include_slang: bool = True,
    ) -> tuple[str, str]:
        names: list[str] = []
        for player in roster_players or []:
            for value in [player.get("canonical_name"), *(player.get("aliases") or [])]:
                value = (value or "").strip()
                if value and value not in names:
                    names.append(value)
        countries = list(config.COUNTRY_CANONICAL_NAMES)
        prompt_parts = [
            "Комментарий минибатла по футболу Goal 3 на NES.",
            "Игроки: " + ", ".join(names[:40]) + "." if names else "",
            "Сборные: " + ", ".join(countries[:20]) + ".",
        ]
        if include_slang:
            prompt_parts.append("Сленг: " + config.WHISPER_SLANG + ".")
        prompt = " ".join(part for part in prompt_parts if part)
        slang_terms = [
            term.strip()
            for term in config.WHISPER_SLANG.replace(";", ",").split(",")
            if term.strip()
        ]
        hotwords = ", ".join(names[:40] + countries[:20] + slang_terms)
        return prompt, hotwords

    def transcribe_window(
        self,
        window: AudioWindow,
        roster_players: Sequence[dict] | None = None,
    ) -> dict:
        if window.path is None or not Path(window.path).exists():
            raise AudioProcessingError(f"Audio window is not cut: {window.key}")
        model = self._load_model()
        prompt, hotwords = self._build_prompt(roster_players)
        kwargs = {
            "language": config.WHISPER_LANGUAGE,
            "beam_size": config.WHISPER_BEAM_SIZE,
            "vad_filter": config.WHISPER_VAD_FILTER,
            "vad_parameters": {"min_silence_duration_ms": config.WHISPER_MIN_SILENCE_MS},
            "word_timestamps": config.WHISPER_WORD_TIMESTAMPS,
            "initial_prompt": prompt,
            "condition_on_previous_text": False,
        }
        try:
            segments_iter, info = model.transcribe(str(window.path), hotwords=hotwords, **kwargs)
        except TypeError:
            # Older faster-whisper builds do not expose hotwords; the prompt
            # still contains every player name.
            segments_iter, info = model.transcribe(str(window.path), **kwargs)
        segments: list[dict] = []
        words_count = 0
        pieces: list[str] = []
        try:
            for segment in segments_iter:
                text = (segment.text or "").strip()
                if not text:
                    continue
                start_abs = window.start + float(segment.start)
                end_abs = window.start + float(segment.end)
                words = []
                if getattr(segment, "words", None):
                    for word in segment.words:
                        word_text = (word.word or "").strip()
                        if word_text:
                            words.append(
                                {
                                    "start": window.start + float(word.start),
                                    "end": window.start + float(word.end),
                                    "text": word_text,
                                }
                            )
                            words_count += 1
                if not words:
                    words_count += len(text.split())
                segments.append(
                    {
                        "start": round(start_abs, 3),
                        "end": round(end_abs, 3),
                        "relative_start": round(float(segment.start), 3),
                        "relative_end": round(float(segment.end), 3),
                        "text": text,
                        "words": words,
                    }
                )
                pieces.append(text)
        except Exception as exc:
            log.error("Whisper transcription failed for %s: %s", window.key, exc)
            return self._empty_transcript(window, error=str(exc))
        duration = max(0.001, window.duration)
        result = {
            "key": window.key,
            "kind": window.kind,
            "match_num": window.match_num,
            "goal_index": window.goal_index,
            "start": round(window.start, 3),
            "end": round(window.end, 3),
            "duration": round(window.duration, 3),
            "text": " ".join(pieces).strip(),
            "segments": segments,
            "word_count": words_count,
            "words_per_min": round(words_count / duration * 60.0, 2),
            "language": getattr(info, "language", config.WHISPER_LANGUAGE),
            "language_probability": round(float(getattr(info, "language_probability", 0.0) or 0.0), 3),
            "error": None,
        }
        window.transcript = result
        log.info(
            "Transcribed %s (%.1fs): %d words, %.0f wpm",
            window.key,
            window.duration,
            words_count,
            result["words_per_min"],
        )
        return result

    @staticmethod
    def _empty_transcript(window: AudioWindow, error: str) -> dict:
        result = {
            "key": window.key,
            "kind": window.kind,
            "match_num": window.match_num,
            "goal_index": window.goal_index,
            "start": window.start,
            "end": window.end,
            "duration": window.duration,
            "text": "",
            "segments": [],
            "word_count": 0,
            "words_per_min": 0.0,
            "language": config.WHISPER_LANGUAGE,
            "language_probability": 0.0,
            "error": error,
        }
        window.transcript = result
        return result

    def transcribe(
        self,
        windows: Sequence[AudioWindow] | None = None,
        roster_players: Sequence[dict] | None = None,
    ) -> dict[str, dict]:
        targets = list(windows if windows is not None else self.windows)
        results: dict[str, dict] = {}
        for index, window in enumerate(targets, start=1):
            if window.transcript is not None and not window.transcript.get("error"):
                results[window.key] = window.transcript
                continue
            if window.path is None or not Path(window.path).exists():
                results[window.key] = (
                    window.transcript
                    if window.transcript and window.transcript.get("error")
                    else self._empty_transcript(window, error="audio window was not extracted")
                )
                continue
            results[window.key] = self.transcribe_window(window, roster_players)
            if index % 10 == 0 or index == len(targets):
                log.info("Whisper windows %d/%d", index, len(targets))
        return results

    def opening_results(self) -> list[dict]:
        return [w.transcript for w in self.windows if w.kind == "opening" and w.transcript]

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    def cleanup(self) -> None:
        for window in self.windows:
            if window.path and Path(window.path).exists():
                try:
                    Path(window.path).unlink()
                except OSError:
                    pass
        try:
            if self.work_dir.exists():
                shutil.rmtree(self.work_dir, ignore_errors=True)
        except OSError:
            pass
        self._model = None

    def close(self) -> None:
        self.cleanup()


def process_minibattle_audio(
    audio_path: str | Path,
    video_id: str,
    matches: Sequence[dict],
    video_duration: float,
    roster_callback=None,
    opening_start_sec: float = 0.0,
) -> tuple[dict[str, dict], dict | None, AudioSlicer]:
    """Convenience two-phase helper used by the worker.

    The opening window is transcribed first; if roster_callback is provided it
    receives the opening transcript and may return a roster, which is then used
    for hotwords on every remaining window.  The loaded model is reused.
    """
    slicer = AudioSlicer(audio_path, video_id, video_duration)
    try:
        windows = slicer.build_windows(matches, opening_start_sec=opening_start_sec)
        slicer.cut_all(windows)
        opening = [w for w in windows if w.kind == "opening"]
        opening_results = slicer.transcribe(opening)
        roster = roster_callback(opening_results) if roster_callback else None
        players = roster.get("players") if roster else None
        rest = [w for w in windows if w.kind != "opening"]
        results = dict(opening_results)
        if rest:
            results.update(slicer.transcribe(rest, roster_players=players))
        return results, roster, slicer
    except BaseException:
        # Includes KeyboardInterrupt: remove all temporary audio clips.
        slicer.cleanup()
        raise
