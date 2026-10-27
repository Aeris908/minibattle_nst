"""Central configuration for the Goal 3 (NES) minibattle analytics pipeline.

Everything is local.  No cloud APIs, no paid services and no shell wrappers.
FFmpeg / ffprobe are invoked as direct executables through subprocess.run.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable

import numpy as np

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("GOAL3_DATA_DIR", ROOT_DIR / "data")).resolve()
DB_PATH = Path(os.environ.get("GOAL3_DB_PATH", DATA_DIR / "tournament.db"))
TEMP_DIR = DATA_DIR / "temp"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"
TEMPLATES_DIR = DATA_DIR / "templates"
DIGIT_TEMPLATES_DIR = TEMPLATES_DIR / "digits"
CALIBRATION_DIR = DATA_DIR / "calibration"
CALIBRATION_PATH = CALIBRATION_DIR / "ui.json"
ANCHOR_TEMPLATE_PATH = TEMPLATES_DIR / "scoreboard_anchor.png"
LOGS_DIR = DATA_DIR / "logs"
EXPORT_DIR = DATA_DIR / "exports"


def ensure_directories() -> None:
    for path in (
        DATA_DIR,
        TEMP_DIR,
        SCREENSHOTS_DIR,
        TEMPLATES_DIR,
        DIGIT_TEMPLATES_DIR,
        CALIBRATION_DIR,
        LOGS_DIR,
        EXPORT_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Executables.  Direct paths only; never use pwsh.exe / powershell.exe.
# ---------------------------------------------------------------------------
FFMPEG_BIN = os.environ.get("GOAL3_FFMPEG", "ffmpeg")
FFPROBE_BIN = os.environ.get("GOAL3_FFPROBE", "ffprobe")
FFMPEG_LOCATION = os.environ.get("GOAL3_FFMPEG_LOCATION", "")

# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------
VIDEO_MAX_HEIGHT = 480
VIDEO_MIN_HEIGHT = 360
VIDEO_FORMAT = (
    "bv*[height<=480][height>=360][ext=mp4]/"
    "bv*[height<=480][height>=360]/"
    "bv*[height<=480]/"
    "b[height<=480]/b"
)
AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"
YTDLP_RETRIES = 5
YTDLP_SOCKET_TIMEOUT = 30

# ---------------------------------------------------------------------------
# OpenCV / scoreboard calibration
# ---------------------------------------------------------------------------
SCAN_FPS = 1.0
SCORE_CONFIDENCE = 0.80
SCORE_STABILITY_SEC = 3.0
MATCH_START_STABLE_SEC = 2.0
ANCHOR_SCALES = tuple(float(value) for value in np.linspace(0.5, 1.5, 25))
ANCHOR_MATCH_THRESHOLD = 0.67
DIGIT_MATCH_THRESHOLD = 0.55
DIGIT_TEMPLATE_SIZE = (32, 48)
ANCHOR_SEARCH_PADDING = 90
MATCH_END_GAP_SEC = 40.0
MATCH_FINALIZE_DURATION_SEC = 210.0
MAX_GAP_BEFORE_FINALIZE_SEC = 600.0
MIN_MATCH_DURATION_SEC = 90.0
STALE_PROCESSING_HOURS = 12.0
MAX_MATCHES_PER_MINIBATTLE = 9

# A source video can contain more than one minibattle.  A long inter-match
# break is the strongest signal; a very long video is a secondary signal used
# only when at least three matches are present and the split is balanced.
SERIES_BREAK_GAP_SEC = 600.0
MULTI_MINIBATTLE_VIDEO_DURATION_SEC = 7200.0
SERIES_DURATION_SPLIT_MIN_GAP_SEC = 240.0
MIN_MATCHES_PER_SERIES = 3
MAX_MINIBATTLES_PER_VIDEO = 3
SERIES_MIN_BALANCE_RATIO = 0.25

HALF_LENGTH_SEC = 150.0
CLUTCH_WINDOW_SEC = 60.0
DOUBLET_WINDOW_SEC = 45.0
FIELD_GREEN_RATIO_MIN = 0.18
FIELD_HSV_LOWER = (35, 55, 30)
FIELD_HSV_UPPER = (95, 255, 255)

# ---------------------------------------------------------------------------
# Roster / OCR
# ---------------------------------------------------------------------------
OCR_LANGUAGES = ["ru", "en"]
OCR_GPU = False
OCR_MIN_CONFIDENCE = 0.25
ROSTER_SCAN_WINDOW_SEC = 240
ROSTER_SAMPLE_STEP_SEC = 15
ROSTER_MIN_PLAYERS_PER_TEAM = 4
ROSTER_MAX_PLAYERS_PER_TEAM = 6
ROSTER_TOP_TEAM_ROLE = os.environ.get("GOAL3_TOP_TEAM_ROLE", "VOICE_TEAM")
ROSTER_FUZZY_CUTOFF = 80
ROSTER_OPENING_LOOKBACK_SEC = 180.0

POSITION_OCR_ALIASES = {
    "GK": {"gk", "вк", "вр", "вратарь", "вратаря", "goalkeeper", "keeper"},
    "DF": {"df", "защ", "защитник", "защитника", "defender", "defence", "defense"},
    "MF": {"mf", "пз", "полузащ", "полузащитник", "midfielder", "midfield"},
    "FW": {"fw", "нап", "нападающий", "нападающего", "forward", "striker", "attacker"},
}
VALID_POSITIONS = ("GK", "DF", "MF", "FW")
POSITION_FALLBACK_ORDER = ("GK", "DF", "DF", "MF", "MF", "FW")
POSITION_FALLBACKS_BY_COUNT = {
    3: ("GK", "DF", "FW"),
    4: ("GK", "DF", "MF", "FW"),
    5: ("GK", "DF", "DF", "MF", "FW"),
    6: POSITION_FALLBACK_ORDER,
}

# Up to 6 players per side (12 total) and six controller slots per side.
CONTROLLERS_PER_TEAM = 6

# ---------------------------------------------------------------------------
# Audio / Whisper
# ---------------------------------------------------------------------------
WHISPER_MODEL = os.environ.get("GOAL3_WHISPER_MODEL", "small")
WHISPER_DEVICE = os.environ.get("GOAL3_WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.environ.get("GOAL3_WHISPER_COMPUTE_TYPE", "int8")
WHISPER_LANGUAGE = os.environ.get("GOAL3_WHISPER_LANGUAGE", "ru")
WHISPER_BEAM_SIZE = 5
WHISPER_VAD_FILTER = True
WHISPER_MIN_SILENCE_MS = 500
WHISPER_WORD_TIMESTAMPS = True
WHISPER_LOCAL_ONLY = os.environ.get("GOAL3_WHISPER_LOCAL_ONLY", "0") == "1"
AUDIO_SAMPLE_RATE = 16000
AUDIO_CHANNELS = 1

OPENING_AUDIO_SEC = 180.0
PREMATCH_WINDOW = (-60.0, 0.0)
GOAL_WINDOW = (-20.0, 35.0)
POSTGAME_WINDOW = (0.0, 90.0)
VOICE_EXCERPT_MAX_CHARS = 2200

WHISPER_SLANG = (
    "суперудар, привоз, селект, инпут-лаг, инпут лаг, скидка, пас, вратарь, "
    "гол, забил, ассист, передача, лаг, эмулятор, не нажалось, залип, фейл, "
    "красава, молодец, тайминг, офсайд, штанга, перекладина"
)

# ---------------------------------------------------------------------------
# Tournament / voice parsing
# ---------------------------------------------------------------------------
VOICE_TEAM = "VOICE_TEAM"
OPPONENT_TEAM = "OPPONENT_TEAM"
TEAM_ROLES = (VOICE_TEAM, OPPONENT_TEAM)
SIDE_LEFT = "1P_LEFT"
SIDE_RIGHT = "2P_RIGHT"
VALID_SIDES = (SIDE_LEFT, SIDE_RIGHT)
MATCH_OUTCOMES = ("WIN", "DRAW", "LOSS")
VERIFICATION_AUTO = "auto_verified"
VERIFICATION_REVIEW = "needs_review"
VERIFICATION_MANUAL = "manual_verified"

ASSIST_MARKERS = (
    "пас", "пасует", "паснул", "передал", "передача", "скидка", "скинул",
    "отдал", "ассист", "накинул", "вывел", "прострел", "прострел",
    "подача", "подал", "закинул",
)
AUTHOR_MARKERS = (
    "гол", "забил", "забивает", "залетел", "влетел", "банка", "шайба",
    "красава", "красавчик", "молодец", "умница", "хорош", "легенда",
    "сделал", "оформил", "дубль", "хет-трик",
)
PRAISE_MARKERS = (
    "красава", "красавчик", "молодец", "умница", "хорош", "легенда",
    "гениально", "шикарно", "топ", "сильно", "какой гол", "что за гол",
)
BLUNDER_MARKERS = (
    "привоз", "привёз", "привез", "привезли", "ошибка", "ошибся", "проспал",
    "прошляпил", "потерял", "провал", "фейл", "виноват", "вина", "не выбежал",
    "не добежал", "не успел", "не нажал", "не нажалось", "залип", "лаг",
    "лаги", "селект", "эмулятор", "инпут", "тупняк", "косяк", "автогол",
    "свой", "не туда", "отдал сопернику",
)
TECH_COMPLAINT_MARKERS = (
    "лаг", "лаги", "лагает", "залип", "залипло", "селект", "эмулятор",
    "не нажалось", "не нажимается", "инпут-лаг", "инпут лаг", "фризит",
    "тормозит", "пропуск кадра", "десинк", "управление", "не слушается",
    "нажал", "не нажал",
)
RAGE_MARKERS = (
    "блять", "бля", "сука", "пиздец", "нахуй", "нахер", "твою мать",
    "да что", "как так", "опять", "ну блин", "черт", "ёб", "еб",
)

# Strict team whitelist.  These 14 teams are the only selectable Goal 3
# teams in this project.  "Япония 1"/"Япония 2" are distinct teams: a bare
# "Япония" without a number is ambiguous and must be reviewed manually.
COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "Япония 1": (
        "япония 1", "японию 1", "японии 1", "японией 1", "япония-1",
        "япония один", "японию один", "япония первая", "japan 1", "japan one",
    ),
    "Япония 2": (
        "япония 2", "японию 2", "японии 2", "японией 2", "япония-2",
        "япония два", "японию два", "япония вторая", "japan 2", "japan two",
    ),
    "Италия": ("италия", "италию", "италии", "италией", "italy", "итальянцы"),
    "Германия": ("германия", "германию", "германии", "германией", "germany", "немцы"),
    "Аргентина": (
        "аргентина", "аргентину", "аргентины", "аргентине", "аргентиной",
        "argentina",
    ),
    "Бразилия": ("бразилия", "бразилию", "бразилии", "бразилией", "brazil", "бразил"),
    "Камерун": ("камерун", "камеруна", "камеруну", "камеруне", "камеруном", "cameroon"),
    "Мексика": ("мексика", "мексику", "мексики", "мексике", "мексикой", "mexico"),
    "Монголия": ("монголия", "монголию", "монголии", "монголией", "mongolia"),
    "Сенегал": (
        "сенегал", "сенегала", "сенегалу", "сенегале", "сенегалом", "senegal",
    ),
    "Гвинея": ("гвинея", "гвинею", "гвинеи", "гвинее", "гвинеей", "guinea"),
    "Англия": ("англия", "англию", "англии", "англией", "england", "англичане"),
    "Тайланд": (
        "тайланд", "таиланд", "тайланда", "таиланда", "тайланду", "таиланду",
        "тайланде", "таиланде", "thailand",
    ),
    "Корея": (
        "корея", "корею", "кореи", "корее", "кореей", "korea", "south korea",
        "южная корея", "южную корею", "южной корее",
    ),
}
COUNTRY_CANONICAL_NAMES = tuple(COUNTRY_ALIASES.keys())
UNKNOWN_COUNTRY = "Unknown"

COUNTRY_OCR_REJECT = set(COUNTRY_CANONICAL_NAMES) | {
    alias for aliases in COUNTRY_ALIASES.values() for alias in aliases
}

OCR_STOPWORDS = {
    "stream", "voice", "team", "player", "players", "game", "goal", "score",
    "счет", "счёт", "игра", "гол", "голы", "тайм", "матч", "серия", "минибатл",
    "состав", "расстановка", "позиция", "вратарь", "защитник", "полузащитник",
    "нападающий", "онлайн", "трансляция", "подписчики", "донат", "донаты",
    "стрим", "нижний", "новый", "сообщение", "комментарий", "команда",
    "voice_team", "opponent", "против", "счет", "счёт", "1p", "2p",
    "первый", "второй", "третий", "четвертый", "четвёртый", "пятый",
    "gk", "df", "mf", "fw", "вр", "защ", "пз", "нап", "vs", "x", ":", "-",
    "подписался", "подписал", "лайк", "репост", "япония",
}
OCR_MIN_NAME_LENGTH = 2
OCR_MAX_NAME_LENGTH = 28

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
LOG_LEVEL = os.environ.get("GOAL3_LOG_LEVEL", "INFO")


def configure_logging() -> None:
    ensure_directories()
    root = logging.getLogger("goal3")
    if root.handlers:
        return
    root.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(handler)
    file_handler = logging.FileHandler(LOGS_DIR / "pipeline.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(file_handler)


# ---------------------------------------------------------------------------
# Small helpers shared by every module
# ---------------------------------------------------------------------------
def rotate_voice_side(initial_side: str, match_num: int) -> str:
    """Apply the strict two-matches-per-side rule.

    side_shifted = ((match_num - 1) // 2) % 2
    """
    if initial_side not in VALID_SIDES:
        initial_side = SIDE_RIGHT
    if match_num < 1:
        raise ValueError("match_num starts at 1")
    shifted = ((match_num - 1) // 2) % 2
    if shifted == 0:
        return initial_side
    return SIDE_LEFT if initial_side == SIDE_RIGHT else SIDE_RIGHT


def opposite_side(side: str) -> str:
    return SIDE_LEFT if side == SIDE_RIGHT else SIDE_RIGHT


def side_to_team_role(scoring_side: str, voice_side: str) -> str:
    """Map a scoreboard side (LEFT/RIGHT) to a tournament team role."""
    if scoring_side == "LEFT":
        return VOICE_TEAM if voice_side == SIDE_LEFT else OPPONENT_TEAM
    return VOICE_TEAM if voice_side == SIDE_RIGHT else OPPONENT_TEAM


def normalize_text(text: str) -> str:
    text = (text or "").lower().replace("ё", "е")
    text = text.replace("—", "-").replace("–", "-")
    text = "".join(ch if ch.isalnum() or ch in " -" else " " for ch in text)
    return " ".join(text.split())


def contains_any(text: str, markers: Iterable[str]) -> bool:
    haystack = normalize_text(text)
    return any(normalize_text(marker) in haystack for marker in markers)


def count_any(text: str, markers: Iterable[str]) -> int:
    haystack = normalize_text(text)
    return sum(1 for marker in markers if normalize_text(marker) in haystack)


def hhmmss(seconds: float | int | None) -> str:
    if seconds is None:
        return "00:00:00"
    total = max(0, int(round(float(seconds))))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def minute_bucket(game_sec: float | int | None) -> str:
    if game_sec is None:
        return "1m"
    minute = int(max(0, game_sec) // 60) + 1
    return "6m+" if minute >= 6 else f"{minute}m"


def half_for_game_sec(game_sec: float | int | None, half_length: float = HALF_LENGTH_SEC) -> int:
    if game_sec is None:
        return 1
    return 1 if float(game_sec) < float(half_length) else 2


def outcome_for_team(score_for: int, score_against: int) -> str:
    if score_for > score_against:
        return "WIN"
    if score_for < score_against:
        return "LOSS"
    return "DRAW"


def opposite_outcome(outcome: str) -> str:
    return {"WIN": "LOSS", "LOSS": "WIN", "DRAW": "DRAW"}.get(outcome, "DRAW")


ensure_directories()
