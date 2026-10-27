"""Advanced metrics for matches and entire minibattles.

All formulas are deterministic and documented in code.  They operate on the
parsed goal events, the voice roster and the final scoreboard scores.
"""
from __future__ import annotations

import logging
import math
from typing import Sequence

import config

log = logging.getLogger("goal3.metrics")

POSITION_ATTACK_ORDER = {"FW": 0, "MF": 1, "DF": 2, "GK": 3}


def _sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _is_clutch(game_sec: float, duration_sec: float) -> bool:
    """A goal in the final minute of either half (or of the match)."""
    boundaries = set()
    if duration_sec > config.HALF_LENGTH_SEC * 1.5:
        boundaries.add(config.HALF_LENGTH_SEC)
        boundaries.add(min(duration_sec, config.HALF_LENGTH_SEC * 2.0))
    else:
        boundaries.add(duration_sec)
    boundaries.add(duration_sec)
    return any(
        0 <= boundary - game_sec <= config.CLUTCH_WINDOW_SEC
        for boundary in boundaries
        if boundary > 0
    )


def _apply_goal_flags(match: dict) -> None:
    events = match.get("events") or []
    duration = float(match.get("duration_sec") or 0)
    last_opponent_goal = None
    for event in sorted(events, key=lambda e: (e.get("video_sec", 0), e.get("event_id", 0))):
        game_sec = float(event.get("game_sec") or 0)
        event["is_clutch_goal"] = 1 if _is_clutch(game_sec, duration) else 0
        if event.get("scoring_team") == config.OPPONENT_TEAM:
            if last_opponent_goal is not None and (
                game_sec - last_opponent_goal
            ) <= config.DOUBLET_WINDOW_SEC:
                event["is_doublet_goal"] = 1
            else:
                event["is_doublet_goal"] = 0
            last_opponent_goal = game_sec
        else:
            event["is_doublet_goal"] = 0


def _simulate(match: dict) -> dict:
    voice = 0
    opp = 0
    lead_changes = 0
    previous_leader = 0
    voice_was_behind = False
    voice_was_ahead = False
    events = sorted(match.get("events") or [], key=lambda e: e.get("video_sec", 0))
    for event in events:
        if event.get("scoring_team") == config.VOICE_TEAM:
            voice += 1
        else:
            opp += 1
        if voice < opp:
            voice_was_behind = True
        if voice > opp:
            voice_was_ahead = True
        leader = _sign(voice - opp)
        if leader != 0 and previous_leader != 0 and leader != previous_leader:
            lead_changes += 1
        if leader != 0:
            previous_leader = leader
    final_voice = int(match.get("score_voice", voice))
    final_opp = int(match.get("score_opp", opp))
    outcome = config.outcome_for_team(final_voice, final_opp)
    comeback = bool(voice_was_behind and outcome == "WIN")
    lead_blown = bool(voice_was_ahead and outcome != "WIN")
    return {
        "lead_changes": lead_changes,
        "comeback_flag": 1 if comeback else 0,
        "lead_blown_flag": 1 if lead_blown else 0,
        "simulated_voice": voice,
        "simulated_opp": opp,
    }


def drama_score(match: dict, sim: dict, events: Sequence[dict]) -> float:
    """Valium index: 0-10, higher is more dramatic."""
    total_goals = len(events)
    margin = abs(int(match.get("score_voice", 0)) - int(match.get("score_opp", 0)))
    clutch = sum(1 for e in events if e.get("is_clutch_goal"))
    comeback = int(sim.get("comeback_flag", 0))
    doublets = sum(1 for e in events if e.get("is_doublet_goal"))
    close_bonus = 1.5 if margin <= 1 and total_goals > 0 else 0.0
    blowout_penalty = 0.8 if margin >= 4 else 0.0
    raw = (
        total_goals * 0.45
        + sim.get("lead_changes", 0) * 1.4
        + clutch * 1.7
        + comeback * 1.5
        + doublets * 0.5
        + close_bonus
        - blowout_penalty
    )
    return round(max(0.0, min(10.0, raw)), 2)


def self_tilt_index(match: dict, roster: dict) -> float:
    events = match.get("events") or []
    conceded = [
        e for e in events if e.get("scoring_team") == config.OPPONENT_TEAM
    ]
    silences = [float(e.get("silence_after_conceded_sec") or 0) for e in conceded]
    avg_silence = sum(silences) / len(silences) if silences else 0.0
    wpm_values = [float(e.get("words_per_min_burst") or 0) for e in events]
    avg_wpm = sum(wpm_values) / len(wpm_values) if wpm_values else 0.0
    complaints = int(match.get("tech_complaints_count") or 0)
    rage = 0
    for event in events:
        excerpt = (event.get("voice_excerpt") or "").lower()
        rage += sum(excerpt.count(marker) for marker in ("блять", "сука", "пиздец", "нахуй", "бля"))
    score = (
        avg_silence * 1.35
        + complaints * 3.6
        + rage * 4.0
        + max(0.0, 75.0 - avg_wpm) * 0.22
    )
    return round(max(0.0, min(100.0, score)), 2)


