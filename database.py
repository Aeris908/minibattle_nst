"""SQLite persistence layer for the Goal 3 minibattle pipeline.

Includes the complete schema, automatic additive migrations, analytical SQL
views, queue management and cascading player merges.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from rapidfuzz import fuzz, process

import config

log = logging.getLogger("goal3.database")

SCHEMA_VERSION = 2

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS players (
    player_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    aliases_json   TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS videos (
    video_id            TEXT PRIMARY KEY,
    url                 TEXT NOT NULL,
    title               TEXT,
    duration_sec        REAL NOT NULL DEFAULT 0,
    initial_voice_side  TEXT NOT NULL DEFAULT '2P_RIGHT',
    status              TEXT NOT NULL DEFAULT 'pending',
    error_msg           TEXT,
    processed_at        DATETIME,
    minibattles_found   INTEGER NOT NULL DEFAULT 0,
    split_note          TEXT
);

CREATE TABLE IF NOT EXISTS minibattles (
    video_id                   TEXT PRIMARY KEY,
    source_video_id            TEXT REFERENCES videos(video_id) ON DELETE CASCADE,
    series_index               INTEGER NOT NULL DEFAULT 1,
    series_start_sec           INTEGER NOT NULL DEFAULT 0,
    series_end_sec             INTEGER,
    source_video_duration_sec  REAL NOT NULL DEFAULT 0,
    series_detection_reason    TEXT,
    url                        TEXT NOT NULL,
    title                      TEXT,
    initial_voice_side         TEXT NOT NULL DEFAULT '2P_RIGHT',
    status                     TEXT NOT NULL DEFAULT 'pending',
    error_msg                  TEXT,
    processed_at               DATETIME,
    total_matches              INTEGER NOT NULL DEFAULT 0,
    series_wins_voice          INTEGER NOT NULL DEFAULT 0,
    series_draws               INTEGER NOT NULL DEFAULT 0,
    series_losses_voice        INTEGER NOT NULL DEFAULT 0,
    total_goals_voice          INTEGER NOT NULL DEFAULT 0,
    total_goals_opp            INTEGER NOT NULL DEFAULT 0,
    minibattle_mvp_player_id   INTEGER REFERENCES players(player_id) ON DELETE SET NULL,
    minibattle_blunder_player_id INTEGER REFERENCES players(player_id) ON DELETE SET NULL,
    avg_self_tilt_index        REAL NOT NULL DEFAULT 0.0,
    minibattle_report_md       TEXT,
    verification_status        TEXT NOT NULL DEFAULT 'needs_review'
);

CREATE TABLE IF NOT EXISTS minibattle_rosters (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id       TEXT NOT NULL REFERENCES minibattles(video_id) ON DELETE CASCADE,
    player_id      INTEGER NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
    team_role      TEXT NOT NULL,
    controller_num INTEGER,
    field_position TEXT NOT NULL,
    UNIQUE(video_id, player_id)
);

CREATE TABLE IF NOT EXISTS matches (
    match_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id                 TEXT NOT NULL REFERENCES minibattles(video_id) ON DELETE CASCADE,
    match_num_in_minibattle  INTEGER NOT NULL,
    voice_side               TEXT NOT NULL,
    team_voice_country       TEXT,
    team_opp_country         TEXT,
    score_voice              INTEGER NOT NULL DEFAULT 0,
    score_opp                INTEGER NOT NULL DEFAULT 0,
    match_outcome            TEXT NOT NULL DEFAULT 'DRAW',
    start_video_sec          INTEGER NOT NULL DEFAULT 0,
    end_video_sec            INTEGER NOT NULL DEFAULT 0,
    duration_sec             INTEGER NOT NULL DEFAULT 0,
    final_score_crop_path    TEXT,
    drama_score              REAL NOT NULL DEFAULT 0.0,
    comeback_flag            INTEGER NOT NULL DEFAULT 0,
    lead_blown_flag          INTEGER NOT NULL DEFAULT 0,
    self_tilt_index          REAL NOT NULL DEFAULT 0.0,
    tech_complaints_count    INTEGER NOT NULL DEFAULT 0,
    mvp_player_id            INTEGER REFERENCES players(player_id) ON DELETE SET NULL,
    blunder_player_id        INTEGER REFERENCES players(player_id) ON DELETE SET NULL,
    verification_status      TEXT NOT NULL DEFAULT 'needs_review',
    UNIQUE(video_id, match_num_in_minibattle)
);

CREATE TABLE IF NOT EXISTS match_lineups (
    lineup_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id        INTEGER NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
    video_id        TEXT NOT NULL REFERENCES minibattles(video_id) ON DELETE CASCADE,
    player_id       INTEGER NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
    team_role       TEXT NOT NULL,
    country_played  TEXT,
    controller_num  INTEGER,
    field_position  TEXT NOT NULL,
    match_result    TEXT NOT NULL,
    is_clean_sheet  INTEGER NOT NULL DEFAULT 0,
    UNIQUE(match_id, player_id)
);

CREATE TABLE IF NOT EXISTS match_events (
    event_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id                  INTEGER NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
    video_id                  TEXT NOT NULL REFERENCES minibattles(video_id) ON DELETE CASCADE,
    video_sec                 INTEGER NOT NULL,
    video_timestamp_str       TEXT NOT NULL,
    game_sec                  INTEGER NOT NULL DEFAULT 0,
    game_minute_bucket        TEXT NOT NULL DEFAULT '1m',
    half                      INTEGER NOT NULL DEFAULT 1,
    scoring_team              TEXT NOT NULL,
    score_snapshot            TEXT NOT NULL,
    is_clutch_goal            INTEGER NOT NULL DEFAULT 0,
    is_doublet_goal           INTEGER NOT NULL DEFAULT 0,
    author_player_id          INTEGER REFERENCES players(player_id) ON DELETE SET NULL,
    assist_player_id          INTEGER REFERENCES players(player_id) ON DELETE SET NULL,
    blunder_player_id         INTEGER REFERENCES players(player_id) ON DELETE SET NULL,
    silence_after_conceded_sec REAL NOT NULL DEFAULT 0.0,
    words_per_min_burst       REAL NOT NULL DEFAULT 0.0,
    voice_excerpt             TEXT
);

CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_minibattles_status ON minibattles(status);
CREATE INDEX IF NOT EXISTS idx_rosters_video ON minibattle_rosters(video_id);
CREATE INDEX IF NOT EXISTS idx_matches_video ON matches(video_id);
CREATE INDEX IF NOT EXISTS idx_lineups_match ON match_lineups(match_id);
CREATE INDEX IF NOT EXISTS idx_lineups_player ON match_lineups(player_id);
CREATE INDEX IF NOT EXISTS idx_events_match ON match_events(match_id);
CREATE INDEX IF NOT EXISTS idx_events_player ON match_events(author_player_id);
CREATE INDEX IF NOT EXISTS idx_events_assist ON match_events(assist_player_id);
CREATE INDEX IF NOT EXISTS idx_events_blunder ON match_events(blunder_player_id);
"""

