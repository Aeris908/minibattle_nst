"""Roster and formation extraction (easyocr, fully local).

The right-hand stream overlay is the ground truth for player names.  A single
OCR pass near the start of the minibattle creates the Active Stream Roster.
The pre-match formation screen is then OCR-scanned for positions and controller
numbers, supplemented by the first three minutes of voice roll-call.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import cv2
import numpy as np
from rapidfuzz import fuzz, process

import config
from database import Database

log = logging.getLogger("goal3.roster")


class RosterExtractionError(RuntimeError):
    pass


@dataclass
class OCRItem:
    text: str
    confidence: float
    bbox: tuple[float, float, float, float]  # x1, y1, x2, y2 in ROI coordinates

    @property
    def cy(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2.0

    @property
    def cx(self) -> float:
        return (self.bbox[0] + self.bbox[2]) / 2.0


_READER = None


def get_ocr_reader():
    global _READER
    if _READER is None:
        try:
            import easyocr  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RosterExtractionError(
                "easyocr is not installed. Run: pip install -r requirements.txt"
            ) from exc
        log.info("Loading easyocr model (languages=%s, gpu=%s)", config.OCR_LANGUAGES, config.OCR_GPU)
        _READER = easyocr.Reader(
            config.OCR_LANGUAGES,
            gpu=config.OCR_GPU,
            verbose=False,
        )
    return _READER


# ---------------------------------------------------------------------------
# Frame I/O
# ---------------------------------------------------------------------------
def read_frame(video_path: str | Path, seconds: float) -> np.ndarray | None:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return None
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(seconds * fps)))
    ok, frame = cap.read()
    cap.release()
    return frame if ok else None


def _crop_roi(frame: np.ndarray, roi: Sequence[int] | None) -> np.ndarray:
    if frame is None:
        raise ValueError("Frame is empty")
    if not roi:
        h, w = frame.shape[:2]
        return frame[:, int(w * 0.70):]
    x, y, w, h = [int(v) for v in roi]
    x1 = max(0, x)
    y1 = max(0, y)
    x2 = min(frame.shape[1], x + w)
    y2 = min(frame.shape[0], y + h)
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        raise ValueError("ROI is outside the frame")
    return crop


def _ocr_crop(crop: np.ndarray, upscale: float = 2.0) -> list[OCRItem]:
    if crop is None or crop.size == 0:
        return []
    reader = get_ocr_reader()
    work = crop
    if upscale > 1.0:
        work = cv2.resize(
            crop,
            (int(crop.shape[1] * upscale), int(crop.shape[0] * upscale)),
            interpolation=cv2.INTER_CUBIC,
        )
    try:
        results = reader.readtext(work, detail=1, paragraph=False)
    except Exception as exc:
        log.warning("easyocr failed on crop: %s", exc)
        return []
    items: list[OCRItem] = []
    for entry in results:
        if len(entry) < 3:
            continue
        bbox, text, confidence = entry[0], entry[1], entry[2]
        if float(confidence) < config.OCR_MIN_CONFIDENCE:
            continue
        xs = [point[0] / upscale for point in bbox]
        ys = [point[1] / upscale for point in bbox]
        items.append(
            OCRItem(
                text=str(text).strip(),
                confidence=float(confidence),
                bbox=(min(xs), min(ys), max(xs), max(ys)),
            )
        )
    return items


# ---------------------------------------------------------------------------
# Text parsing helpers
# ---------------------------------------------------------------------------
_COUNTRY_LOOKUP: list[tuple[str, str]] = []
for canonical, aliases in config.COUNTRY_ALIASES.items():
    _COUNTRY_LOOKUP.append((canonical, canonical))
    for alias in aliases:
        _COUNTRY_LOOKUP.append((alias, canonical))

_POSITION_LOOKUP: list[tuple[str, str]] = []
for position, aliases in config.POSITION_OCR_ALIASES.items():
    _POSITION_LOOKUP.append((position, position))
    for alias in aliases:
        _POSITION_LOOKUP.append((alias, position))

_ORDINAL_PATTERNS: tuple[tuple[str, int], ...] = (
    (r"\b(?:перв\w*|1[- ]?(?:й|ый|st)|один)\b", 1),
    (r"\b(?:втор\w*|2[- ]?(?:й|ой|nd)|два)\b", 2),
    (r"\b(?:трет\w*|3[- ]?(?:й|ий|rd)|три)\b", 3),
    (r"\b(?:четв[её]рт\w*|4[- ]?(?:й|ый|th)|четыре)\b", 4),
    (r"\b(?:пят\w*|5[- ]?(?:й|ый|th)|пять)\b", 5),
    (r"\b(?:шест\w*|6[- ]?(?:й|ой|th)|шесть)\b", 6),
)
_CONTROLLER_WORD_RE = re.compile(
    r"(?:контроллер|номер|играю|играет|беру|взял|на|у|под)\D{0,18}"
    r"(\d|один|два|три|четыре|пять|шесть)",
    re.IGNORECASE,
)


def _match_country(text: str) -> str | None:
    norm = config.normalize_text(text)
    if not norm:
        return None
    if norm in {"япония", "japan", "жапан"}:
        # Bare Japan is ambiguous between Япония 1 and Япония 2.
        return None
    choices = [alias for alias, _ in _COUNTRY_LOOKUP]
    result = process.extractOne(norm, choices, scorer=fuzz.WRatio, score_cutoff=86)
    if not result:
        return None
    matched_text = result[0]
    for alias, canonical in _COUNTRY_LOOKUP:
        if alias == matched_text:
            return canonical
    return None


def _match_position(text: str) -> str | None:
    norm = config.normalize_text(text)
    tokens = set(norm.split())
    for alias, position in _POSITION_LOOKUP:
        if alias in tokens:
            return position
    for alias, position in _POSITION_LOOKUP:
        if len(alias) >= 3 and re.search(rf"\b{re.escape(alias)}\b", norm):
            return position
    return None


def _looks_like_player_name(text: str) -> bool:
    raw = " ".join(str(text).replace("\n", " ").split()).strip(" .,:;|—–-")
    if not raw:
        return False
    if re.fullmatch(r"[\d\s:.\-–—/]+", raw):
        return False
    if re.search(r"(https?://|www\.|@\w+)", raw, re.IGNORECASE):
        return False
    norm = config.normalize_text(raw)
    if not norm:
        return False
    if _match_country(raw):
        return False
    tokens = norm.split()
    if not tokens:
        return False
    if len(raw) < config.OCR_MIN_NAME_LENGTH or len(raw) > config.OCR_MAX_NAME_LENGTH:
        return False
    meaningful = [
        token for token in tokens
        if token not in config.OCR_STOPWORDS and token not in config.COUNTRY_OCR_REJECT
    ]
    if not meaningful:
        return False
    if all(token.isdigit() for token in meaningful):
        return False
    # Reject scoreboard-like strings and percentages.
    if re.fullmatch(r"\d+\s*[:xх\-]\s*\d+", norm):
        return False
    if "%" in raw or raw.count("|") >= 2:
        return False
    # A name needs at least one letter.
    if not any(ch.isalpha() for ch in raw):
        return False
    return True


def clean_ocr_name(text: str) -> tuple[str | None, int | None, str | None]:
    """Strip controller / position decorations from an OCR line."""
    raw = " ".join(str(text).replace("\n", " ").split()).strip(" .,:;|")
    raw = re.sub(r"^[•●○▪▫★☆✓✔\-–—]+\s*", "", raw)
    controller = None
    m = re.match(r"^(\d{1,2})\s*[\.\):\-]?\s+(.+)$", raw)
    if m and _looks_like_player_name(m.group(2)):
        controller = int(m.group(1))
        raw = m.group(2).strip()
    position = _match_position(raw)
    # Remove standalone position tokens and team/role prefixes.
    cleaned_tokens = []
    for token in raw.split():
        low = config.normalize_text(token)
        if low in config.OCR_STOPWORDS or low in config.COUNTRY_OCR_REJECT:
            continue
        is_position = any(low == alias for alias, _ in _POSITION_LOOKUP)
        if is_position:
            continue
        if low in {"vs", "x", ":", "-", "|"}:
            continue
        cleaned_tokens.append(token)
    name = " ".join(cleaned_tokens).strip(" .,:;|")
    if not _looks_like_player_name(name):
        return None, controller, position
    if len(name) > config.OCR_MAX_NAME_LENGTH:
        name = name[: config.OCR_MAX_NAME_LENGTH].strip()
    return name, controller, position


def _dedupe_names(names: Iterable[str]) -> list[str]:
    result: list[str] = []
    for name in names:
        if not name:
            continue
        duplicate = False
        for existing in result:
            if fuzz.WRatio(name, existing) >= 92:
                duplicate = True
                break
        if not duplicate:
            result.append(name)
    return result


def _split_teams(items: list[tuple[str, int | None, str | None, OCRItem]]) -> tuple[list, list, str | None, str | None]:
    """Split parsed OCR rows into two teams and detect country headers."""
    items = sorted(items, key=lambda row: row[3].cy)
    header_indices: list[tuple[int, str | None]] = []
    for idx, (name, controller, position, item) in enumerate(items):
        country = _match_country(item.text)
        if country:
            header_indices.append((idx, country))
        elif re.search(r"\b(1p|2p|voice|opponent|команда|team)\b", config.normalize_text(item.text)):
            header_indices.append((idx, None))
    if len(header_indices) >= 2:
        header_indices = sorted(header_indices[:2])
        groups: list[list] = [[], []]
        labels: list[str | None] = [header_indices[0][1], header_indices[1][1]]
        for idx, row in enumerate(items):
            if idx <= header_indices[0][0]:
                continue
            if idx < header_indices[1][0]:
                groups[0].append(row)
            elif idx > header_indices[1][0]:
                groups[1].append(row)
        if min(len(groups[0]), len(groups[1])) >= 2:
            return groups[0], groups[1], labels[0], labels[1]

    # No usable headers: find the largest vertical gap that splits the list in
    # two plausible teams.
    rows = sorted(items, key=lambda row: row[3].cy)
    n = len(rows)
    best_split = None
    best_gap = -1.0
    max_side = config.ROSTER_MAX_PLAYERS_PER_TEAM
    lower = max(2, n - max_side)
    upper = min(max_side, n - 2)
    for split in range(lower, upper + 1):
        gap = rows[split][3].cy - rows[split - 1][3].cy
        if gap > best_gap:
            best_gap = gap
            best_split = split
    if best_split is None:
        midpoint = max(1, n // 2)
        return rows[:midpoint], rows[midpoint:], None, None
    return rows[:best_split], rows[best_split:], None, None


# ---------------------------------------------------------------------------
# Voice roll-call
# ---------------------------------------------------------------------------
def _extract_ordinal(text: str) -> int | None:
    norm = config.normalize_text(text)
    for pattern, number in _ORDINAL_PATTERNS:
        if re.search(pattern, norm):
            return number
    m = _CONTROLLER_WORD_RE.search(norm)
    if m:
        token = m.group(1)
        if token.isdigit():
            value = int(token)
            if 1 <= value <= config.CONTROLLERS_PER_TEAM:
                return value
        mapping = {
            "один": 1, "два": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
        }
        return mapping.get(token)
    return None


def _voice_controller_numbers(
    transcript: Sequence[dict],
    voice_players: list[dict],
) -> dict[int, int]:
    """Infer VOICE_TEAM controller numbers from the three-minute roll-call.

    Opponent numbers are intentionally not inferred here: only the voice team
    is audible, so opponent numbers must come from the formation-screen OCR.
    """
    names = [
        (p["canonical_name"], p["player_id"], p.get("aliases") or [])
        for p in voice_players
    ]
    preferred: dict[int, int] = {}
    for segment in transcript or []:
        text = segment.get("text", "")
        if not text:
            continue
        ordinal = _extract_ordinal(text)
        if ordinal is None:
            continue
        norm_text = config.normalize_text(text)
        words = set(norm_text.split())
        best = None
        for canonical, pid, aliases in names:
            for candidate in [canonical, *aliases]:
                candidate_norm = config.normalize_text(candidate)
                if len(candidate_norm) < 2:
                    continue
                score = 0.0
                if candidate_norm in words:
                    score = 100.0
                elif len(candidate_norm) >= 4 and candidate_norm in norm_text:
                    score = 98.0
                elif len(candidate_norm) >= 4 and any(
                    word.startswith(candidate_norm[:4]) for word in words
                ):
                    score = 92.0
                elif len(candidate_norm) >= 3:
                    ratio = fuzz.partial_ratio(candidate_norm, norm_text)
                    if ratio >= 90:
                        score = float(ratio)
                if score >= 90 and (best is None or score > best[0]):
                    best = (score, pid)
        if best and best[1] not in preferred and ordinal not in preferred.values():
            preferred[best[1]] = ordinal
    # Only the voice team is audible during the opening roll-call.  Opponent
    # controllers must come from the formation-screen OCR; missing values are
    # filled later by the deterministic allocator in extract_roster().
    return {
        p["player_id"]: preferred[p["player_id"]]
        for p in voice_players
        if p["player_id"] in preferred
    }


def _fallback_position(index: int, count: int) -> str:
    order = config.POSITION_FALLBACKS_BY_COUNT.get(
        count, config.POSITION_FALLBACK_ORDER
    )
    return order[min(index, len(order) - 1)]


# ---------------------------------------------------------------------------
# Lineup screen
# ---------------------------------------------------------------------------
def _formation_positions(
    frame: np.ndarray,
    lineup_roi: Sequence[int] | None,
    roster_players: list[dict],
) -> tuple[dict[int, str], dict[int, int], str | None]:
    """OCR the pre-match formation screen, matching names to positions."""
    positions: dict[int, str] = {}
    controllers: dict[int, int] = {}
    if frame is None or not lineup_roi:
        return positions, controllers, None
    try:
        crop = _crop_roi(frame, lineup_roi)
    except ValueError:
        return positions, controllers, None
    items = _ocr_crop(crop, upscale=1.5)
    if not items:
        return positions, controllers, None
    position_tokens: list[tuple[OCRItem, str]] = []
    for item in items:
        pos = _match_position(item.text)
        if pos and len(item.text.strip()) <= 12:
            position_tokens.append((item, pos))
        if pos:
            positions.setdefault(-1, pos)  # marker that the screen has labels
    for player in roster_players:
        pid = player["player_id"]
        candidates = [player["canonical_name"], *(player.get("aliases") or [])]
        best = None
        best_item = None
        for item in items:
            if _match_country(item.text):
                continue
            for candidate in candidates:
                score = fuzz.WRatio(config.normalize_text(candidate), config.normalize_text(item.text))
                if score >= 80 and (best is None or score > best):
                    best = score
                    best_item = item
        if best_item is None:
            continue
        pos = _match_position(best_item.text)
        if pos is None:
            # Nearest short position label on roughly the same row.
            best_dist = None
            for token_item, token_pos in position_tokens:
                dy = abs(token_item.cy - best_item.cy)
                if dy <= max(14.0, crop.shape[0] * 0.035):
                    dist = dy + abs(token_item.cx - best_item.cx) * 0.1
                    if best_dist is None or dist < best_dist:
                        best_dist = dist
                        pos = token_pos
        if pos:
            positions[pid] = pos
        m = re.match(r"^(\d{1,2})\s*[\.\):\-]", best_item.text.strip())
        if m:
            num = int(m.group(1))
            if 1 <= num <= 5:
                controllers[pid] = num
    positions.pop(-1, None)
    screenshot_dir = config.SCREENSHOTS_DIR
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    return positions, controllers, None


def _find_lineup_frame(
    video_path: str | Path,
    first_match_start: float,
    lineup_roi: Sequence[int] | None,
    roster_players: list[dict],
    video_id: str,
) -> tuple[np.ndarray | None, dict[int, str], dict[int, int], str | None]:
    if not lineup_roi:
        return None, {}, {}, None
    start = max(0.0, float(first_match_start or 0.0))
    times = []
    t = max(0.0, start - 180.0)
    while t < max(1.0, start - 1.0):
        times.append(t)
        t += 6.0
    times = times[-30:]
    best = None
    best_meta = ({}, {})
    for ts in times:
        frame = read_frame(video_path, ts)
        if frame is None:
            continue
        pos, ctrl, extra = _formation_positions(frame, lineup_roi, roster_players)
        score = len(pos) + 0.25 * len(ctrl)
        if best is None or score > best[0]:
            best = (score, frame, pos, ctrl, ts)
        if len(pos) == len(roster_players) and len(roster_players) >= 8:
            break
    if best is None or best[0] <= 0:
        return None, {}, {}, None
    _, frame, pos, ctrl, ts = best
    path = config.SCREENSHOTS_DIR / f"{video_id}_lineup.jpg"
    try:
        cv2.imwrite(str(path), frame)
        screenshot = str(path)
    except Exception:
        screenshot = None
    log.info("Lineup screen selected at %s (positions=%d)", config.hhmmss(ts), len(pos))
    return frame, pos, ctrl, screenshot


# ---------------------------------------------------------------------------
# Main extraction
# ---------------------------------------------------------------------------
def _sample_roster_frame(
    video_path: str | Path,
    overlay_roi: Sequence[int] | None,
    first_match_start: float,
    video_id: str,
    scan_start_sec: float | None = None,
    scan_end_sec: float | None = None,
) -> tuple[np.ndarray | None, list[tuple[str, int | None, str | None, OCRItem]], str | None]:
    """Try frames around a series opening, return the richest roster."""
    start = max(0.0, float(scan_start_sec if scan_start_sec is not None else 0.0))
    if scan_end_sec is None:
        end = max(
            float(first_match_start or 0.0) + 30.0,
            start + config.ROSTER_SCAN_WINDOW_SEC,
        )
    else:
        end = float(scan_end_sec)
    end = max(start + 1.0, end)
    times = list(
        range(
            int(start) + 2,
            int(end) + 1,
            max(1, int(config.ROSTER_SAMPLE_STEP_SEC)),
        )
    )
    if not times:
        times = [start + 1.0, start + 5.0, start + 15.0]
    if first_match_start:
        times = sorted(
            set(
                times
                + [
                    max(start, first_match_start - 20.0),
                    min(end, first_match_start + 5.0),
                ]
            )
        )
    best_score = -1
    best_items: list = []
    best_frame = None
    best_ts = None
    for ts in times:
        frame = read_frame(video_path, ts)
        if frame is None:
            continue
        try:
            crop = _crop_roi(frame, overlay_roi)
        except ValueError:
            continue
        raw_items = _ocr_crop(crop, upscale=2.0)
        parsed: list[tuple[str, int | None, str | None, OCRItem]] = []
        for item in raw_items:
            name, controller, position = clean_ocr_name(item.text)
            if name:
                parsed.append((name, controller, position, item))
        if len(parsed) < 2:
            continue
        score = len(parsed) * 10 + sum(item.confidence for item in raw_items)
        if score > best_score:
            best_score = score
            best_items = parsed
            best_frame = frame
            best_ts = ts
    screenshot = None
    if best_frame is not None:
        path = config.SCREENSHOTS_DIR / f"{video_id}_roster.jpg"
        try:
            cv2.imwrite(str(path), best_frame)
            screenshot = str(path)
        except Exception:
            screenshot = None
        log.info("Roster overlay frame selected at %s with %d OCR name(s)", config.hhmmss(best_ts), len(best_items))
    return best_frame, best_items, screenshot


def _team_payload(
    rows: list[tuple[str, int | None, str | None, OCRItem]],
    team_role: str,
    db: Database,
    limit: int = config.ROSTER_MAX_PLAYERS_PER_TEAM,
) -> list[dict]:
    players: list[dict] = []
    seen_ids: set[int] = set()
    for name, controller, position, item in rows:
        if len(players) >= limit:
            break
        try:
            resolved = db.resolve_player(name, score_cutoff=config.ROSTER_FUZZY_CUTOFF)
        except ValueError:
            continue
        pid = int(resolved["player_id"])
        if pid in seen_ids:
            continue
        seen_ids.add(pid)
        players.append(
            {
                "player_id": pid,
                "canonical_name": resolved["canonical_name"],
                "ocr_name": name,
                "team_role": team_role,
                "controller_num": controller,
                "field_position": position,
                "ocr_confidence": round(item.confidence, 3),
                "aliases": [],
            }
        )
    return players


def _enrich_aliases(db: Database, players: list[dict]) -> None:
    for player in players:
        stored = db.get_player(player["player_id"])
        if stored:
            player["aliases"] = stored.get("aliases", [])


def extract_roster(
    video_path: str | Path,
    video_id: str,
    db: Database,
    first_match_start: float,
    opening_transcript: Sequence[dict] | None = None,
    calibration: dict | None = None,
    scan_start_sec: float | None = None,
    scan_end_sec: float | None = None,
) -> dict:
    """Extract the active roster, countries, controllers and positions."""
    calibration = calibration or {}
    overlay_roi = calibration.get("overlay")
    lineup_roi = calibration.get("lineup")
    warnings: list[str] = []

    frame, parsed_rows, roster_screenshot = _sample_roster_frame(
        video_path,
        overlay_roi,
        first_match_start,
        video_id,
        scan_start_sec=scan_start_sec,
        scan_end_sec=scan_end_sec,
    )
    if frame is None or not parsed_rows:
        # Fallback: OCR the pre-match formation screen for names.
        if lineup_roi:
            lineup_frame = read_frame(video_path, max(0.0, float(first_match_start or 0.0) - 90.0))
            if lineup_frame is not None:
                try:
                    crop = _crop_roi(lineup_frame, lineup_roi)
                    for item in _ocr_crop(crop, upscale=1.5):
                        name, controller, position = clean_ocr_name(item.text)
                        if name:
                            parsed_rows.append((name, controller, position, item))
                except ValueError:
                    pass
                if parsed_rows:
                    frame = lineup_frame
                    warnings.append("Roster overlay OCR failed; names were taken from the lineup screen.")
    if not parsed_rows:
        raise RosterExtractionError(
            "Could not OCR any player names. Check the roster overlay ROI with tools/calibrate_ui.py."
        )

    team_a_rows, team_b_rows, label_a, label_b = _split_teams(parsed_rows)
    team_a_rows = [row for row in team_a_rows if row[0]]
    team_b_rows = [row for row in team_b_rows if row[0]]
    # Deduplicate and cap each side.
    def unique_rows(rows):
        result = []
        for row in rows:
            if all(fuzz.WRatio(row[0], existing[0]) < 92 for existing in result):
                result.append(row)
        if len(result) > config.ROSTER_MAX_PLAYERS_PER_TEAM:
            result = result[: config.ROSTER_MAX_PLAYERS_PER_TEAM]
        return result

    team_a_rows = unique_rows(team_a_rows)
    team_b_rows = unique_rows(team_b_rows)
    if not team_a_rows or not team_b_rows:
        # Last-resort split of one noisy list into two halves.
        all_rows = unique_rows(team_a_rows + team_b_rows)
        mid = max(1, len(all_rows) // 2)
        team_a_rows, team_b_rows = all_rows[:mid], all_rows[mid:]
    if not team_a_rows or not team_b_rows:
        raise RosterExtractionError("Roster OCR produced fewer than two teams.")

    top_role = config.ROSTER_TOP_TEAM_ROLE if config.ROSTER_TOP_TEAM_ROLE in config.TEAM_ROLES else config.VOICE_TEAM
    bottom_role = config.OPPONENT_TEAM if top_role == config.VOICE_TEAM else config.VOICE_TEAM
    team_a = _team_payload(team_a_rows, top_role, db)
    team_b = _team_payload(team_b_rows, bottom_role, db)
    if top_role == config.OPPONENT_TEAM:
        label_a, label_b = label_b, label_a

    # OCR/fuzzy matching can map both team headers to the same player.  A
    # player has exactly one team in a minibattle (UNIQUE(video_id, player_id)).
    seen_ids: set[int] = set()
    deduped_teams = []
    for team in (team_a, team_b):
        kept = []
        for player in team:
            pid = int(player["player_id"])
            if pid in seen_ids:
                warnings.append(
                    f"Duplicate roster match '{player['canonical_name']}' removed from a second team."
                )
                continue
            seen_ids.add(pid)
            kept.append(player)
        deduped_teams.append(kept)
    team_a, team_b = deduped_teams
    all_players = team_a + team_b
    _enrich_aliases(db, all_players)
    voice_players = [p for p in all_players if p["team_role"] == config.VOICE_TEAM]
    opponent_players = [p for p in all_players if p["team_role"] == config.OPPONENT_TEAM]

    voice_map = _voice_controller_numbers(opening_transcript or [], voice_players)
    for player in all_players:
        pid = player["player_id"]
        if player["team_role"] == config.VOICE_TEAM and pid in voice_map:
            player["controller_num"] = voice_map[pid]

    # Formation screen: positions and, where visible, controller numbers.
    lineup_frame, formation_pos, formation_ctrl, lineup_screenshot = _find_lineup_frame(
        video_path, first_match_start, lineup_roi, all_players, video_id
    )
    if lineup_frame is not None:
        for player in all_players:
            pid = player["player_id"]
            if pid in formation_pos:
                player["field_position"] = formation_pos[pid]
            if pid in formation_ctrl and player["controller_num"] is None:
                player["controller_num"] = formation_ctrl[pid]

    # Fill every missing position and controller deterministically.
    for team in (voice_players, opponent_players):
        team_sorted = sorted(team, key=lambda p: (p["controller_num"] or 99, p["canonical_name"]))
        for index, player in enumerate(team_sorted):
            if not player.get("field_position"):
                player["field_position"] = _fallback_position(index, len(team_sorted))
                player["position_source"] = "controller_fallback"
            else:
                player["position_source"] = "lineup_ocr"
        used: set[int] = set()
        for player in team_sorted:
            num = player.get("controller_num")
            if num in range(1, config.CONTROLLERS_PER_TEAM + 1) and num not in used:
                used.add(num)
            else:
                # Duplicate/out-of-range OCR values are re-allocated below.
                player["controller_num"] = None
        free_numbers = [
            num
            for num in range(1, config.CONTROLLERS_PER_TEAM + 1)
            if num not in used
        ]
        for player in team_sorted:
            if player.get("controller_num") is None:
                if free_numbers:
                    player["controller_num"] = free_numbers.pop(0)
                else:
                    # More players than available controller slots: keep the
                    # value empty rather than inventing controller 6.
                    player["controller_num"] = None

    if len(voice_players) < config.ROSTER_MIN_PLAYERS_PER_TEAM:
        warnings.append(
            f"Only {len(voice_players)} VOICE_TEAM players recognized "
            f"(expected {config.ROSTER_MIN_PLAYERS_PER_TEAM}-{config.ROSTER_MAX_PLAYERS_PER_TEAM})."
        )
    if len(opponent_players) < config.ROSTER_MIN_PLAYERS_PER_TEAM:
        warnings.append(
            f"Only {len(opponent_players)} OPPONENT_TEAM players recognized "
            f"(expected {config.ROSTER_MIN_PLAYERS_PER_TEAM}-{config.ROSTER_MAX_PLAYERS_PER_TEAM})."
        )

    for player in all_players:
        player.pop("ocr_confidence", None)
    result = {
        "players": all_players,
        "voice_team_label": label_a,
        "opponent_team_label": label_b,
        "roster_frame_path": roster_screenshot,
        "lineup_frame_path": lineup_screenshot,
        "warnings": warnings,
        "needs_review": bool(warnings),
    }
    log.info(
        "Roster: voice=%d opponent=%d, labels=%s/%s",
        len(voice_players),
        len(opponent_players),
        label_a,
        label_b,
    )
    return result