def _player_match_points(match: dict, roster: dict) -> tuple[dict[int, float], dict[int, dict]]:
    points: dict[int, float] = {}
    stats: dict[int, dict] = {}
    for player in roster.get("players") or []:
        pid = int(player["player_id"])
        stats[pid] = {
            "player_id": pid,
            "name": player.get("canonical_name"),
            "team_role": player.get("team_role"),
            "position": player.get("field_position"),
            "goals": 0,
            "assists": 0,
            "blunders": 0,
            "clutch": 0,
        }
        points[pid] = 0.0
    for event in match.get("events") or []:
        author = event.get("author_player_id")
        assist = event.get("assist_player_id")
        blunder = event.get("blunder_player_id")
        if author in stats:
            stats[author]["goals"] += 1
            points[author] += 3.0
            if event.get("is_clutch_goal"):
                stats[author]["clutch"] += 1
                points[author] += 0.7
        if assist in stats:
            stats[assist]["assists"] += 1
            points[assist] += 2.0
        if blunder in stats:
            stats[blunder]["blunders"] += 1
            points[blunder] -= 1.5
    for pid, stat in stats.items():
        if stat["team_role"] == config.VOICE_TEAM:
            if match.get("match_outcome") == "WIN":
                points[pid] += 2.0
            elif match.get("match_outcome") == "DRAW":
                points[pid] += 0.5
            if int(match.get("score_opp", 0)) == 0 and stat["position"] in {"GK", "DF"}:
                points[pid] += 0.8
    return points, stats


def compute_match_metrics(match: dict, roster: dict) -> dict:
    events = match.get("events") or []
    _apply_goal_flags(match)
    sim = _simulate(match)
    match["comeback_flag"] = sim["comeback_flag"]
    match["lead_blown_flag"] = sim["lead_blown_flag"]
    match["drama_score"] = drama_score(match, sim, events)
    match["self_tilt_index"] = self_tilt_index(match, roster)
    points, stats = _player_match_points(match, roster)
    # MVP is chosen from the voice team because only its contributions are
    # observable in the recording.  The winning team's players still receive
    # an outcome bonus, so a defender/keeper can win a clean-sheet match.
    voice_ids = [
        int(p["player_id"])
        for p in roster.get("players") or []
        if p.get("team_role") == config.VOICE_TEAM
    ]
    if voice_ids:
        mvp_id = max(
            voice_ids,
            key=lambda pid: (
                points.get(pid, 0.0),
                POSITION_ATTACK_ORDER.get(stats.get(pid, {}).get("position", "MF"), 9) * -1,
                -pid,
            ),
        )
        if points.get(mvp_id, 0.0) > 0:
            match["mvp_player_id"] = mvp_id
        else:
            match["mvp_player_id"] = None
    else:
        match["mvp_player_id"] = None
    blunder_ids = [
        pid for pid, stat in stats.items() if stat["blunders"] > 0
    ]
    if blunder_ids:
        match["blunder_player_id"] = max(
            blunder_ids,
            key=lambda pid: (stats[pid]["blunders"], -pid),
        )
    else:
        match["blunder_player_id"] = None
    match["_player_points"] = points
    match["_player_stats"] = stats
    match["_sim"] = sim
    return {
        "drama_score": match["drama_score"],
        "self_tilt_index": match["self_tilt_index"],
        "mvp_player_id": match["mvp_player_id"],
        "blunder_player_id": match["blunder_player_id"],
        "lead_changes": sim["lead_changes"],
        "comeback_flag": match["comeback_flag"],
        "lead_blown_flag": match["lead_blown_flag"],
    }


def _fatigue(matches: Sequence[dict]) -> list[dict]:
    grouped: dict[int, dict] = {}
    for match in matches:
        num = int(match.get("match_num_in_minibattle") or 0)
        row = grouped.setdefault(
            num,
            {"match_num_in_minibattle": num, "matches": 0, "wins": 0, "draws": 0, "losses": 0},
        )
        row["matches"] += 1
        if match.get("match_outcome") == "WIN":
            row["wins"] += 1
        elif match.get("match_outcome") == "DRAW":
            row["draws"] += 1
        else:
            row["losses"] += 1
    result = []
    for num in sorted(grouped):
        row = grouped[num]
        row["winrate_pct"] = round(100.0 * row["wins"] / max(1, row["matches"]), 2)
        result.append(row)
    return result