VIEWS_SQL = """
DROP VIEW IF EXISTS v_player_career_stats;
DROP VIEW IF EXISTS v_h2h_matrix;
DROP VIEW IF EXISTS v_synergy_duos;
DROP VIEW IF EXISTS v_country_tier_list;

CREATE VIEW v_player_career_stats AS
WITH lineup_agg AS (
    SELECT
        player_id,
        COUNT(DISTINCT match_id) AS matches_played,
        COUNT(DISTINCT video_id) AS minibattles_played,
        SUM(CASE WHEN match_result = 'WIN' THEN 1 ELSE 0 END) AS wins,
        SUM(CASE WHEN match_result = 'DRAW' THEN 1 ELSE 0 END) AS draws,
        SUM(CASE WHEN match_result = 'LOSS' THEN 1 ELSE 0 END) AS losses,
        SUM(CASE WHEN is_clean_sheet = 1 THEN 1 ELSE 0 END) AS clean_sheets
    FROM match_lineups
    GROUP BY player_id
),
goal_agg AS (
    SELECT author_player_id AS player_id,
           COUNT(*) AS goals,
           SUM(CASE WHEN is_clutch_goal = 1 THEN 1 ELSE 0 END) AS clutch_goals
    FROM match_events
    WHERE author_player_id IS NOT NULL
    GROUP BY author_player_id
),
assist_agg AS (
    SELECT assist_player_id AS player_id, COUNT(*) AS assists
    FROM match_events
    WHERE assist_player_id IS NOT NULL
    GROUP BY assist_player_id
),
mvp_agg AS (
    SELECT mvp_player_id AS player_id, COUNT(*) AS mvp_count
    FROM matches
    WHERE mvp_player_id IS NOT NULL
    GROUP BY mvp_player_id
),
blunder_matches AS (
    SELECT match_id, blunder_player_id AS player_id
    FROM match_events
    WHERE blunder_player_id IS NOT NULL
    UNION
    SELECT m.match_id, m.blunder_player_id
    FROM matches m
    WHERE m.blunder_player_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM match_events e
          WHERE e.match_id = m.match_id
            AND e.blunder_player_id = m.blunder_player_id
      )
),
blunder_agg AS (
    SELECT b.player_id,
           COUNT(*) AS blunders_count,
           SUM(CASE WHEN ml.match_result = 'LOSS' THEN 1 ELSE 0 END) AS blunder_losses
    FROM blunder_matches b
    LEFT JOIN match_lineups ml
           ON ml.match_id = b.match_id AND ml.player_id = b.player_id
    GROUP BY b.player_id
),
pos_agg AS (
    SELECT player_id, field_position, COUNT(*) AS cnt
    FROM match_lineups
    GROUP BY player_id, field_position
),
fav_pos AS (
    SELECT pa.player_id, MIN(pa.field_position) AS field_position
    FROM pos_agg pa
    WHERE pa.cnt = (
        SELECT MAX(p2.cnt) FROM pos_agg p2 WHERE p2.player_id = pa.player_id
    )
    GROUP BY pa.player_id
),
country_agg AS (
    SELECT player_id, country_played, COUNT(*) AS cnt
    FROM match_lineups
    WHERE country_played IS NOT NULL
      AND TRIM(country_played) <> ''
      AND LOWER(country_played) <> 'unknown'
    GROUP BY player_id, country_played
),
fav_country AS (
    SELECT ca.player_id, MIN(ca.country_played) AS country_played
    FROM country_agg ca
    WHERE ca.cnt = (
        SELECT MAX(c2.cnt) FROM country_agg c2 WHERE c2.player_id = ca.player_id
    )
    GROUP BY ca.player_id
)
SELECT
    p.player_id,
    p.canonical_name AS player_name,
    p.aliases_json,
    COALESCE(la.minibattles_played, 0) AS minibattles_played,
    COALESCE(la.matches_played, 0) AS matches_played,
    COALESCE(la.wins, 0) AS wins,
    COALESCE(la.draws, 0) AS draws,
    COALESCE(la.losses, 0) AS losses,
    ROUND(CASE WHEN COALESCE(la.matches_played, 0) > 0
               THEN 100.0 * la.wins / la.matches_played ELSE 0.0 END, 2) AS winrate_pct,
    COALESCE(g.goals, 0) AS goals,
    COALESCE(a.assists, 0) AS assists,
    COALESCE(g.goals, 0) + COALESCE(a.assists, 0) AS goal_plus_pass,
    ROUND(CASE WHEN COALESCE(la.matches_played, 0) > 0
               THEN 1.0 * COALESCE(g.goals, 0) / la.matches_played ELSE 0.0 END, 3) AS goals_per_match,
    COALESCE(mv.mvp_count, 0) AS mvp_count,
    COALESCE(b.blunders_count, 0) AS blunders_count,
    ROUND(CASE WHEN COALESCE(b.blunders_count, 0) > 0
               THEN 100.0 * COALESCE(b.blunder_losses, 0) / b.blunders_count ELSE 0.0 END, 2) AS blunder_impact_rate,
    COALESCE(la.clean_sheets, 0) AS clean_sheets,
    COALESCE(g.clutch_goals, 0) AS clutch_goals,
    COALESCE(fp.field_position, '—') AS favorite_position,
    COALESCE(fc.country_played, '—') AS favorite_country
FROM players p
LEFT JOIN lineup_agg la ON la.player_id = p.player_id
LEFT JOIN goal_agg g ON g.player_id = p.player_id
LEFT JOIN assist_agg a ON a.player_id = p.player_id
LEFT JOIN mvp_agg mv ON mv.player_id = p.player_id
LEFT JOIN blunder_agg b ON b.player_id = p.player_id
LEFT JOIN fav_pos fp ON fp.player_id = p.player_id
LEFT JOIN fav_country fc ON fc.player_id = p.player_id;

CREATE VIEW v_h2h_matrix AS
WITH pairs AS (
    SELECT
        a.player_id AS player_a_id,
        b.player_id AS player_b_id,
        a.match_id,
        a.match_result AS result_a
    FROM match_lineups a
    JOIN match_lineups b
      ON b.match_id = a.match_id
     AND b.player_id <> a.player_id
     AND b.team_role <> a.team_role
)
SELECT
    pa.canonical_name AS player_a_name,
    pb.canonical_name AS player_b_name,
    p.player_a_id,
    p.player_b_id,
    COUNT(*) AS matches,
    SUM(CASE WHEN p.result_a = 'WIN' THEN 1 ELSE 0 END) AS wins_a,
    SUM(CASE WHEN p.result_a = 'LOSS' THEN 1 ELSE 0 END) AS wins_b,
    SUM(CASE WHEN p.result_a = 'DRAW' THEN 1 ELSE 0 END) AS draws,
    ROUND(100.0 * SUM(CASE WHEN p.result_a = 'WIN' THEN 1 ELSE 0 END) / COUNT(*), 2) AS winrate_a_pct
FROM pairs p
JOIN players pa ON pa.player_id = p.player_a_id
JOIN players pb ON pb.player_id = p.player_b_id
GROUP BY p.player_a_id, p.player_b_id;

CREATE VIEW v_synergy_duos AS
WITH pair_stats AS (
    SELECT
        CASE WHEN a.player_id < b.player_id THEN a.player_id ELSE b.player_id END AS p1,
        CASE WHEN a.player_id < b.player_id THEN b.player_id ELSE a.player_id END AS p2,
        COUNT(DISTINCT a.match_id) AS matches_together,
        SUM(CASE WHEN a.match_result = 'WIN' THEN 1 ELSE 0 END) AS joint_wins
    FROM match_lineups a
    JOIN match_lineups b
      ON b.match_id = a.match_id
     AND b.team_role = a.team_role
     AND b.player_id > a.player_id
    GROUP BY
        CASE WHEN a.player_id < b.player_id THEN a.player_id ELSE b.player_id END,
        CASE WHEN a.player_id < b.player_id THEN b.player_id ELSE a.player_id END
),
duo_goals AS (
    SELECT
        assist_player_id,
        author_player_id,
        COUNT(*) AS goals_together
    FROM match_events
    WHERE assist_player_id IS NOT NULL
      AND author_player_id IS NOT NULL
      AND assist_player_id <> author_player_id
    GROUP BY assist_player_id, author_player_id
)
SELECT
    pa.canonical_name AS assist_player_name,
    pb.canonical_name AS author_player_name,
    dg.assist_player_id,
    dg.author_player_id,
    dg.goals_together,
    ps.matches_together,
    ps.joint_wins,
    ROUND(100.0 * ps.joint_wins / ps.matches_together, 2) AS joint_winrate_pct
FROM duo_goals dg
JOIN pair_stats ps
  ON ps.p1 = CASE WHEN dg.assist_player_id < dg.author_player_id THEN dg.assist_player_id ELSE dg.author_player_id END
 AND ps.p2 = CASE WHEN dg.assist_player_id < dg.author_player_id THEN dg.author_player_id ELSE dg.assist_player_id END
JOIN players pa ON pa.player_id = dg.assist_player_id
JOIN players pb ON pb.player_id = dg.author_player_id;

CREATE VIEW v_country_tier_list AS
WITH country_team_match AS (
    -- One row per (country, match, side).  Keeping team_role in the key
    -- avoids mixing voice/opponent scores if both sides picked the same
    -- country (mirror match) or if OCR returned the same label twice.
    SELECT
        ml.country_played AS country_name,
        ml.match_id,
        ml.team_role,
        MAX(ml.match_result) AS result,
        MAX(CASE WHEN ml.team_role = 'VOICE_TEAM'
                 THEN m.score_voice ELSE m.score_opp END) AS goals_for,
        MAX(CASE WHEN ml.team_role = 'VOICE_TEAM'
                 THEN m.score_opp ELSE m.score_voice END) AS goals_against
    FROM match_lineups ml
    JOIN matches m ON m.match_id = ml.match_id
    WHERE ml.country_played IS NOT NULL
      AND TRIM(ml.country_played) <> ''
      AND LOWER(ml.country_played) <> 'unknown'
    GROUP BY ml.country_played, ml.match_id, ml.team_role
)
SELECT
    country_name,
    COUNT(*) AS matches_played,
    SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) AS wins,
    SUM(CASE WHEN result = 'DRAW' THEN 1 ELSE 0 END) AS draws,
    SUM(CASE WHEN result = 'LOSS' THEN 1 ELSE 0 END) AS losses,
    ROUND(100.0 * SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) / COUNT(*), 2) AS winrate_pct,
    ROUND(AVG(goals_for - goals_against), 2) AS avg_goal_diff,
    SUM(goals_for) AS goals_for,
    SUM(goals_against) AS goals_against
FROM country_team_match
GROUP BY country_name;
"""

