"""Full-video OpenCV pass: scoreboard calibration, match segmentation, goals.

The module performs exactly one sequential scan over the downloaded minibattle
video.  It locates the calibrated scoreboard anchor at multiple scales, reads
both score digits with OpenCV template matching, confirms changes only after a
stability gate, segments the recording into matches, and saves the final
scoreboard screenshot of every match.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import cv2
import numpy as np

import config

log = logging.getLogger("goal3.cv")


class CalibrationError(RuntimeError):
    """Raised when the UI calibration is missing or incomplete."""


# ---------------------------------------------------------------------------
# Calibration handling
# ---------------------------------------------------------------------------
def _resolve_path(path_value: str | Path) -> Path:
    path = Path(path_value)
    if not path.is_absolute():
        path = (config.ROOT_DIR / path).resolve()
    return path


def load_calibration(path: str | Path | None = None) -> dict:
    calibration_path = Path(path or config.CALIBRATION_PATH)
    if not calibration_path.exists():
        raise CalibrationError(
            f"UI calibration not found: {calibration_path}. "
            "Run: python tools/calibrate_ui.py --video <video> --time 30"
        )
    try:
        data = json.loads(calibration_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrationError(f"Cannot read calibration {calibration_path}: {exc}") from exc
    required = ("anchor", "digits", "reference_size")
    missing = [key for key in required if key not in data]
    if missing:
        raise CalibrationError(f"Calibration is missing keys: {', '.join(missing)}")
    if not data.get("anchor_template"):
        raise CalibrationError("Calibration has no scoreboard anchor template")
    anchor_path = _resolve_path(data["anchor_template"])
    if not anchor_path.exists():
        raise CalibrationError(f"Anchor template does not exist: {anchor_path}")
    for side in ("left", "right"):
        if side not in data.get("digits", {}):
            raise CalibrationError(f"Calibration is missing the {side} score digit ROI")
    if not data.get("digit_templates"):
        raise CalibrationError(
            "Calibration has no digit templates. Capture digits 0-9 with "
            "tools/calibrate_ui.py --capture-digit N --video <video> --time <sec>."
        )
    data["_path"] = str(calibration_path)
    data["_anchor_path"] = str(anchor_path)
    return data


def preprocess_digit_image(image: np.ndarray, size: tuple[int, int] | None = None) -> np.ndarray:
    """Normalize a score digit crop for stable template matching."""
    if image is None or getattr(image, "size", 0) == 0:
        raise ValueError("Empty digit image")
    target = size or config.DIGIT_TEMPLATE_SIZE
    if image.ndim == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image
    gray = cv2.resize(gray, (int(target[0]), int(target[1])), interpolation=cv2.INTER_AREA)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    gray = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
    return gray


def save_anchor_template(image: np.ndarray, path: Path | None = None) -> Path:
    target = Path(path or config.ANCHOR_TEMPLATE_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(target), image):
        raise OSError(f"Could not write anchor template: {target}")
    return target


def _resize_keep(image: np.ndarray, width: int, height: int) -> np.ndarray:
    if image.shape[1] == width and image.shape[0] == height:
        return image
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)


# ---------------------------------------------------------------------------
# Anchor + score reader
# ---------------------------------------------------------------------------
@dataclass
class ScoreRead:
    left: int | None
    right: int | None
    confidence: float
    anchor_bbox: tuple[int, int, int, int] | None = None
    anchor_confidence: float = 0.0

    @property
    def score(self) -> tuple[int, int] | None:
        if self.left is None or self.right is None:
            return None
        return (int(self.left), int(self.right))


class AnchorScoreReader:
    def __init__(self, calibration: dict):
        self.calibration = calibration
        self.reference_size = tuple(int(v) for v in calibration["reference_size"][:2])
        self.anchor_template = cv2.imread(str(_resolve_path(calibration["anchor_template"])), cv2.IMREAD_GRAYSCALE)
        if self.anchor_template is None:
            raise CalibrationError("Could not read anchor template image")
        self.anchor_bbox = tuple(int(round(v)) for v in calibration["anchor"])
        self.scale = 1.0
        self.last_bbox: tuple[int, int, int, int] | None = None
        self.anchor_confidence = 0.0
        self._load_digit_templates()
        self._calibrated = False

    def _load_digit_templates(self) -> None:
        self.digit_templates: dict[int, list[np.ndarray]] = {}
        raw = self.calibration.get("digit_templates", {})
        for label in range(10):
            files = raw.get(str(label), raw.get(label, []))
            for file_value in files:
                path = _resolve_path(file_value)
                image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
                if image is None:
                    log.warning("Digit template is unreadable: %s", path)
                    continue
                if image.shape[::-1] != config.DIGIT_TEMPLATE_SIZE:
                    image = cv2.resize(image, config.DIGIT_TEMPLATE_SIZE, interpolation=cv2.INTER_AREA)
                self.digit_templates.setdefault(label, []).append(image)
        if not self.digit_templates:
            raise CalibrationError("No readable digit templates were found")

    # -- frame normalization -------------------------------------------------
    def normalize_frame(self, frame: np.ndarray) -> np.ndarray:
        width, height = self.reference_size
        return _resize_keep(frame, width, height)

    # -- anchor matching -----------------------------------------------------
    def _match_anchor(self, gray: np.ndarray, scales: Iterable[float], search_bbox=None) -> tuple[float, float, tuple[int, int, int, int]]:
        if search_bbox is None:
            sx, sy = 0, 0
            ex, ey = gray.shape[1], gray.shape[0]
        else:
            x, y, w, h = search_bbox
            sx = max(0, x)
            sy = max(0, y)
            ex = min(gray.shape[1], x + w)
            ey = min(gray.shape[0], y + h)
        region = gray[sy:ey, sx:ex]
        if region.size == 0:
            return -1.0, 1.0, (0, 0, 0, 0)
        best_score = -1.0
        best_scale = 1.0
        best_bbox = (0, 0, 0, 0)
        for scale in scales:
            tw = max(1, int(round(self.anchor_template.shape[1] * scale)))
            th = max(1, int(round(self.anchor_template.shape[0] * scale)))
            if tw > region.shape[1] or th > region.shape[0]:
                continue
            template = cv2.resize(self.anchor_template, (tw, th), interpolation=cv2.INTER_AREA)
            result = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            if max_val > best_score:
                best_score = float(max_val)
                best_scale = float(scale)
                best_bbox = (sx + int(max_loc[0]), sy + int(max_loc[1]), tw, th)
        return best_score, best_scale, best_bbox

    def calibrate_anchor(self, frames: Iterable[np.ndarray]) -> None:
        """Search all configured scales at the start of the video."""
        best = (-1.0, 1.0, (0, 0, 0, 0))
        for frame in frames:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            result = self._match_anchor(gray, config.ANCHOR_SCALES)
            if result[0] > best[0]:
                best = result
        if best[0] < config.ANCHOR_MATCH_THRESHOLD:
            # The stream layout is fixed; the calibrated absolute ROI is the
            # documented fallback and is still verified frame by frame.
            log.warning(
                "Anchor not found at start (best %.3f); using calibrated ROI %s",
                best[0],
                self.anchor_bbox,
            )
            self.scale = 1.0
            self.last_bbox = self.anchor_bbox
            self._calibrated = True
            return
        self.scale = best[1]
        self.last_bbox = best[2]
        self.anchor_confidence = best[0]
        self._calibrated = True
        log.info("Anchor calibrated: scale=%.3f bbox=%s confidence=%.3f", self.scale, self.last_bbox, best[0])

    def locate_anchor(self, frame: np.ndarray) -> tuple[tuple[int, int, int, int] | None, float]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if not self._calibrated or self.last_bbox is None:
            self.calibrate_anchor([frame])
        # Local search around the previous location.
        if self.last_bbox is not None:
            x, y, w, h = self.last_bbox
            pad = config.ANCHOR_SEARCH_PADDING
            local = (x - pad, y - pad, w + 2 * pad, h + 2 * pad)
            score, _, bbox = self._match_anchor(gray, (self.scale,), local)
            if score >= config.ANCHOR_MATCH_THRESHOLD:
                self.last_bbox = bbox
                self.anchor_confidence = score
                return bbox, score
        # Drift recovery: a few nearby scales over the calibrated or full area.
        scales = (max(0.5, self.scale - 0.05), self.scale, min(1.5, self.scale + 0.05))
        score, scale, bbox = self._match_anchor(gray, scales, self._calibrated_search_area(gray.shape))
        if score >= config.ANCHOR_MATCH_THRESHOLD:
            self.scale = scale
            self.last_bbox = bbox
            self.anchor_confidence = score
            return bbox, score
        self.anchor_confidence = max(0.0, float(score))
        return None, float(score)

    def _calibrated_search_area(self, shape: tuple[int, int]) -> tuple[int, int, int, int]:
        x, y, w, h = self.anchor_bbox
        margin_x = max(120, int(w * 1.5))
        margin_y = max(90, int(h * 1.5))
        return (x - margin_x, y - margin_y, w + 2 * margin_x, h + 2 * margin_y)

    # -- digit reading -------------------------------------------------------
    def _crop_relative_digit(self, frame: np.ndarray, bbox: tuple[int, int, int, int], side: str) -> np.ndarray | None:
        ax, ay, aw, ah = bbox
        rel = self.calibration["digits"][side]
        rx, ry, rw, rh = [float(v) for v in rel]
        x1 = int(round(ax + rx * aw))
        y1 = int(round(ay + ry * ah))
        x2 = int(round(ax + (rx + rw) * aw))
        y2 = int(round(ay + (ry + rh) * ah))
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(frame.shape[1], x2), min(frame.shape[0], y2)
        if x2 - x1 < 2 or y2 - y1 < 2:
            return None
        return frame[y1:y2, x1:x2]

    def read_digit(self, crop: np.ndarray) -> tuple[int | None, float]:
        processed = preprocess_digit_image(crop)
        best_label = None
        best_score = -1.0
        for label, templates in self.digit_templates.items():
            for template in templates:
                if template.shape != processed.shape:
                    template = cv2.resize(template, processed.shape[::-1], interpolation=cv2.INTER_AREA)
                result = cv2.matchTemplate(processed, template, cv2.TM_CCOEFF_NORMED)
                score = float(result[0, 0])
                if score > best_score:
                    best_score = score
                    best_label = label
        if best_score < config.DIGIT_MATCH_THRESHOLD:
            return None, max(0.0, best_score)
        return best_label, best_score

    def read(self, frame: np.ndarray) -> ScoreRead:
        frame = self.normalize_frame(frame)
        bbox, anchor_conf = self.locate_anchor(frame)
        if bbox is None:
            return ScoreRead(None, None, 0.0, None, anchor_conf)
        left_crop = self._crop_relative_digit(frame, bbox, "left")
        right_crop = self._crop_relative_digit(frame, bbox, "right")
        if left_crop is None or right_crop is None:
            return ScoreRead(None, None, 0.0, bbox, anchor_conf)
        left, left_conf = self.read_digit(left_crop)
        right, right_conf = self.read_digit(right_crop)
        confidence = min(left_conf, right_conf)
        return ScoreRead(left, right, confidence, bbox, anchor_conf)


# ---------------------------------------------------------------------------
# Field detection
# ---------------------------------------------------------------------------
class FieldDetector:
    def __init__(self, calibration: dict):
        self.calibration = calibration
        self.reference_size = tuple(int(v) for v in calibration["reference_size"][:2])

    def _game_roi(self, frame: np.ndarray) -> np.ndarray:
        roi = self.calibration.get("game")
        if not roi:
            h, w = frame.shape[:2]
            return frame[int(h * 0.10): int(h * 0.88), int(w * 0.06): int(w * 0.76)]
        x, y, w, h = [int(v) for v in roi]
        return frame[max(0, y): max(0, y + h), max(0, x): max(0, x + w)]

    def green_ratio(self, frame: np.ndarray) -> float:
        frame = _resize_keep(frame, *self.reference_size)
        roi = self._game_roi(frame)
        if roi.size == 0:
            return 0.0
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, np.array(config.FIELD_HSV_LOWER), np.array(config.FIELD_HSV_UPPER))
        return float(np.count_nonzero(mask)) / float(mask.shape[0] * mask.shape[1])


# ---------------------------------------------------------------------------
# Match segmentation and goals
# ---------------------------------------------------------------------------
@dataclass
class PendingScore:
    score: tuple[int, int]
    first_t: float
    last_t: float
    observations: int = 1


@dataclass
class ActiveMatch:
    num: int
    start_t: float
    last_active_t: float
    last_frame: np.ndarray
    confirmed: tuple[int, int] = (0, 0)
    last_read_t: float = 0.0
    last_read_confidence: float = 0.0
    goals: list[dict] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    pending: PendingScore | None = None
    ref_score: tuple[int, int] = (0, 0)

    def duration_at(self, t: float) -> float:
        return max(0.0, float(t) - float(self.start_t))


class MatchScanner:
    def __init__(
        self,
        reader: AnchorScoreReader,
        field_detector: FieldDetector,
        video_id: str,
        video_duration: float,
    ):
        self.reader = reader
        self.field_detector = field_detector
        self.video_id = video_id
        self.video_duration = float(video_duration or 0.0)
        self.matches: list[dict] = []
        self.active: ActiveMatch | None = None
        self.start_candidate_t: float | None = None
        self.start_counter = 0
        self.warnings: list[str] = []
        self.frames_sampled = 0
        self.frames_gameplay = 0
        self.last_time = 0.0
        self.gap_warned = False

    # -- event helpers -------------------------------------------------------
    def _confirm_score(self, new_score: tuple[int, int], timestamp: float, active: ActiveMatch) -> None:
        old = active.confirmed
        dl = new_score[0] - old[0]
        dr = new_score[1] - old[1]
        if dl < 0 or dr < 0:
            active.issues.append(
                f"Score regression at {config.hhmmss(timestamp)}: {old[0]}:{old[1]} -> {new_score[0]}:{new_score[1]}"
            )
            active.confirmed = new_score
            return
        total = dl + dr
        if total <= 0:
            return
        if total == 1:
            side = "LEFT" if dl == 1 else "RIGHT"
            self._append_goal(active, timestamp, side, new_score)
        else:
            # The game stores exactly one goal per +1 score change.  A larger
            # stable jump means at least one frame was missed; do not fabricate
            # goal events, but keep the score and flag the match for review.
            active.issues.append(
                f"Score jump at {config.hhmmss(timestamp)}: {old[0]}:{old[1]} -> {new_score[0]}:{new_score[1]} "
                "(only +1 changes are accepted as goals)"
            )
        active.confirmed = new_score
        active.pending = None

    def _append_goal(self, active: ActiveMatch, timestamp: float, side: str, score_after: tuple[int, int]) -> None:
        active.goals.append(
            {
                "video_sec": int(round(timestamp)),
                "video_timestamp_str": config.hhmmss(timestamp),
                "scoring_side": side,
                "score_left": int(score_after[0]),
                "score_right": int(score_after[1]),
            }
        )
        log.info("Match #%d goal at %s (%s) -> %d:%d", active.num, config.hhmmss(timestamp), side, *score_after)

    def _observe_score(self, read: ScoreRead, timestamp: float, active: ActiveMatch) -> None:
        if read.score is None:
            return
        if read.confidence <= config.SCORE_CONFIDENCE:
            return
        active.last_read_confidence = read.confidence
        active.last_read_t = timestamp
        raw = read.score
        active.ref_score = raw
        if raw == active.confirmed:
            active.pending = None
            return
        # A lower score than confirmed should never be accepted as a goal.
        if raw[0] < active.confirmed[0] or raw[1] < active.confirmed[1]:
            active.pending = None
            return
        if active.pending is None or active.pending.score != raw:
            active.pending = PendingScore(raw, timestamp, timestamp, 1)
            return
        active.pending.last_t = timestamp
        active.pending.observations += 1
        if (timestamp - active.pending.first_t) >= config.SCORE_STABILITY_SEC and active.pending.observations >= 3:
            self._confirm_score(raw, active.pending.first_t, active)

    def _start_match(self, timestamp: float, frame: np.ndarray) -> None:
        num = len(self.matches) + 1
        self.active = ActiveMatch(
            num=num,
            start_t=timestamp,
            last_active_t=timestamp,
            last_frame=frame.copy(),
            confirmed=(0, 0),
            ref_score=(0, 0),
        )
        self.start_candidate_t = None
        self.start_counter = 0
        log.info("Match #%d starts at %s", num, config.hhmmss(timestamp))

    def _finalize_match(self, timestamp: float) -> None:
        active = self.active
        if active is None:
            return
        end_t = max(active.start_t, min(active.last_active_t, self.video_duration or active.last_active_t))
        duration = max(0.0, end_t - active.start_t)
        screenshot = config.SCREENSHOTS_DIR / f"{self.video_id}_m{active.num}_final.jpg"
        try:
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            saved = cv2.imwrite(str(screenshot), active.last_frame)
            if not saved:
                active.issues.append("Could not write final scoreboard screenshot")
                screenshot_value = None
            else:
                screenshot_value = str(screenshot)
        except Exception as exc:  # pragma: no cover - filesystem failures
            active.issues.append(f"Screenshot error: {exc}")
            screenshot_value = None
        if duration < config.MIN_MATCH_DURATION_SEC:
            active.issues.append(f"Match is suspiciously short: {duration:.0f}s")
        goals_total = len(active.goals)
        score_total = sum(active.confirmed)
        if goals_total != score_total:
            active.issues.append(
                f"Goal count mismatch: detected={goals_total}, final score total={score_total}"
            )
        if active.last_read_t <= 0:
            active.issues.append("Final scoreboard was never read with enough confidence")
        if active.ref_score != active.confirmed:
            active.issues.append(
                "Final scoreboard change was not stable for the required "
                f"{config.SCORE_STABILITY_SEC:.0f}s: stable={active.confirmed}, last_read={active.ref_score}"
            )
        match = {
            "match_num_in_minibattle": active.num,
            "start_video_sec": int(round(active.start_t)),
            "end_video_sec": int(round(end_t)),
            "duration_sec": int(round(duration)),
            "score_left": int(active.confirmed[0]),
            "score_right": int(active.confirmed[1]),
            "final_score_crop_path": screenshot_value,
            "goals": sorted(active.goals, key=lambda g: (g["video_sec"], g["scoring_side"])),
            "needs_review": bool(active.issues),
            "issues": list(active.issues),
            "last_read_confidence": round(active.last_read_confidence, 3),
        }
        self.matches.append(match)
        log.info(
            "Match #%d ends at %s (%ds) score=%d:%d goals=%d%s",
            active.num,
            config.hhmmss(end_t),
            int(duration),
            active.confirmed[0],
            active.confirmed[1],
            len(active.goals),
            " [REVIEW]" if active.issues else "",
        )
        self.active = None

    # -- main loop -----------------------------------------------------------
    def process_frame(self, frame: np.ndarray, timestamp: float, frame_index: int) -> None:
        self.frames_sampled += 1
        self.last_time = timestamp
        frame = self.reader.normalize_frame(frame)
        read = self.reader.read(frame)
        green = self.field_detector.green_ratio(frame)
        gameplay = read.anchor_bbox is not None and green >= config.FIELD_GREEN_RATIO_MIN
        if gameplay:
            self.frames_gameplay += 1

        if self.active is not None:
            gap = max(0.0, timestamp - self.active.last_active_t)
            duration = timestamp - self.active.start_t
            score_reset = (
                read.score is not None
                and read.confidence > config.SCORE_CONFIDENCE
                and read.score == (0, 0)
                and self.active.confirmed != (0, 0)
            )
            if gameplay:
                should_split_on_return = (
                    gap >= config.MATCH_END_GAP_SEC
                    and duration >= config.MATCH_FINALIZE_DURATION_SEC
                ) or score_reset
                if should_split_on_return:
                    self._finalize_match(self.active.last_active_t)
                else:
                    # Continue the same match through a short gap (half-time).
                    if gap > 0:
                        log.debug("Short gameplay gap %.1fs inside match #%d", gap, self.active.num)
            elif gap >= config.MAX_GAP_BEFORE_FINALIZE_SEC:
                self._finalize_match(self.active.last_active_t)

        if self.active is None:
            if gameplay and read.score == (0, 0) and read.confidence > config.SCORE_CONFIDENCE:
                if self.start_candidate_t is None:
                    self.start_candidate_t = timestamp
                    self.start_counter = 1
                else:
                    self.start_counter += 1
                    if (timestamp - self.start_candidate_t) >= config.MATCH_START_STABLE_SEC and self.start_counter >= 2:
                        self._start_match(self.start_candidate_t, frame)
            else:
                self.start_candidate_t = None
                self.start_counter = 0
            return

        if gameplay:
            self.active.last_active_t = timestamp
            self.active.last_frame = frame.copy()
            if read.score is not None:
                self._observe_score(read, timestamp, self.active)
        else:
            gap = timestamp - self.active.last_active_t
            if gap >= config.MAX_GAP_BEFORE_FINALIZE_SEC:
                self._finalize_match(self.active.last_active_t)

    def finish(self) -> None:
        if self.active is not None:
            self._finalize_match(self.active.last_active_t)


# ---------------------------------------------------------------------------
# Splitting a source video into logical minibattles
# ---------------------------------------------------------------------------
def _inter_match_gap(previous: dict, current: dict) -> float:
    return max(
        0.0,
        float(current.get("start_video_sec", 0))
        - float(previous.get("end_video_sec", previous.get("start_video_sec", 0))),
    )


def _best_gap_split(matches: list[dict], min_side: int) -> int | None:
    """Return the index of the strongest balanced match-list split."""
    n = len(matches)
    if n < 2 * min_side:
        return None
    best_index = None
    best_key = (-1.0, -1.0)
    for split in range(min_side, n - min_side + 1):
        left = split
        right = n - split
        balance = min(left, right) / max(left, right)
        if balance < config.SERIES_MIN_BALANCE_RATIO:
            continue
        gap = _inter_match_gap(matches[split - 1], matches[split])
        key = (gap, balance)
        if key > best_key:
            best_key = key
            best_index = split
    return best_index


def _enforce_max_matches(groups: list[list[dict]]) -> list[list[dict]]:
    result: list[list[dict]] = []
    for group in groups:
        pending = list(group)
        while len(pending) > config.MAX_MATCHES_PER_MINIBATTLE:
            split = _best_gap_split(
                pending, min_side=min(config.MIN_MATCHES_PER_SERIES, len(pending) // 2)
            )
            if split is None:
                split = max(1, len(pending) // 2)
            result.append(pending[:split])
            pending = pending[split:]
        if pending:
            result.append(pending)
    return result


def _merge_excess_groups(groups: list[list[dict]]) -> list[list[dict]]:
    """Keep the number of automatic minibattles within the configured cap."""
    while len(groups) > config.MAX_MINIBATTLES_PER_VIDEO:
        best_index = None
        best_gap = None
        for index in range(len(groups) - 1):
            gap = _inter_match_gap(groups[index][-1], groups[index + 1][0])
            if best_gap is None or gap < best_gap:
                best_gap = gap
                best_index = index
        if best_index is None:
            break
        groups[best_index] = groups[best_index] + groups.pop(best_index + 1)
    return groups


def split_matches_into_minibattles(
    matches: Sequence[dict],
    video_duration: float,
) -> list[dict]:
    """Split a full-video match list into one or more logical minibattles.

    Signals, in priority order:
      1. A gap between matches of at least SERIES_BREAK_GAP_SEC (default 10m).
      2. More than MAX_MATCHES_PER_MINIBATTLE (9) matches, split at the
         strongest balanced gap.
      3. A long source video (>= MULTI_MINIBATTLE_VIDEO_DURATION_SEC, default
         2h) with at least 2 * MIN_MATCHES_PER_SERIES matches and a balanced
         split point.

    The result is capped at MAX_MINIBATTLES_PER_VIDEO.  All heuristics are
    configurable and the split note is persisted with the source video.
    """
    ordered = sorted(matches, key=lambda item: float(item.get("start_video_sec", 0)))
    if not ordered:
        return []

    groups: list[list[dict]] = []
    current: list[dict] = []
    boundaries: list[str] = []
    for match in ordered:
        if current:
            gap = _inter_match_gap(current[-1], match)
            if gap >= config.SERIES_BREAK_GAP_SEC:
                groups.append(current)
                current = []
                boundaries.append(f"long break {gap / 60.0:.1f} min")
        current.append(match)
    if current:
        groups.append(current)

    if any(len(group) > config.MAX_MATCHES_PER_MINIBATTLE for group in groups):
        boundaries.append(
            f"more than {config.MAX_MATCHES_PER_MINIBATTLE} matches in a block"
        )
    groups = _enforce_max_matches(groups)
    if len(groups) == 1 and len(groups[0]) >= 2 * config.MIN_MATCHES_PER_SERIES:
        if float(video_duration or 0) >= config.MULTI_MINIBATTLE_VIDEO_DURATION_SEC:
            split = _best_gap_split(groups[0], min_side=config.MIN_MATCHES_PER_SERIES)
            if split is not None:
                split_gap = _inter_match_gap(groups[0][split - 1], groups[0][split])
                if split_gap >= config.SERIES_DURATION_SPLIT_MIN_GAP_SEC:
                    groups = [groups[0][:split], groups[0][split:]]
                    boundaries.append(
                        "long video duration heuristic "
                        f"(break {split_gap / 60.0:.1f} min)"
                    )
    groups = _merge_excess_groups(groups)
    # A merge can only join groups; re-apply the 9-match invariant without
    # dropping any source matches.
    groups = _enforce_max_matches(groups)

    result: list[dict] = []
    for series_index, group in enumerate(groups, start=1):
        local_matches = []
        for local_num, match in enumerate(group, start=1):
            copied = dict(match)
            copied["source_match_num"] = int(
                match.get("match_num_in_minibattle") or local_num
            )
            copied["match_num_in_minibattle"] = local_num
            local_matches.append(copied)
        result.append(
            {
                "series_index": series_index,
                "series_count": len(groups),
                "start_video_sec": int(local_matches[0].get("start_video_sec", 0)),
                "end_video_sec": int(local_matches[-1].get("end_video_sec", 0)),
                "match_count": len(local_matches),
                "matches": local_matches,
                "detection_reason": (
                    "; ".join(boundaries)
                    if len(groups) > 1 and boundaries
                    else "single logical minibattle"
                ),
            }
        )
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def _video_duration(cap: cv2.VideoCapture, fps: float, frame_count: float) -> float:
    duration = 0.0
    if fps and frame_count:
        duration = float(frame_count) / float(fps)
    if not math.isfinite(duration) or duration <= 0:
        duration = 0.0
    return duration


def analyze_video(
    video_path: str | Path,
    video_id: str,
    calibration_path: str | Path | None = None,
    max_seconds: float | None = None,
) -> dict:
    """Scan a full minibattle video and return every match plus all goals."""
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")
    calibration = load_calibration(calibration_path)
    reader = AnchorScoreReader(calibration)
    field_detector = FieldDetector(calibration)
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"OpenCV cannot open video: {video_path}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
    frame_count = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = _video_duration(cap, fps, frame_count)
    if max_seconds is not None:
        duration = min(duration, float(max_seconds)) if duration else float(max_seconds)
    step = max(1, int(round(fps / config.SCAN_FPS)))
    log.info(
        "CV scan: %s fps=%.2f duration=%s step=%d",
        video_path.name,
        fps,
        config.hhmmss(duration),
        step,
    )

    scanner = MatchScanner(reader, field_detector, video_id, duration)
    try:
        # Anchor calibration uses the first up-to-15 seconds of sampled frames.
        calibration_frames: list[np.ndarray] = []
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        probe_index = 0
        while len(calibration_frames) < 15:
            ok, frame = cap.read()
            if not ok:
                break
            if probe_index % step == 0:
                calibration_frames.append(reader.normalize_frame(frame))
            probe_index += 1
        if calibration_frames:
            reader.calibrate_anchor(calibration_frames)
        else:
            raise RuntimeError("Video contains no readable frames")

        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        frame_index = 0
        last_log = 0
        while True:
            # Grab (without colour conversion) is enough for the frames that
            # are not sampled at SCAN_FPS; this keeps a 1.5-hour scan practical.
            if frame_index % step != 0:
                if not cap.grab():
                    break
                frame_index += 1
                continue
            ok, frame = cap.read()
            if not ok:
                break
            timestamp = frame_index / fps if fps else float(frame_index)
            if max_seconds is not None and timestamp > float(max_seconds):
                break
            try:
                scanner.process_frame(frame, timestamp, frame_index)
            except Exception as exc:
                log.exception("Frame processing failed at %s", config.hhmmss(timestamp))
                scanner.warnings.append(f"Frame {frame_index} ({config.hhmmss(timestamp)}): {exc}")
            if int(timestamp) // 300 > last_log:
                last_log = int(timestamp) // 300
                log.info(
                    "CV progress %s / %s, matches=%d, gameplay=%d",
                    config.hhmmss(timestamp),
                    config.hhmmss(duration),
                    len(scanner.matches),
                    scanner.frames_gameplay,
                )
            frame_index += 1
    finally:
        # Always release the Windows file handle before the worker deletes the
        # temporary .mp4 in its own finally block.
        cap.release()
    scanner.finish()

    integrity_ok = bool(scanner.matches) and all(not m["needs_review"] for m in scanner.matches)
    if not scanner.matches:
        integrity_ok = False
        scanner.warnings.append("No matches were detected. Check UI calibration and field thresholds.")
    for match in scanner.matches:
        if match["goals"]:
            # The sum check is explicit even though goals are generated from
            # stable score deltas.
            if len(match["goals"]) != match["score_left"] + match["score_right"]:
                match["needs_review"] = True
                match["issues"].append("Integrity: sum of goals != final scoreboard")
                integrity_ok = False
    return {
        "video_id": video_id,
        "video_path": str(video_path),
        "duration_sec": round(duration, 2),
        "matches": scanner.matches,
        "warnings": scanner.warnings,
        "integrity_ok": integrity_ok,
        "frames_sampled": scanner.frames_sampled,
        "frames_gameplay": scanner.frames_gameplay,
        "scan_fps": config.SCAN_FPS,
        "calibration_path": str(calibration.get("_path", calibration_path or config.CALIBRATION_PATH)),
    }