def _series_note(fatigue: list[dict]) -> str:
    if not fatigue:
        return ""
    early = [r for r in fatigue if r["match_num_in_minibattle"] <= 2]
    late = [r for r in fatigue if r["match_num_in_minibattle"] >= 5]
    if not early or not late:
        return ""
    early_matches = sum(r["matches"] for r in early)
    early_wins = sum(r["wins"] for r in early)
    late_matches = sum(r["matches"] for r in late)
    late_wins = sum(r["wins"] for r in late)
    if early_matches == 0 or late_matches == 0:
        return ""
    early_wr = 100.0 * early_wins / early_matches
    late_wr = 100.0 * late_wins / late_matches
    if late_wr + 5 < early_wr:
        return (
            f"**Усталость в серии:** винрейт на старте {early_wr:.0f}% "
            f"против {late_wr:.0f}% в концовке — команда проседает."
        )
    if late_wr > early_wr + 5:
        return (
            f"**Разгон в серии:** винрейт на старте {early_wr:.0f}% "
            f"против {late_wr:.0f}% в концовке — команда прибавляет."
        )
    return (
        f"**Стабильность в серии:** винрейт на старте {early_wr:.0f}% "
        f"и в концовке {late_wr:.0f}%."
    )


def apply_minibattle_metrics(matches: Sequence[dict], roster: dict) -> dict:
    """Mutates matches with match-level metrics and returns series summary."""
    for match in matches:
        compute_match_metrics(match, roster)
    fatigue = _fatigue(matches)

    # Series-level score is based on the verified final scoreboard, not on the
    # number of parsed commentary events (which may miss a muted goal).
    total_voice_goals = sum(int(m.get("score_voice", 0)) for m in matches)
    total_opp_goals = sum(int(m.get("score_opp", 0)) for m in matches)
    wins = sum(1 for m in matches if m.get("match_outcome") == "WIN")
    draws = sum(1 for m in matches if m.get("match_outcome") == "DRAW")
    losses = sum(1 for m in matches if m.get("match_outcome") == "LOSS")
    avg_tilt = (
        round(sum(float(m.get("self_tilt_index", 0.0)) for m in matches) / len(matches), 2)
        if matches
        else 0.0
    )

    aggregate_points: dict[int, float] = {}
    aggregate_blunders: dict[int, int] = {}
    aggregate_goals: dict[int, int] = {}
    aggregate_assists: dict[int, int] = {}
    for match in matches:
        for pid, points in (match.get("_player_points") or {}).items():
            aggregate_points[pid] = aggregate_points.get(pid, 0.0) + float(points)
        for pid, stats in (match.get("_player_stats") or {}).items():
            aggregate_blunders[pid] = aggregate_blunders.get(pid, 0) + int(stats["blunders"])
            aggregate_goals[pid] = aggregate_goals.get(pid, 0) + int(stats["goals"])
            aggregate_assists[pid] = aggregate_assists.get(pid, 0) + int(stats["assists"])
    voice_ids = [
        int(p["player_id"])
        for p in roster.get("players") or []
        if p.get("team_role") == config.VOICE_TEAM
    ]
    mvp_id = None
    if voice_ids and aggregate_points:
        candidate = max(
            voice_ids,
            key=lambda pid: (
                aggregate_points.get(pid, 0.0),
                aggregate_goals.get(pid, 0),
                -pid,
            ),
        )
        if aggregate_points.get(candidate, 0.0) > 0:
            mvp_id = candidate
    blunder_id = None
    if aggregate_blunders:
        candidate = max(aggregate_blunders, key=lambda pid: (aggregate_blunders[pid], -pid))
        if aggregate_blunders[candidate] > 0:
            blunder_id = candidate

    summary = {
        "total_matches": len(matches),
        "series_wins_voice": wins,
        "series_draws": draws,
        "series_losses_voice": losses,
        "total_goals_voice": total_voice_goals,
        "total_goals_opp": total_opp_goals,
        "minibattle_mvp_player_id": mvp_id,
        "minibattle_blunder_player_id": blunder_id,
        "avg_self_tilt_index": avg_tilt,
        "series_note": _series_note(fatigue),
        "fatigue": fatigue,
        "player_points": {str(k): round(v, 2) for k, v in sorted(aggregate_points.items())},
        "clutch_goals": sum(
            1 for m in matches for e in (m.get("events") or []) if e.get("is_clutch_goal")
        ),
        "doublet_goals": sum(
            1 for m in matches for e in (m.get("events") or []) if e.get("is_doublet_goal")
        ),
    }
    log.info(
        "Series metrics: %d matches, %d-%d-%d, goals %d:%d, MVP=%s, blunder=%s",
        len(matches),
        wins,
        draws,
        losses,
        total_voice_goals,
        total_opp_goals,
        mvp_id,
        blunder_id,
    )
    return {
        "summary": summary,
        "fatigue": fatigue,
        "matches": matches,
        "clutch_goals": summary["clutch_goals"],
        "doublet_goals": summary["doublet_goals"],
    }


def build_fatigue_series(matches: Sequence[dict]) -> list[dict]:
    return _fatigue(matches)