# Additive migrations for databases created by earlier revisions of the app.
MIGRATION_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "players": [
        ("aliases_json", "TEXT NOT NULL DEFAULT '[]'"),
    ],
    "videos": [
        ("url", "TEXT NOT NULL DEFAULT ''"),
        ("title", "TEXT"),
        ("duration_sec", "REAL NOT NULL DEFAULT 0"),
        ("initial_voice_side", "TEXT NOT NULL DEFAULT '2P_RIGHT'"),
        ("status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("error_msg", "TEXT"),
        ("processed_at", "DATETIME"),
        ("minibattles_found", "INTEGER NOT NULL DEFAULT 0"),
        ("split_note", "TEXT"),
    ],
    "minibattles": [
        ("source_video_id", "TEXT"),
        ("series_index", "INTEGER NOT NULL DEFAULT 1"),
        ("series_start_sec", "INTEGER NOT NULL DEFAULT 0"),
        ("series_end_sec", "INTEGER"),
        ("source_video_duration_sec", "REAL NOT NULL DEFAULT 0"),
        ("series_detection_reason", "TEXT"),
        ("initial_voice_side", "TEXT NOT NULL DEFAULT '2P_RIGHT'"),
        ("total_matches", "INTEGER NOT NULL DEFAULT 0"),
        ("series_wins_voice", "INTEGER NOT NULL DEFAULT 0"),
        ("series_draws", "INTEGER NOT NULL DEFAULT 0"),
        ("series_losses_voice", "INTEGER NOT NULL DEFAULT 0"),
        ("total_goals_voice", "INTEGER NOT NULL DEFAULT 0"),
        ("total_goals_opp", "INTEGER NOT NULL DEFAULT 0"),
        ("minibattle_mvp_player_id", "INTEGER"),
        ("minibattle_blunder_player_id", "INTEGER"),
        ("avg_self_tilt_index", "REAL NOT NULL DEFAULT 0.0"),
        ("minibattle_report_md", "TEXT"),
        ("verification_status", "TEXT NOT NULL DEFAULT 'needs_review'"),
    ],
    "matches": [
        ("voice_side", "TEXT NOT NULL DEFAULT '2P_RIGHT'"),
        ("team_voice_country", "TEXT"),
        ("team_opp_country", "TEXT"),
        ("score_voice", "INTEGER NOT NULL DEFAULT 0"),
        ("score_opp", "INTEGER NOT NULL DEFAULT 0"),
        ("match_outcome", "TEXT NOT NULL DEFAULT 'DRAW'"),
        ("start_video_sec", "INTEGER NOT NULL DEFAULT 0"),
        ("end_video_sec", "INTEGER NOT NULL DEFAULT 0"),
        ("duration_sec", "INTEGER NOT NULL DEFAULT 0"),
        ("final_score_crop_path", "TEXT"),
        ("drama_score", "REAL NOT NULL DEFAULT 0.0"),
        ("comeback_flag", "INTEGER NOT NULL DEFAULT 0"),
        ("lead_blown_flag", "INTEGER NOT NULL DEFAULT 0"),
        ("self_tilt_index", "REAL NOT NULL DEFAULT 0.0"),
        ("tech_complaints_count", "INTEGER NOT NULL DEFAULT 0"),
        ("mvp_player_id", "INTEGER"),
        ("blunder_player_id", "INTEGER"),
        ("verification_status", "TEXT NOT NULL DEFAULT 'needs_review'"),
    ],
    "match_lineups": [
        ("country_played", "TEXT"),
        ("controller_num", "INTEGER"),
        ("field_position", "TEXT NOT NULL DEFAULT 'MF'"),
        ("match_result", "TEXT NOT NULL DEFAULT 'DRAW'"),
        ("is_clean_sheet", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "match_events": [
        ("game_sec", "INTEGER NOT NULL DEFAULT 0"),
        ("game_minute_bucket", "TEXT NOT NULL DEFAULT '1m'"),
        ("half", "INTEGER NOT NULL DEFAULT 1"),
        ("score_snapshot", "TEXT NOT NULL DEFAULT '0:0'"),
        ("is_clutch_goal", "INTEGER NOT NULL DEFAULT 0"),
        ("is_doublet_goal", "INTEGER NOT NULL DEFAULT 0"),
        ("author_player_id", "INTEGER"),
        ("assist_player_id", "INTEGER"),
        ("blunder_player_id", "INTEGER"),
        ("silence_after_conceded_sec", "REAL NOT NULL DEFAULT 0.0"),
        ("words_per_min_burst", "REAL NOT NULL DEFAULT 0.0"),
        ("voice_excerpt", "TEXT"),
    ],
}


class Database:
    """Thread-safe SQLite facade used by the worker and the Streamlit UI."""

    def __init__(self, db_path: str | Path | None = None):
        config.ensure_directories()
        self.path = Path(db_path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")
        self.init_schema()

    # ------------------------------------------------------------------
    # Schema / migrations
    # ------------------------------------------------------------------
    def init_schema(self) -> None:
        with self._lock:
            with self.conn:
                self.conn.executescript(SCHEMA_SQL)
                self._run_additive_migrations()
                try:
                    self.conn.executescript(VIEWS_SQL)
                except sqlite3.Error as exc:
                    log.exception("Could not create analytical views: %s", exc)
                    raise
                self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _run_additive_migrations(self) -> None:
        for table, columns in MIGRATION_COLUMNS.items():
            existing = {
                row["name"]
                for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not existing:
                continue
            for name, ddl in columns:
                if name not in existing:
                    log.info("Migration: adding %s.%s", table, name)
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        self._migrate_legacy_videos()
        # Recreate indexes that may be missing in an older DB.  Indexes that
        # reference newly added columns are created only after the ALTER TABLE
        # statements above.
        for statement in SCHEMA_SQL.split(";"):
            if "CREATE INDEX" in statement:
                try:
                    self.conn.execute(statement)
                except sqlite3.Error:
                    pass
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_minibattles_source "
            "ON minibattles(source_video_id, series_index)"
        )
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_minibattles_source_series "
            "ON minibattles(source_video_id, series_index)"
        )

    def _migrate_legacy_videos(self) -> None:
        """Create queue rows for databases that predate the videos table."""
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(minibattles)").fetchall()
        }
        if "source_video_id" not in columns:
            return
        legacy = self.conn.execute(
            """SELECT video_id, url, title, initial_voice_side, status, processed_at
               FROM minibattles
               WHERE source_video_id IS NULL OR source_video_id = ''"""
        ).fetchall()
        for row in legacy:
            video_status = (
                row["status"] if row["status"] in {"completed", "failed"} else "pending"
            )
            self.conn.execute(
                """INSERT OR IGNORE INTO videos(
                       video_id, url, title, duration_sec, initial_voice_side,
                       status, processed_at, minibattles_found, split_note
                   ) VALUES(?, ?, ?, 0, ?, ?, ?, 1, 'legacy single-video row')""",
                (
                    row["video_id"],
                    row["url"],
                    row["title"],
                    row["initial_voice_side"] or config.SIDE_RIGHT,
                    video_status,
                    row["processed_at"],
                ),
            )
            self.conn.execute(
                """UPDATE minibattles
                   SET source_video_id = video_id,
                       series_index = 1,
                       series_start_sec = 0,
                       series_detection_reason = 'legacy single-video row'
                   WHERE video_id = ?""",
                (row["video_id"],),
            )
        if legacy:
            log.info("Migrated %d legacy minibattle rows into the videos queue", len(legacy))

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def executemany(self, sql: str, params: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            self.conn.executemany(sql, list(params))
            self.conn.commit()

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
            return [dict(r) for r in rows]

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        with self._lock:
            row = self.conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    def query_df(self, sql: str, params: Sequence[Any] = ()):
        import pandas as pd

        with self._lock:
            return pd.read_sql_query(sql, self.conn, params=tuple(params))

    # ------------------------------------------------------------------
    # Players / aliases
    # ------------------------------------------------------------------
    @staticmethod
    def _clean_name(name: str) -> str:
        return " ".join(str(name or "").replace("\n", " ").split()).strip(" .,:;|-—")

    @staticmethod
    def _parse_aliases(raw: str | None) -> list[str]:
        try:
            value = json.loads(raw or "[]")
            if isinstance(value, list):
                return [str(x).strip() for x in value if str(x).strip()]
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        return []

    def add_player(self, name: str, aliases: Iterable[str] | None = None) -> int:
        clean = self._clean_name(name)
        if not clean:
            raise ValueError("Player name is empty")
        alias_list = []
        for alias in aliases or []:
            alias = self._clean_name(alias)
            if alias and alias.casefold() != clean.casefold() and alias not in alias_list:
                alias_list.append(alias)
        with self._lock:
            row = self.conn.execute(
                "SELECT player_id FROM players WHERE canonical_name = ? COLLATE NOCASE",
                (clean,),
            ).fetchone()
            if row:
                pid = int(row["player_id"])
                existing = set()
                current = self.conn.execute(
                    "SELECT aliases_json FROM players WHERE player_id = ?", (pid,)
                ).fetchone()
                if current:
                    existing = {
                        alias.casefold()
                        for alias in self._parse_aliases(current["aliases_json"])
                    }
                for alias in alias_list:
                    if alias.casefold() not in existing:
                        self.add_alias(pid, alias)
                        existing.add(alias.casefold())
                return pid
            cur = self.conn.execute(
                "INSERT INTO players(canonical_name, aliases_json) VALUES(?, ?)",
                (clean, json.dumps(alias_list, ensure_ascii=False)),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def get_player(self, player_id: int) -> dict | None:
        row = self.fetchone("SELECT * FROM players WHERE player_id = ?", (player_id,))
        if row:
            row["aliases"] = self._parse_aliases(row.get("aliases_json"))
        return row

    def get_all_players(self) -> list[dict]:
        rows = self.fetchall("SELECT * FROM players ORDER BY canonical_name COLLATE NOCASE")
        for row in rows:
            row["aliases"] = self._parse_aliases(row.get("aliases_json"))
        return rows

    def _all_player_choices(self) -> list[tuple[str, int]]:
        choices: list[tuple[str, int]] = []
        for row in self.fetchall("SELECT player_id, canonical_name, aliases_json FROM players"):
            pid = int(row["player_id"])
            choices.append((row["canonical_name"], pid))
            for alias in self._parse_aliases(row["aliases_json"]):
                choices.append((alias, pid))
        return choices

    def resolve_player(
        self,
        name: str,
        score_cutoff: int = config.ROSTER_FUZZY_CUTOFF,
        remember_alias: bool = True,
    ) -> dict:
        """Fuzzy-resolve a spoken/OCR name, creating a player if necessary.

        Returns {"player_id", "canonical_name", "score", "created"}.
        """
        clean = self._clean_name(name)
        if not clean:
            raise ValueError("Player name is empty")
        # Exact match first.
        exact = self.fetchone(
            "SELECT player_id, canonical_name FROM players WHERE canonical_name = ? COLLATE NOCASE",
            (clean,),
        )
        if exact:
            return {
                "player_id": int(exact["player_id"]),
                "canonical_name": exact["canonical_name"],
                "score": 100.0,
                "created": False,
            }
        for row in self.fetchall("SELECT player_id, canonical_name, aliases_json FROM players"):
            for alias in self._parse_aliases(row["aliases_json"]):
                if alias.casefold() == clean.casefold():
                    return {
                        "player_id": int(row["player_id"]),
                        "canonical_name": row["canonical_name"],
                        "score": 100.0,
                        "created": False,
                    }
        choices = self._all_player_choices()
        if choices:
            names = [c[0] for c in choices]
            result = process.extractOne(clean, names, scorer=fuzz.WRatio, score_cutoff=score_cutoff)
            if result:
                matched_name, score, idx = result
                pid = choices[idx][1]
                player = self.get_player(pid)
                if player and remember_alias and clean.casefold() != player["canonical_name"].casefold():
                    if clean.casefold() not in {a.casefold() for a in player["aliases"]}:
                        self.add_alias(pid, clean)
                return {
                    "player_id": pid,
                    "canonical_name": player["canonical_name"] if player else matched_name,
                    "score": float(score),
                    "created": False,
                }
        pid = self.add_player(clean)
        return {
            "player_id": pid,
            "canonical_name": clean,
            "score": 100.0,
            "created": True,
        }

    def add_alias(self, player_id: int, alias: str) -> None:
        alias = self._clean_name(alias)
        if not alias:
            return
        with self._lock:
            row = self.conn.execute(
                "SELECT canonical_name, aliases_json FROM players WHERE player_id = ?",
                (player_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"Unknown player_id {player_id}")
            if alias.casefold() == row["canonical_name"].casefold():
                return
            aliases = self._parse_aliases(row["aliases_json"])
            if alias.casefold() in {a.casefold() for a in aliases}:
                return
            aliases.append(alias)
            self.conn.execute(
                "UPDATE players SET aliases_json = ? WHERE player_id = ?",
                (json.dumps(aliases, ensure_ascii=False), player_id),
            )
            self.conn.commit()

    def set_aliases(self, player_id: int, aliases: Iterable[str]) -> None:
        cleaned: list[str] = []
        seen: set[str] = set()
        player = self.get_player(player_id)
        if not player:
            raise ValueError(f"Unknown player_id {player_id}")
        for alias in aliases:
            alias = self._clean_name(alias)
            key = alias.casefold()
            if alias and key != player["canonical_name"].casefold() and key not in seen:
                cleaned.append(alias)
                seen.add(key)
        self.execute(
            "UPDATE players SET aliases_json = ? WHERE player_id = ?",
            (json.dumps(cleaned, ensure_ascii=False), player_id),
        )

    def rename_player(self, player_id: int, new_name: str) -> None:
        new_name = self._clean_name(new_name)
        if not new_name:
            raise ValueError("Player name is empty")
        self.execute(
            "UPDATE players SET canonical_name = ? WHERE player_id = ?",
            (new_name, player_id),
        )

    def merge_players(self, source_id: int, target_id: int) -> dict:
        """Merge source into target, cascading all foreign keys.

        Duplicate roster/lineup rows that would violate UNIQUE constraints are
        removed after moving meaningful data to the target.
        """
        if int(source_id) == int(target_id):
            raise ValueError("Cannot merge a player into themselves")
        source = self.get_player(int(source_id))
        target = self.get_player(int(target_id))
        if not source or not target:
            raise ValueError("Both players must exist")
        aliases = set(target["aliases"]) | set(source["aliases"]) | {source["canonical_name"]}
        aliases = {a for a in aliases if a.casefold() != target["canonical_name"].casefold()}
        with self._lock:
            try:
                self.conn.execute("BEGIN")
                self.conn.execute(
                    "UPDATE players SET aliases_json = ? WHERE player_id = ?",
                    (json.dumps(sorted(aliases, key=str.casefold), ensure_ascii=False), target_id),
                )
                # Rosters: one row per video.
                for row in self.conn.execute(
                    "SELECT id, video_id FROM minibattle_rosters WHERE player_id = ?",
                    (source_id,),
                ).fetchall():
                    conflict = self.conn.execute(
                        "SELECT id FROM minibattle_rosters WHERE video_id = ? AND player_id = ?",
                        (row["video_id"], target_id),
                    ).fetchone()
                    if conflict:
                        self.conn.execute("DELETE FROM minibattle_rosters WHERE id = ?", (row["id"],))
                    else:
                        self.conn.execute(
                            "UPDATE minibattle_rosters SET player_id = ? WHERE id = ?",
                            (target_id, row["id"]),
                        )
                # Matches MVP / blunder.
                self.conn.execute(
                    "UPDATE matches SET mvp_player_id = ? WHERE mvp_player_id = ?",
                    (target_id, source_id),
                )
                self.conn.execute(
                    "UPDATE matches SET blunder_player_id = ? WHERE blunder_player_id = ?",
                    (target_id, source_id),
                )
                # Lineups: one row per match.
                for row in self.conn.execute(
                    "SELECT lineup_id, match_id FROM match_lineups WHERE player_id = ?",
                    (source_id,),
                ).fetchall():
                    conflict = self.conn.execute(
                        "SELECT lineup_id FROM match_lineups WHERE match_id = ? AND player_id = ?",
                        (row["match_id"], target_id),
                    ).fetchone()
                    if conflict:
                        self.conn.execute("DELETE FROM match_lineups WHERE lineup_id = ?", (row["lineup_id"],))
                    else:
                        self.conn.execute(
                            "UPDATE match_lineups SET player_id = ? WHERE lineup_id = ?",
                            (target_id, row["lineup_id"]),
                        )
                for column in ("author_player_id", "assist_player_id", "blunder_player_id"):
                    self.conn.execute(
                        f"UPDATE match_events SET {column} = ? WHERE {column} = ?",
                        (target_id, source_id),
                    )
                self.conn.execute(
                    "UPDATE minibattles SET minibattle_mvp_player_id = ? WHERE minibattle_mvp_player_id = ?",
                    (target_id, source_id),
                )
                self.conn.execute(
                    "UPDATE minibattles SET minibattle_blunder_player_id = ? WHERE minibattle_blunder_player_id = ?",
                    (target_id, source_id),
                )
                self.conn.execute("DELETE FROM players WHERE player_id = ?", (source_id,))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return {
            "source_id": source_id,
            "target_id": target_id,
            "moved_name": source["canonical_name"],
            "into": target["canonical_name"],
        }

    # ------------------------------------------------------------------
    # Video queue / minibattles
    # ------------------------------------------------------------------
    @staticmethod
    def make_minibattle_id(source_video_id: str, series_index: int) -> str:
        return f"{source_video_id}__mb{int(series_index)}"

    def queue_video(
        self,
        video_id: str,
        url: str,
        title: str | None = None,
        initial_voice_side: str = config.SIDE_RIGHT,
        duration_sec: float = 0.0,
    ) -> bool:
        """Add a source video to the queue.  Returns True if newly created.

        A source video is downloaded once and may produce several logical
        minibattles; queue state lives here, not in the minibattles table.
        """
        if initial_voice_side not in config.VALID_SIDES:
            initial_voice_side = config.SIDE_RIGHT
        with self._lock:
            existing = self.conn.execute(
                "SELECT video_id, status FROM videos WHERE video_id = ?", (video_id,)
            ).fetchone()
            if existing:
                if existing["status"] in {"pending", "failed"}:
                    self.conn.execute(
                        """UPDATE videos
                           SET url = ?, title = COALESCE(?, title),
                               duration_sec = CASE WHEN ? > 0 THEN ? ELSE duration_sec END,
                               initial_voice_side = ?, status = 'pending',
                               error_msg = NULL, processed_at = NULL,
                               minibattles_found = 0, split_note = NULL
                           WHERE video_id = ?""",
                        (url, title, float(duration_sec), float(duration_sec),
                         initial_voice_side, video_id),
                    )
                else:
                    self.conn.execute(
                        "UPDATE videos SET url = ?, title = COALESCE(?, title) WHERE video_id = ?",
                        (url, title, video_id),
                    )
                self.conn.commit()
                return False
            self.conn.execute(
                """INSERT INTO videos(
                       video_id, url, title, duration_sec, initial_voice_side, status
                   ) VALUES(?, ?, ?, ?, ?, 'pending')""",
                (video_id, url, title, float(duration_sec or 0.0), initial_voice_side),
            )
            self.conn.commit()
            return True

    # Backward-compatible alias for older integrations.
    def queue_minibattle(
        self,
        video_id: str,
        url: str,
        title: str | None = None,
        initial_voice_side: str = config.SIDE_RIGHT,
    ) -> bool:
        return self.queue_video(video_id, url, title, initial_voice_side)

    def set_initial_voice_side(self, video_id: str, side: str) -> None:
        if side not in config.VALID_SIDES:
            raise ValueError(f"Invalid side: {side}")
        self.execute(
            "UPDATE videos SET initial_voice_side = ? WHERE video_id = ?",
            (side, video_id),
        )

    def get_video(self, video_id: str) -> dict | None:
        return self.fetchone("SELECT * FROM videos WHERE video_id = ?", (video_id,))

    def list_videos(self, status: str | None = None) -> list[dict]:
        if status:
            return self.fetchall(
                "SELECT * FROM videos WHERE status = ? ORDER BY rowid DESC", (status,)
            )
        return self.fetchall("SELECT * FROM videos ORDER BY rowid DESC")

    def get_minibattle(self, video_id: str) -> dict | None:
        return self.fetchone("SELECT * FROM minibattles WHERE video_id = ?", (video_id,))

    def get_source_minibattles(self, source_video_id: str) -> list[dict]:
        return self.fetchall(
            """SELECT * FROM minibattles
               WHERE source_video_id = ?
               ORDER BY series_index""",
            (source_video_id,),
        )

    def list_minibattles(
        self,
        status: str | None = None,
        source_video_id: str | None = None,
    ) -> list[dict]:
        where = []
        params: list[Any] = []
        if status:
            where.append("m.status = ?")
            params.append(status)
        if source_video_id:
            where.append("m.source_video_id = ?")
            params.append(source_video_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        return self.fetchall(
            f"""SELECT m.*, v.title AS source_title, v.duration_sec AS video_duration_sec,
                       v.status AS video_status
                FROM minibattles m
                LEFT JOIN videos v ON v.video_id = m.source_video_id
                {clause}
                ORDER BY m.rowid DESC""",
            params,
        )

    def queue_counts(self) -> dict:
        rows = self.fetchall("SELECT status, COUNT(*) AS n FROM videos GROUP BY status")
        return {row["status"]: int(row["n"]) for row in rows}

    def update_video(self, video_id: str, **fields: Any) -> None:
        allowed = {
            "url", "title", "duration_sec", "initial_voice_side", "status",
            "error_msg", "processed_at", "minibattles_found", "split_note",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        self.execute(
            f"UPDATE videos SET {assignments} WHERE video_id = ?",
            list(updates.values()) + [video_id],
        )

    def set_video_status(
        self,
        video_id: str,
        status: str,
        error_msg: str | None = None,
        minibattles_found: int | None = None,
        split_note: str | None = None,
    ) -> None:
        if status not in {"pending", "processing", "completed", "failed"}:
            raise ValueError(f"Invalid status {status}")
        processed_at = (
            datetime.now().isoformat(timespec="seconds")
            if status in {"processing", "completed", "failed"}
            else None
        )
        fields = {
            "status": status,
            "error_msg": error_msg,
            "processed_at": processed_at,
        }
        if minibattles_found is not None:
            fields["minibattles_found"] = int(minibattles_found)
        if split_note is not None:
            fields["split_note"] = split_note
        self.update_video(video_id, **fields)

    def claim_next_video(self) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM videos WHERE status = 'pending' ORDER BY rowid ASC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            self.conn.execute(
                """UPDATE videos
                   SET status = 'processing', error_msg = NULL, processed_at = ?
                   WHERE video_id = ?""",
                (datetime.now().isoformat(timespec="seconds"), row["video_id"]),
            )
            self.conn.commit()
            result = dict(row)
            result["status"] = "processing"
            return result

    # Backward-compatible alias.
    def claim_next_pending(self) -> dict | None:
        return self.claim_next_video()

    def set_status(self, video_id: str, status: str, error_msg: str | None = None) -> None:
        """Backward-compatible status setter for legacy callers.

        New code should use set_video_status() for a source video or
        update_minibattle(status=...) for a logical minibattle.
        """
        self.set_video_status(video_id, status, error_msg)

    def create_minibattle_series(
        self,
        source_video_id: str,
        series_index: int,
        source_url: str,
        title: str,
        initial_voice_side: str,
        series_start_sec: int,
        series_end_sec: int,
        video_duration_sec: float,
        detection_reason: str | None = None,
    ) -> str:
        logical_id = self.make_minibattle_id(source_video_id, series_index)
        self.execute(
            """INSERT INTO minibattles(
                   video_id, source_video_id, series_index, series_start_sec,
                   series_end_sec, source_video_duration_sec, series_detection_reason,
                   url, title, initial_voice_side, status, verification_status
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'processing', 'needs_review')""",
            (
                logical_id,
                source_video_id,
                int(series_index),
                int(series_start_sec),
                int(series_end_sec),
                float(video_duration_sec),
                detection_reason,
                source_url,
                title,
                initial_voice_side,
            ),
        )
        return logical_id

    def reset_stale_processing(
        self, max_age_hours: float = config.STALE_PROCESSING_HOURS
    ) -> int:
        """Return only genuinely stale source videos to pending."""
        with self._lock:
            if float(max_age_hours) <= 0:
                cur = self.conn.execute(
                    """UPDATE videos
                       SET status = 'pending', processed_at = NULL
                       WHERE status = 'processing'"""
                )
            else:
                cutoff = (
                    datetime.now() - timedelta(hours=float(max_age_hours))
                ).isoformat(timespec="seconds")
                cur = self.conn.execute(
                    """UPDATE videos
                       SET status = 'pending', processed_at = NULL
                       WHERE status = 'processing'
                         AND (processed_at IS NULL OR processed_at < ?)""",
                    (cutoff,),
                )
            self.conn.commit()
            return int(cur.rowcount or 0)

    def clear_video_analysis(self, source_video_id: str) -> None:
        """Delete all logical minibattles produced by one source video."""
        with self._lock, self.conn:
            ids = [
                row["video_id"]
                for row in self.conn.execute(
                    "SELECT video_id FROM minibattles WHERE source_video_id = ?",
                    (source_video_id,),
                ).fetchall()
            ]
            for logical_id in ids:
                self.conn.execute("DELETE FROM match_events WHERE video_id = ?", (logical_id,))
                self.conn.execute("DELETE FROM match_lineups WHERE video_id = ?", (logical_id,))
                self.conn.execute("DELETE FROM matches WHERE video_id = ?", (logical_id,))
                self.conn.execute("DELETE FROM minibattle_rosters WHERE video_id = ?", (logical_id,))
                self.conn.execute("DELETE FROM minibattles WHERE video_id = ?", (logical_id,))

    def clear_analysis(self, video_id: str) -> None:
        """Delete one logical minibattle's rows but keep its minibattles row."""
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM match_events WHERE video_id = ?", (video_id,))
            self.conn.execute("DELETE FROM match_lineups WHERE video_id = ?", (video_id,))
            self.conn.execute("DELETE FROM matches WHERE video_id = ?", (video_id,))
            self.conn.execute("DELETE FROM minibattle_rosters WHERE video_id = ?", (video_id,))
            self.conn.execute(
                """UPDATE minibattles SET
                       total_matches = 0,
                       series_wins_voice = 0,
                       series_draws = 0,
                       series_losses_voice = 0,
                       total_goals_voice = 0,
                       total_goals_opp = 0,
                       minibattle_mvp_player_id = NULL,
                       minibattle_blunder_player_id = NULL,
                       avg_self_tilt_index = 0.0,
                       minibattle_report_md = NULL,
                       verification_status = 'needs_review'
                   WHERE video_id = ?""",
                (video_id,),
            )

    def update_minibattle(self, video_id: str, **fields: Any) -> None:
        allowed = {
            "url", "title", "initial_voice_side", "status", "error_msg", "processed_at",
            "source_video_id", "series_index", "series_start_sec", "series_end_sec",
            "source_video_duration_sec", "series_detection_reason",
            "total_matches", "series_wins_voice", "series_draws", "series_losses_voice",
            "total_goals_voice", "total_goals_opp", "minibattle_mvp_player_id",
            "minibattle_blunder_player_id", "avg_self_tilt_index",
            "minibattle_report_md", "verification_status",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        params = list(updates.values()) + [video_id]
        self.execute(f"UPDATE minibattles SET {assignments} WHERE video_id = ?", params)

    def finalize_minibattle(
        self,
        video_id: str,
        summary: dict,
        report_md: str,
        verification_status: str,
    ) -> None:
        self.update_minibattle(
            video_id,
            total_matches=summary.get("total_matches", 0),
            series_wins_voice=summary.get("series_wins_voice", 0),
            series_draws=summary.get("series_draws", 0),
            series_losses_voice=summary.get("series_losses_voice", 0),
            total_goals_voice=summary.get("total_goals_voice", 0),
            total_goals_opp=summary.get("total_goals_opp", 0),
            minibattle_mvp_player_id=summary.get("minibattle_mvp_player_id"),
            minibattle_blunder_player_id=summary.get("minibattle_blunder_player_id"),
            avg_self_tilt_index=summary.get("avg_self_tilt_index", 0.0),
            minibattle_report_md=report_md,
            verification_status=verification_status,
            status="completed",
            error_msg=None,
            processed_at=datetime.now().isoformat(timespec="seconds"),
        )

    def rebuild_minibattle_artifacts(self, video_id: str) -> str | None:
        """Recompute MVP/blunder, summary and Markdown after manual edits.

        Event attribution is stored in match_events; the match/series MVP and
        the Markdown report must be rebuilt from the edited database rows.
        """
        from core import event_parser, metrics_engine

        minibattle = self.get_minibattle(video_id)
        if not minibattle:
            return None
        roster_rows = self.get_roster(video_id)
        players = []
        for row in roster_rows:
            players.append(
                {
                    "player_id": int(row["player_id"]),
                    "canonical_name": row["canonical_name"],
                    "team_role": row["team_role"],
                    "controller_num": row["controller_num"],
                    "field_position": row["field_position"],
                    "aliases": self._parse_aliases(row.get("aliases_json")),
                }
            )
        roster = {
            "players": players,
            "warnings": [],
            "voice_team_label": None,
            "opponent_team_label": None,
        }
        matches = self.get_matches(video_id)
        for match in matches:
            match["events"] = self.get_match_events(int(match["match_id"]))
        metrics = metrics_engine.apply_minibattle_metrics(matches, roster)
        summary = metrics["summary"]
        for match in matches:
            self.update_match(
                int(match["match_id"]),
                drama_score=match.get("drama_score", 0.0),
                comeback_flag=match.get("comeback_flag", 0),
                lead_blown_flag=match.get("lead_blown_flag", 0),
                self_tilt_index=match.get("self_tilt_index", 0.0),
                tech_complaints_count=match.get("tech_complaints_count", 0),
                mvp_player_id=match.get("mvp_player_id"),
                blunder_player_id=match.get("blunder_player_id"),
            )
        source_id = minibattle.get("source_video_id")
        series_count = 1
        if source_id:
            series_count = max(1, len(self.get_source_minibattles(source_id)))
        video = {
            "video_id": video_id,
            "source_video_id": source_id,
            "title": minibattle.get("title") or video_id,
            "url": minibattle.get("url"),
            "series_index": minibattle.get("series_index", 1),
            "series_count": series_count,
        }
        report = event_parser.build_minibattle_report(
            video=video,
            roster=roster,
            analysis={"matches": matches},
            summary=summary,
            metrics=metrics,
        )
        self.update_minibattle(
            video_id,
            total_matches=summary.get("total_matches", 0),
            series_wins_voice=summary.get("series_wins_voice", 0),
            series_draws=summary.get("series_draws", 0),
            series_losses_voice=summary.get("series_losses_voice", 0),
            total_goals_voice=summary.get("total_goals_voice", 0),
            total_goals_opp=summary.get("total_goals_opp", 0),
            minibattle_mvp_player_id=summary.get("minibattle_mvp_player_id"),
            minibattle_blunder_player_id=summary.get("minibattle_blunder_player_id"),
            avg_self_tilt_index=summary.get("avg_self_tilt_index", 0.0),
            minibattle_report_md=report,
        )
        config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = config.EXPORT_DIR / f"{video_id}_minibattle.md"
        path.write_text(report, encoding="utf-8")
        log.info("Rebuilt report and MVP for %s", video_id)
        return report

    def mark_minibattle_verified(self, video_id: str) -> None:
        # Picks up all manual attribution edits before flipping the status.
        self.rebuild_minibattle_artifacts(video_id)
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE matches SET verification_status = ? WHERE video_id = ?",
                (config.VERIFICATION_MANUAL, video_id),
            )
            self.conn.execute(
                "UPDATE minibattles SET verification_status = ? WHERE video_id = ?",
                (config.VERIFICATION_MANUAL, video_id),
            )

    # ------------------------------------------------------------------
    # Rosters
    # ------------------------------------------------------------------
    def replace_roster(self, video_id: str, roster: Iterable[dict]) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM minibattle_rosters WHERE video_id = ?", (video_id,))
            for entry in roster:
                self.conn.execute(
                    """INSERT OR REPLACE INTO minibattle_rosters
                       (video_id, player_id, team_role, controller_num, field_position)
                       VALUES(?, ?, ?, ?, ?)""",
                    (
                        video_id,
                        int(entry["player_id"]),
                        entry["team_role"],
                        entry.get("controller_num"),
                        entry.get("field_position") or "MF",
                    ),
                )

    def get_roster(self, video_id: str) -> list[dict]:
        return self.fetchall(
            """SELECT r.*, p.canonical_name, p.aliases_json
               FROM minibattle_rosters r
               JOIN players p ON p.player_id = r.player_id
               WHERE r.video_id = ?
               ORDER BY CASE r.team_role WHEN 'VOICE_TEAM' THEN 0 ELSE 1 END,
                        COALESCE(r.controller_num, 99), p.canonical_name""",
            (video_id,),
        )

    # ------------------------------------------------------------------
    # Matches / lineups / events
    # ------------------------------------------------------------------
    def insert_match(self, video_id: str, match: dict) -> int:
        cur = self.execute(
            """INSERT INTO matches(
                   video_id, match_num_in_minibattle, voice_side,
                   team_voice_country, team_opp_country, score_voice, score_opp,
                   match_outcome, start_video_sec, end_video_sec, duration_sec,
                   final_score_crop_path, verification_status
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                video_id,
                int(match["match_num_in_minibattle"]),
                match.get("voice_side", config.SIDE_RIGHT),
                match.get("team_voice_country"),
                match.get("team_opp_country"),
                int(match.get("score_voice", 0)),
                int(match.get("score_opp", 0)),
                match.get("match_outcome", "DRAW"),
                int(match.get("start_video_sec", 0)),
                int(match.get("end_video_sec", 0)),
                int(match.get("duration_sec", 0)),
                match.get("final_score_crop_path"),
                match.get("verification_status", config.VERIFICATION_REVIEW),
            ),
        )
        return int(cur.lastrowid)

    def update_match(self, match_id: int, **fields: Any) -> None:
        allowed = {
            "voice_side", "team_voice_country", "team_opp_country", "score_voice",
            "score_opp", "match_outcome", "start_video_sec", "end_video_sec",
            "duration_sec", "final_score_crop_path", "drama_score", "comeback_flag",
            "lead_blown_flag", "self_tilt_index", "tech_complaints_count",
            "mvp_player_id", "blunder_player_id", "verification_status",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        self.execute(
            f"UPDATE matches SET {assignments} WHERE match_id = ?",
            list(updates.values()) + [match_id],
        )

    def insert_lineup(self, match_id: int, video_id: str, entry: dict) -> int:
        cur = self.execute(
            """INSERT INTO match_lineups(
                   match_id, video_id, player_id, team_role, country_played,
                   controller_num, field_position, match_result, is_clean_sheet
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                match_id,
                video_id,
                int(entry["player_id"]),
                entry["team_role"],
                entry.get("country_played"),
                entry.get("controller_num"),
                entry.get("field_position") or "MF",
                entry.get("match_result", "DRAW"),
                int(entry.get("is_clean_sheet", 0)),
            ),
        )
        return int(cur.lastrowid)

    def insert_event(self, match_id: int, video_id: str, event: dict) -> int:
        cur = self.execute(
            """INSERT INTO match_events(
                   match_id, video_id, video_sec, video_timestamp_str, game_sec,
                   game_minute_bucket, half, scoring_team, score_snapshot,
                   is_clutch_goal, is_doublet_goal, author_player_id,
                   assist_player_id, blunder_player_id,
                   silence_after_conceded_sec, words_per_min_burst, voice_excerpt
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                match_id,
                video_id,
                int(event.get("video_sec", 0)),
                event.get("video_timestamp_str", "00:00:00"),
                int(event.get("game_sec", 0)),
                event.get("game_minute_bucket", "1m"),
                int(event.get("half", 1)),
                event["scoring_team"],
                event.get("score_snapshot", "0:0"),
                int(event.get("is_clutch_goal", 0)),
                int(event.get("is_doublet_goal", 0)),
                event.get("author_player_id"),
                event.get("assist_player_id"),
                event.get("blunder_player_id"),
                float(event.get("silence_after_conceded_sec", 0.0)),
                float(event.get("words_per_min_burst", 0.0)),
                event.get("voice_excerpt"),
            ),
        )
        return int(cur.lastrowid)

    def update_event(self, event_id: int, **fields: Any) -> None:
        allowed = {
            "video_sec", "video_timestamp_str", "game_sec", "game_minute_bucket",
            "half", "scoring_team", "score_snapshot", "is_clutch_goal",
            "is_doublet_goal", "author_player_id", "assist_player_id",
            "blunder_player_id", "silence_after_conceded_sec",
            "words_per_min_burst", "voice_excerpt",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        self.execute(
            f"UPDATE match_events SET {assignments} WHERE event_id = ?",
            list(updates.values()) + [event_id],
        )

    def get_matches(self, video_id: str) -> list[dict]:
        return self.fetchall(
            """SELECT m.*,
                      mv.canonical_name AS mvp_player_name,
                      bl.canonical_name AS blunder_player_name,
                      (SELECT COUNT(*) FROM match_events e WHERE e.match_id = m.match_id) AS goals_count
               FROM matches m
               LEFT JOIN players mv ON mv.player_id = m.mvp_player_id
               LEFT JOIN players bl ON bl.player_id = m.blunder_player_id
               WHERE m.video_id = ?
               ORDER BY m.match_num_in_minibattle""",
            (video_id,),
        )

    def get_events(self, video_id: str, match_id: int | None = None) -> list[dict]:
        params: list[Any] = [video_id]
        where = "e.video_id = ?"
        if match_id is not None:
            where += " AND e.match_id = ?"
            params.append(match_id)
        return self.fetchall(
            f"""SELECT e.*, m.match_num_in_minibattle,
                       a.canonical_name AS author_name,
                       s.canonical_name AS assist_name,
                       b.canonical_name AS blunder_name,
                       mb.url AS video_url
                FROM match_events e
                JOIN matches m ON m.match_id = e.match_id
                JOIN minibattles mb ON mb.video_id = e.video_id
                LEFT JOIN players a ON a.player_id = e.author_player_id
                LEFT JOIN players s ON s.player_id = e.assist_player_id
                LEFT JOIN players b ON b.player_id = e.blunder_player_id
                WHERE {where}
                ORDER BY e.video_sec""",
            params,
        )

    def get_match_events(self, match_id: int) -> list[dict]:
        return self.fetchall(
            """SELECT e.*, a.canonical_name AS author_name,
                      s.canonical_name AS assist_name,
                      b.canonical_name AS blunder_name
               FROM match_events e
               LEFT JOIN players a ON a.player_id = e.author_player_id
               LEFT JOIN players s ON s.player_id = e.assist_player_id
               LEFT JOIN players b ON b.player_id = e.blunder_player_id
               WHERE e.match_id = ?
               ORDER BY e.video_sec""",
            (match_id,),
        )

    def get_match(self, match_id: int) -> dict | None:
        return self.fetchone("SELECT * FROM matches WHERE match_id = ?", (match_id,))

    def get_lineups(self, match_id: int) -> list[dict]:
        return self.fetchall(
            """SELECT ml.*, p.canonical_name
               FROM match_lineups ml
               JOIN players p ON p.player_id = ml.player_id
               WHERE ml.match_id = ?
               ORDER BY CASE ml.team_role WHEN 'VOICE_TEAM' THEN 0 ELSE 1 END,
                        COALESCE(ml.controller_num, 99)""",
            (match_id,),
        )

    def get_minibattle_events_grouped(self, video_id: str) -> dict[int, list[dict]]:
        grouped: dict[int, list[dict]] = {}
        for event in self.get_events(video_id):
            grouped.setdefault(int(event["match_id"]), []).append(event)
        return grouped

    # ------------------------------------------------------------------
    # Career / analytics queries
    # ------------------------------------------------------------------
    def get_player_career(self, player_id: int) -> dict | None:
        return self.fetchone(
            "SELECT * FROM v_player_career_stats WHERE player_id = ?", (player_id,)
        )

    def get_player_position_stats(self, player_id: int) -> list[dict]:
        return self.fetchall(
            """WITH stats AS (
                   SELECT field_position,
                          COUNT(*) AS matches,
                          SUM(CASE WHEN match_result = 'WIN' THEN 1 ELSE 0 END) AS wins,
                          SUM(CASE WHEN match_result = 'DRAW' THEN 1 ELSE 0 END) AS draws,
                          SUM(CASE WHEN match_result = 'LOSS' THEN 1 ELSE 0 END) AS losses
                   FROM match_lineups
                   WHERE player_id = ?
                   GROUP BY field_position
               ),
               goal_stats AS (
                   SELECT ml.field_position, COUNT(e.event_id) AS goals
                   FROM match_lineups ml
                   JOIN match_events e
                     ON e.match_id = ml.match_id AND e.author_player_id = ml.player_id
                   WHERE ml.player_id = ?
                   GROUP BY ml.field_position
               )
               SELECT s.field_position, s.matches, s.wins, s.draws, s.losses,
                      ROUND(100.0 * s.wins / s.matches, 2) AS winrate_pct,
                      COALESCE(g.goals, 0) AS goals
               FROM stats s
               LEFT JOIN goal_stats g ON g.field_position = s.field_position
               ORDER BY CASE s.field_position WHEN 'FW' THEN 0 WHEN 'MF' THEN 1
                                              WHEN 'DF' THEN 2 ELSE 3 END""",
            (player_id, player_id),
        )

    def get_player_goal_timing(self, player_id: int) -> list[dict]:
        return self.fetchall(
            """SELECT game_minute_bucket, COUNT(*) AS goals
               FROM match_events
               WHERE author_player_id = ?
               GROUP BY game_minute_bucket
               ORDER BY CASE game_minute_bucket
                            WHEN '1m' THEN 1 WHEN '2m' THEN 2 WHEN '3m' THEN 3
                            WHEN '4m' THEN 4 WHEN '5m' THEN 5 ELSE 6 END""",
            (player_id,),
        )

    def get_player_history(self, player_id: int) -> list[dict]:
        return self.fetchall(
            """SELECT m.match_id, m.video_id, m.match_num_in_minibattle,
                      m.start_video_sec, m.final_score_crop_path,
                      mb.title, mb.url, mb.source_video_id, mb.series_index,
                      ml.team_role, ml.country_played, ml.field_position,
                      ml.controller_num, ml.match_result, ml.is_clean_sheet,
                      m.score_voice, m.score_opp, m.match_outcome,
                      m.team_voice_country, m.team_opp_country,
                      (SELECT COUNT(*) FROM match_events e
                       WHERE e.match_id = m.match_id AND e.author_player_id = ?) AS goals,
                      (SELECT COUNT(*) FROM match_events e
                       WHERE e.match_id = m.match_id AND e.assist_player_id = ?) AS assists,
                      (SELECT COUNT(*) FROM match_events e
                       WHERE e.match_id = m.match_id AND e.blunder_player_id = ?) AS blunders
               FROM match_lineups ml
               JOIN matches m ON m.match_id = ml.match_id
               JOIN minibattles mb ON mb.video_id = m.video_id
               WHERE ml.player_id = ?
               ORDER BY mb.processed_at DESC, m.match_num_in_minibattle""",
            (player_id, player_id, player_id, player_id),
        )

    def get_country_tier_list(self) -> list[dict]:
        return self.fetchall("SELECT * FROM v_country_tier_list ORDER BY winrate_pct DESC, matches_played DESC")

    def get_synergy_duos(self, limit: int = 50) -> list[dict]:
        return self.fetchall(
            "SELECT * FROM v_synergy_duos ORDER BY goals_together DESC, joint_winrate_pct DESC LIMIT ?",
            (limit,),
        )

    def get_h2h_matrix(self) -> list[dict]:
        return self.fetchall("SELECT * FROM v_h2h_matrix")

    def get_global_goal_timings(self) -> list[dict]:
        return self.fetchall(
            """SELECT game_minute_bucket, COUNT(*) AS goals
               FROM match_events
               GROUP BY game_minute_bucket
               ORDER BY CASE game_minute_bucket
                            WHEN '1m' THEN 1 WHEN '2m' THEN 2 WHEN '3m' THEN 3
                            WHEN '4m' THEN 4 WHEN '5m' THEN 5 ELSE 6 END"""
        )

    def get_top_drama_matches(self, limit: int = 20) -> list[dict]:
        return self.fetchall(
            """SELECT m.*, mb.title, mv.canonical_name AS mvp_player_name
               FROM matches m
               JOIN minibattles mb ON mb.video_id = m.video_id
               LEFT JOIN players mv ON mv.player_id = m.mvp_player_id
               ORDER BY m.drama_score DESC, ABS(m.score_voice - m.score_opp) ASC
               LIMIT ?""",
            (limit,),
        )

    def get_self_tilt_matches(self, limit: int = 20) -> list[dict]:
        return self.fetchall(
            """SELECT m.*, mb.title
               FROM matches m
               JOIN minibattles mb ON mb.video_id = m.video_id
               ORDER BY m.self_tilt_index DESC, m.tech_complaints_count DESC
               LIMIT ?""",
            (limit,),
        )

    def get_tech_complaint_correlation(self) -> list[dict]:
        return self.fetchall(
            """SELECT CASE WHEN tech_complaints_count > 0 THEN 'with_complaints'
                           ELSE 'without_complaints' END AS complaint_bucket,
                      match_outcome, COUNT(*) AS matches
               FROM matches
               GROUP BY CASE WHEN tech_complaints_count > 0 THEN 'with_complaints'
                             ELSE 'without_complaints' END,
                        match_outcome"""
        )

    def get_fatigue_series(self) -> list[dict]:
        return self.fetchall(
            """SELECT match_num_in_minibattle,
                      COUNT(*) AS matches,
                      SUM(CASE WHEN match_outcome = 'WIN' THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN match_outcome = 'DRAW' THEN 1 ELSE 0 END) AS draws,
                      SUM(CASE WHEN match_outcome = 'LOSS' THEN 1 ELSE 0 END) AS losses,
                      ROUND(100.0 * SUM(CASE WHEN match_outcome = 'WIN' THEN 1 ELSE 0 END)
                            / COUNT(*), 2) AS winrate_pct
               FROM matches
               GROUP BY match_num_in_minibattle
               ORDER BY match_num_in_minibattle"""
        )

    def get_review_queue(self) -> list[dict]:
        return self.fetchall(
            """SELECT * FROM minibattles
               WHERE verification_status = 'needs_review'
                 AND status = 'completed'
               ORDER BY rowid DESC"""
        )

    def get_dashboard_stats(self) -> dict:
        return {
            "videos": int((self.fetchone("SELECT COUNT(*) AS n FROM videos") or {"n": 0})["n"]),
            "completed_videos": int((self.fetchone("SELECT COUNT(*) AS n FROM videos WHERE status='completed'") or {"n": 0})["n"]),
            "minibattles": int((self.fetchone("SELECT COUNT(*) AS n FROM minibattles") or {"n": 0})["n"]),
            "completed": int((self.fetchone("SELECT COUNT(*) AS n FROM minibattles WHERE status='completed'") or {"n": 0})["n"]),
            "matches": int((self.fetchone("SELECT COUNT(*) AS n FROM matches") or {"n": 0})["n"]),
            "goals": int((self.fetchone("SELECT COUNT(*) AS n FROM match_events") or {"n": 0})["n"]),
            "players": int((self.fetchone("SELECT COUNT(*) AS n FROM players") or {"n": 0})["n"]),
            "review": int((self.fetchone("SELECT COUNT(*) AS n FROM minibattles WHERE verification_status='needs_review' AND status='completed'") or {"n": 0})["n"]),
        }

    def update_event_attribution(
        self,
        event_id: int,
        author_player_id: int | None,
        assist_player_id: int | None,
        blunder_player_id: int | None,
    ) -> None:
        event = self.fetchone(
            "SELECT video_id FROM match_events WHERE event_id = ?", (event_id,)
        )
        self.update_event(
            event_id,
            author_player_id=author_player_id,
            assist_player_id=assist_player_id,
            blunder_player_id=blunder_player_id,
        )
        if event:
            self.rebuild_minibattle_artifacts(event["video_id"])
