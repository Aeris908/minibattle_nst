"""Deterministic voice parser and minibattle Markdown report builder.

No LLM and no external APIs are used.  Player mentions are found with
rapidfuzz, then goal authors, assists and blunders are selected from markers,
timing windows, player positions and the deterministic two-matches-per-side
rotation rule.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Iterable, Sequence

from rapidfuzz import fuzz

import config

log = logging.getLogger("goal3.events")


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
POSITION_WEIGHT = {"FW": 0.10, "MF": 0.06, "DF": 0.0, "GK": -0.04}
BLUNDER_POSITION_WEIGHT = {"GK": 0.08, "DF": 0.10, "MF": 0.03, "FW": -0.03}


def _normalize(value: str) -> str:
    return config.normalize_text(value)


def _marker_hits(text: str, markers: Iterable[str]) -> int:
    norm = _normalize(text)
    hits = 0
    for marker in markers:
        marker_norm = _normalize(marker)
        if not marker_norm:
            continue
        if len(marker_norm) <= 5 and " " not in marker_norm:
            # Short markers ("пас", "гол", "лаг") must be whole words: this
            # prevents "спас" -> assist and "голкипер" -> goal.
            hits += len(
                re.findall(rf"(?<!\w){re.escape(marker_norm)}(?!\w)", norm)
            )
        else:
            hits += len(re.findall(re.escape(marker_norm), norm))
    return hits


def _has_marker(text: str, markers: Iterable[str]) -> bool:
    return _marker_hits(text, markers) > 0


def _segments_in(
    transcript: dict | None,
    start_abs: float,
    end_abs: float,
) -> list[dict]:
    if not transcript:
        return []
    result = []
    for segment in transcript.get("segments") or []:
        s = float(segment.get("start", 0))
        e = float(segment.get("end", s))
        if e < start_abs or s > end_abs:
            continue
        result.append(segment)
    return result


def _segment_mentions(
    segment: dict,
    players: Sequence[dict],
    cutoff: float = 80.0,
) -> list[dict]:
    text = segment.get("text", "")
    norm = _normalize(text)
    if not norm:
        return []
    words = set(norm.split())
    candidates: list[dict] = []
    for player in players:
        aliases = [player.get("canonical_name") or "", *(player.get("aliases") or [])]
        best_score = 0.0
        best_alias = ""
        for alias in aliases:
            alias_norm = _normalize(alias)
            if len(alias_norm) < 2:
                continue
            score = 0.0
            if alias_norm in words:
                score = 100.0
            elif len(alias_norm) >= 3 and alias_norm in norm:
                score = 98.0
            elif len(alias_norm) >= 4:
                # Handle Russian case endings without a stemmer dependency.
                stem = alias_norm[:4]
                if any(word.startswith(stem) for word in words if len(word) >= 4):
                    score = 94.0
                else:
                    ratio = fuzz.partial_ratio(alias_norm, norm)
                    if ratio >= 88:
                        score = float(ratio)
            elif alias_norm in words:
                score = 100.0
            if score > best_score:
                best_score = score
                best_alias = alias
        required = cutoff if len(best_alias) >= 4 else max(cutoff, 88.0)
        if best_score >= required:
            candidates.append(
                {
                    "player_id": int(player["player_id"]),
                    "name": player.get("canonical_name"),
                    "team_role": player.get("team_role"),
                    "position": player.get("field_position"),
                    "score": round(best_score, 2),
                    "alias": best_alias,
                    "segment": segment,
                    "text": text,
                }
            )
    return sorted(candidates, key=lambda c: -c["score"])


def _collect_mentions(
    transcript: dict | None,
    players: Sequence[dict],
    start_abs: float,
    end_abs: float,
) -> list[dict]:
    mentions: list[dict] = []
    for segment in _segments_in(transcript, start_abs, end_abs):
        mentions.extend(_segment_mentions(segment, players))
    return mentions


def _excerpt(transcript: dict | None, start_abs: float, end_abs: float, max_chars: int | None = None) -> str:
    segments = _segments_in(transcript, start_abs, end_abs)
    lines = []
    for segment in segments:
        s = int(round(float(segment.get("start", 0))))
        text = (segment.get("text") or "").strip()
        if text:
            lines.append(f"[{config.hhmmss(s)}] {text}")
    value = "\n".join(lines)
    limit = max_chars or config.VOICE_EXCERPT_MAX_CHARS
    if len(value) > limit:
        value = value[: limit - 1] + "…"
    return value


def _silence_after_goal(transcript: dict | None, goal_video_sec: float) -> float:
    if not transcript:
        return 0.0
    segments = transcript.get("segments") or []
    # If somebody is speaking across the goal, there is no silence.
    for segment in segments:
        s = float(segment.get("start", 0))
        e = float(segment.get("end", s))
        if s <= goal_video_sec <= e and (segment.get("text") or "").strip():
            return 0.0
    next_start = None
    for segment in segments:
        s = float(segment.get("start", 0))
        if s > goal_video_sec and (segment.get("text") or "").strip():
            next_start = s if next_start is None else min(next_start, s)
    if next_start is None:
        return max(0.0, float(transcript.get("end", goal_video_sec)) - goal_video_sec)
    return max(0.0, next_start - goal_video_sec)


def _words_per_min_burst(transcript: dict | None) -> float:
    if not transcript:
        return 0.0
    try:
        return float(transcript.get("words_per_min") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _country_occurrences(text: str) -> list[tuple[int, int, str]]:
    norm = _normalize(text)
    found: list[tuple[int, int, str]] = []
    for canonical, aliases in config.COUNTRY_ALIASES.items():
        for alias in [canonical, *aliases]:
            alias_norm = _normalize(alias)
            if not alias_norm:
                continue
            for match in re.finditer(rf"(?<!\w){re.escape(alias_norm)}(?!\w)", norm):
                found.append((match.start(), match.end(), canonical))
    found.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    # Remove overlapping matches of the same country.
    result: list[tuple[int, int, str]] = []
    occupied: list[tuple[int, int]] = []
    for start, end, canonical in found:
        if any(not (end <= a or start >= b) for a, b in occupied):
            continue
        occupied.append((start, end))
        result.append((start, end, canonical))
    result.sort()
    return result


def _looks_country(text: str) -> str | None:
    norm = _normalize(text)
    for canonical, aliases in config.COUNTRY_ALIASES.items():
        for alias in [canonical, *aliases]:
            alias_norm = _normalize(alias)
            if alias_norm and (norm == alias_norm or re.search(rf"(?<!\w){re.escape(alias_norm)}(?!\w)", norm)):
                return canonical
    return None


def parse_match_countries(
    prematch_transcript: dict | None,
    postgame_transcript: dict | None,
    voice_team_label: str | None,
    opponent_team_label: str | None,
) -> tuple[str, str]:
    """Identify the two countries selected for this match from voice."""
    text = " ".join(
        [prematch_transcript.get("text", "") if prematch_transcript else "",
         postgame_transcript.get("text", "") if postgame_transcript else ""]
    )
    norm = _normalize(text)
    occurrences = _country_occurrences(text)
    voice_country = None
    opponent_country = None
    other_countries: list[str] = []
    for start, end, canonical in occurrences:
        prefix = norm[max(0, start - 50):start]
        if re.search(
            r"(против|соперник|оппонент|у них|их сборн|враг|играем с|играем со)",
            prefix,
        ):
            if opponent_country is None:
                opponent_country = canonical
            continue
        if re.search(
            r"(мы|наш|берем|берём|выбира|выбрали|взяли|пик|играем за|за нас)",
            prefix,
        ):
            if voice_country is None:
                voice_country = canonical
            continue
        if canonical not in other_countries:
            other_countries.append(canonical)
    if voice_country is None and other_countries:
        voice_country = other_countries.pop(0)
    if opponent_country is None and other_countries:
        opponent_country = other_countries.pop(0)
    label_voice = _looks_country(voice_team_label or "")
    label_opp = _looks_country(opponent_team_label or "")
    if voice_country is None:
        voice_country = label_voice
    if opponent_country is None:
        opponent_country = label_opp if label_opp and label_opp != voice_country else None
    if opponent_country is None:
        for canonical in other_countries:
            if canonical != voice_country:
                opponent_country = canonical
                break
    if opponent_country is None and voice_country is None:
        # Last-resort distinction for the dashboard; the match is flagged for
        # review by the caller.
        voice_country = label_voice or config.UNKNOWN_COUNTRY
        opponent_country = label_opp or config.UNKNOWN_COUNTRY
    voice_country = voice_country or config.UNKNOWN_COUNTRY
    opponent_country = opponent_country or config.UNKNOWN_COUNTRY
    if voice_country == opponent_country:
        opponent_country = label_opp if label_opp and label_opp != voice_country else config.UNKNOWN_COUNTRY
    return voice_country, opponent_country


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------
def _position_bonus(player: dict) -> float:
    return POSITION_WEIGHT.get(player.get("field_position") or "MF", 0.0)


def _pick_assist(
    mentions: list[dict],
    players_by_id: dict[int, dict],
    goal_sec: float,
    lower_bound: float,
    author_id: int | None = None,
) -> int | None:
    candidates = []
    for mention in mentions:
        seg = mention["segment"]
        seg_start = float(seg.get("start", 0))
        seg_end = float(seg.get("end", seg_start))
        if seg_end > goal_sec + 2.0 or seg_start < lower_bound:
            continue
        if not _has_marker(seg.get("text", ""), config.ASSIST_MARKERS):
            continue
        pid = mention["player_id"]
        if author_id is not None and pid == author_id:
            continue
        player = players_by_id.get(pid, {})
        if player.get("team_role") != config.VOICE_TEAM:
            continue
        score = mention["score"] / 100.0 + _position_bonus(player)
        score += min(0.15, _marker_hits(seg.get("text", ""), config.ASSIST_MARKERS) * 0.05)
        candidates.append((score, pid))
    if not candidates:
        return None
    candidates.sort(key=lambda item: -item[0])
    return candidates[0][1]


def _pick_author(
    mentions: list[dict],
    players_by_id: dict[int, dict],
    goal_sec: float,
    upper_bound: float,
    assist_id: int | None = None,
) -> int | None:
    candidates = []
    for mention in mentions:
        pid = mention["player_id"]
        if assist_id is not None and pid == assist_id:
            continue
        seg = mention["segment"]
        seg_start = float(seg.get("start", 0))
        seg_end = float(seg.get("end", seg_start))
        if seg_start > upper_bound:
            continue
        player = players_by_id.get(pid, {})
        if player.get("team_role") != config.VOICE_TEAM:
            continue
        score = mention["score"] / 100.0 + _position_bonus(player)
        text = seg.get("text", "")
        if _has_marker(text, config.AUTHOR_MARKERS):
            score += 0.20
        if _has_marker(text, config.PRAISE_MARKERS):
            score += 0.15
        if seg_start >= goal_sec - 2.0:
            score += 0.12
        if seg_end < goal_sec - 10.0:
            score -= 0.10
        candidates.append((score, pid))
    if not candidates:
        return None
    candidates.sort(key=lambda item: -item[0])
    return candidates[0][1]


def _pick_blunder(
    mentions: list[dict],
    players_by_id: dict[int, dict],
    goal_sec: float,
    lower_bound: float,
    upper_bound: float,
) -> int | None:
    candidates = []
    for mention in mentions:
        seg = mention["segment"]
        seg_start = float(seg.get("start", 0))
        seg_end = float(seg.get("end", seg_start))
        if seg_end < lower_bound or seg_start > upper_bound:
            continue
        text = seg.get("text", "")
        blunder_hits = _marker_hits(text, config.BLUNDER_MARKERS)
        rage_hits = _marker_hits(text, config.RAGE_MARKERS)
        tech_hits = _marker_hits(text, config.TECH_COMPLAINT_MARKERS)
        if blunder_hits + rage_hits + tech_hits == 0:
            continue
        pid = mention["player_id"]
        player = players_by_id.get(pid, {})
        if player.get("team_role") != config.VOICE_TEAM:
            continue
        posture = BLUNDER_POSITION_WEIGHT.get(player.get("field_position") or "MF", 0.0)
        score = mention["score"] / 100.0 + posture
        score += min(0.30, blunder_hits * 0.12 + rage_hits * 0.05 + tech_hits * 0.04)
        if seg_start >= goal_sec:
            score += 0.08
        candidates.append((score, pid))
    if not candidates:
        return None
    candidates.sort(key=lambda item: -item[0])
    return candidates[0][1]


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------
def _tech_complaints_for_match(transcripts: Sequence[dict]) -> int:
    return sum(_marker_hits(t.get("text", ""), config.TECH_COMPLAINT_MARKERS) for t in transcripts)


def parse_minibattle(
    matches: Sequence[dict],
    roster: dict,
    transcripts: dict[str, dict],
    initial_voice_side: str = config.SIDE_RIGHT,
    video_url: str = "",
) -> dict:
    players = list(roster.get("players") or [])
    players_by_id = {int(p["player_id"]): p for p in players}
    voice_team_label = roster.get("voice_team_label")
    opponent_team_label = roster.get("opponent_team_label")
    parsed_matches: list[dict] = []
    total_voice_goals = 0
    total_opp_goals = 0
    global_issues: list[str] = []

    for order, match in enumerate(matches, start=1):
        num = int(match.get("match_num_in_minibattle") or order)
        voice_side = config.rotate_voice_side(initial_voice_side, num)
        match["voice_side"] = voice_side
        pre = transcripts.get(f"m{num}_pre")
        post = transcripts.get(f"m{num}_post")
        voice_country, opp_country = parse_match_countries(
            pre, post, voice_team_label, opponent_team_label
        )
        match["team_voice_country"] = voice_country
        match["team_opp_country"] = opp_country
        if config.UNKNOWN_COUNTRY in (voice_country, opp_country):
            global_issues.append(f"Match #{num}: country selection was not recognized")

        goals = sorted(match.get("goals") or [], key=lambda g: float(g.get("video_sec", 0)))
        events: list[dict] = []
        score_voice = 0
        score_opp = 0
        goal_times = [float(g.get("video_sec", 0)) for g in goals]
        for goal_index, goal in enumerate(goals):
            goal_video_sec = float(goal.get("video_sec", 0))
            scoring_team = config.side_to_team_role(goal.get("scoring_side", "LEFT"), voice_side)
            if scoring_team == config.VOICE_TEAM:
                score_voice += 1
            else:
                score_opp += 1
            transcript = transcripts.get(f"m{num}_g{goal_index}")
            prev_goal = goal_times[goal_index - 1] if goal_index > 0 else -1e9
            next_goal = goal_times[goal_index + 1] if goal_index + 1 < len(goal_times) else 1e9
            pre_lower = max(goal_video_sec - 15.0, prev_goal + 1.0 if prev_goal > -1e8 else 0.0)
            post_upper = min(goal_video_sec + 25.0, next_goal - 1.0 if next_goal < 1e8 else goal_video_sec + 25.0)
            mentions = _collect_mentions(
                transcript, players, goal_video_sec - 20.0, goal_video_sec + 35.0
            )
            author_id = None
            assist_id = None
            blunder_id = None
            review_reasons: list[str] = []
            if scoring_team == config.VOICE_TEAM:
                assist_id = _pick_assist(
                    mentions, players_by_id, goal_video_sec, pre_lower, author_id=None
                )
                author_id = _pick_author(
                    mentions, players_by_id, goal_video_sec, post_upper, assist_id=assist_id
                )
                if author_id is None and mentions:
                    # Remove the assist-only restriction if no other candidate
                    # was available, but still never assign the assister as
                    # the scorer and never attribute a voice goal to the
                    # opponent team.
                    alternatives = [
                        m
                        for m in mentions
                        if m["player_id"] != assist_id
                        and players_by_id.get(m["player_id"], {}).get("team_role") == config.VOICE_TEAM
                    ]
                    if alternatives:
                        alternatives.sort(key=lambda m: (-m["score"], -POSITION_WEIGHT.get(players_by_id.get(m["player_id"], {}).get("field_position", "MF"), 0.0)))
                        author_id = alternatives[0]["player_id"]
                if author_id is None:
                    review_reasons.append("Автор гола не распознан")
                if author_id is not None and author_id == assist_id:
                    assist_id = None
            else:
                blunder_id = _pick_blunder(
                    mentions,
                    players_by_id,
                    goal_video_sec,
                    goal_video_sec - 15.0,
                    goal_video_sec + 25.0,
                )
                if blunder_id is None:
                    review_reasons.append("Привоз не распознан")

            game_sec = max(0, int(round(goal_video_sec - float(match.get("start_video_sec", 0)))))
            event = {
                "video_sec": int(round(goal_video_sec)),
                "video_timestamp_str": config.hhmmss(goal_video_sec),
                "game_sec": game_sec,
                "game_minute_bucket": config.minute_bucket(game_sec),
                "half": config.half_for_game_sec(game_sec),
                "scoring_team": scoring_team,
                "score_snapshot": f"{score_voice}:{score_opp}",
                "author_player_id": author_id,
                "assist_player_id": assist_id,
                "blunder_player_id": blunder_id,
                "silence_after_conceded_sec": round(
                    _silence_after_goal(transcript, goal_video_sec), 2
                ),
                "words_per_min_burst": _words_per_min_burst(transcript),
                "voice_excerpt": _excerpt(
                    transcript, goal_video_sec - 20.0, goal_video_sec + 35.0
                ),
                "is_clutch_goal": 0,
                "is_doublet_goal": 0,
                "review_reasons": review_reasons,
            }
            if scoring_team == config.VOICE_TEAM:
                total_voice_goals += 1
            else:
                total_opp_goals += 1
            events.append(event)

        if voice_side == config.SIDE_LEFT:
            final_voice = int(match.get("score_left", score_voice))
            final_opp = int(match.get("score_right", score_opp))
        else:
            final_voice = int(match.get("score_right", score_voice))
            final_opp = int(match.get("score_left", score_opp))
        match_outcome = config.outcome_for_team(final_voice, final_opp)
        match_transcripts = [
            t
            for key, t in transcripts.items()
            if key.startswith(f"m{num}_")
        ]
        tech_count = _tech_complaints_for_match(match_transcripts)
        issues = list(match.get("issues") or [])
        if len(events) != final_voice + final_opp:
            issues.append(
                f"Integrity: events={len(events)}, final scoreboard={final_voice + final_opp}"
            )
        if voice_country == config.UNKNOWN_COUNTRY or opp_country == config.UNKNOWN_COUNTRY:
            issues.append("Country selection is unknown")
        for event in events:
            issues.extend(event.get("review_reasons") or [])
        match.update(
            {
                "match_num_in_minibattle": num,
                "score_voice": final_voice,
                "score_opp": final_opp,
                "match_outcome": match_outcome,
                "events": events,
                "tech_complaints_count": tech_count,
                "issues": issues,
                "verification_status": (
                    config.VERIFICATION_REVIEW if issues else config.VERIFICATION_AUTO
                ),
                "url": video_url,
                "voice_team_label": voice_team_label,
                "opponent_team_label": opponent_team_label,
            }
        )
        parsed_matches.append(match)
        log.info(
            "Parsed match #%d: %s %d:%d, %d events, %d issue(s)",
            num,
            match_outcome,
            final_voice,
            final_opp,
            len(events),
            len(issues),
        )

    return {
        "matches": parsed_matches,
        "total_goals_voice": total_voice_goals,
        "total_goals_opp": total_opp_goals,
        "roster": roster,
        "initial_voice_side": initial_voice_side,
        "voice_team_label": voice_team_label,
        "opponent_team_label": opponent_team_label,
        "issues": global_issues,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def _md_escape(value: object) -> str:
    return str(value if value is not None else "—").replace("|", "\\|").replace("\n", " ")


def _youtube_link(url: str, seconds: int, label: str) -> str:
    if not url:
        return label
    joiner = "&" if "?" in url else "?"
    return f"[{label}]({url}{joiner}t={max(0, int(seconds))}s)"


def _event_identity(event: dict, names: dict[int, str]) -> str:
    parts = []
    if event.get("author_player_id"):
        parts.append(f"⚽ {names.get(event['author_player_id'], 'игрок')}")
    if event.get("assist_player_id"):
        parts.append(f"🅰 {names.get(event['assist_player_id'], 'игрок')}")
    if event.get("blunder_player_id"):
        parts.append(f"❌ {names.get(event['blunder_player_id'], 'игрок')}")
    return " · ".join(parts) if parts else "автор не распознан"


def build_minibattle_report(
    video: dict,
    roster: dict,
    analysis: dict,
    summary: dict,
    metrics: dict,
) -> str:
    matches = analysis.get("matches") or []
    names = {int(p["player_id"]): p["canonical_name"] for p in roster.get("players") or []}
    title = video.get("title") or video.get("video_id") or "Минибатл Goal 3"
    url = video.get("url") or ""
    lines: list[str] = []
    lines.append(f"# Минибатл: {title}")
    lines.append("")
    series_index = video.get("series_index")
    series_count = video.get("series_count")
    if series_index and series_count:
        lines.append(f"**Серия в видео:** {series_index} из {series_count}  ")
    if video.get("source_video_id"):
        lines.append(f"**Исходное видео:** `{video['source_video_id']}`  ")
    if url:
        lines.append(f"**Источник:** [{url}]({url})  ")
    lines.append(f"**Обработано:** {datetime.now().strftime('%Y-%m-%d %H:%M')}  ")
    lines.append(f"**Матчей:** {summary.get('total_matches', len(matches))}  ")
    lines.append(
        f"**Счёт серии (войс):** {summary.get('series_wins_voice', 0)} побед / "
        f"{summary.get('series_draws', 0)} ничьих / "
        f"{summary.get('series_losses_voice', 0)} поражений"
    )
    lines.append(
        f"**Голы:** {summary.get('total_goals_voice', 0)} — "
        f"{summary.get('total_goals_opp', 0)}"
    )
    mvp_id = summary.get("minibattle_mvp_player_id")
    blunder_id = summary.get("minibattle_blunder_player_id")
    if mvp_id:
        lines.append(f"**MVP серии:** {names.get(mvp_id, mvp_id)}")
    if blunder_id:
        lines.append(f"**Антигерой серии:** {names.get(blunder_id, blunder_id)}")
    lines.append("")
    lines.append("## Составы и позиции")
    lines.append("")
    for role, caption in ((config.VOICE_TEAM, "Команда войса"), (config.OPPONENT_TEAM, "Соперник")):
        lines.append(f"**{caption}**")
        lines.append("")
        lines.append("| Игрок | Позиция | Контроллер |")
        lines.append("|---|---|---|")
        for player in roster.get("players") or []:
            if player.get("team_role") != role:
                continue
            lines.append(
                f"| {_md_escape(player.get('canonical_name'))} | "
                f"{_md_escape(player.get('field_position'))} | "
                f"{_md_escape(player.get('controller_num'))} |"
            )
        lines.append("")
    if roster.get("warnings"):
        lines.append("> ⚠ Ростер: " + "; ".join(roster["warnings"]))
        lines.append("")

    lines.append("## Матчи серии")
    for match in matches:
        num = match.get("match_num_in_minibattle")
        lines.append("")
        lines.append(
            f"### Матч #{num}: {match.get('team_voice_country', '?')} — "
            f"{match.get('team_opp_country', '?')} · "
            f"{match.get('score_voice', 0)}:{match.get('score_opp', 0)}"
        )
        lines.append("")
        lines.append(
            f"- Исход: **{match.get('match_outcome')}**; сторона войса: `{match.get('voice_side')}`"
        )
        lines.append(
            f"- Таймкод: {_youtube_link(url, int(match.get('start_video_sec', 0)), config.hhmmss(match.get('start_video_sec')))}"
            f" — {config.hhmmss(match.get('end_video_sec'))} "
            f"({int(match.get('duration_sec', 0))} сек.)"
        )
        lines.append(
            f"- Drama Score: **{match.get('drama_score', 0):.2f}**; "
            f"Self-Tilt: **{match.get('self_tilt_index', 0):.2f}**; "
            f"жалоб на тех. проблемы: {match.get('tech_complaints_count', 0)}"
        )
        flags = []
        if match.get("comeback_flag"):
            flags.append("камбэк")
        if match.get("lead_blown_flag"):
            flags.append("упущенное преимущество")
        if flags:
            lines.append("- Флаги: " + ", ".join(flags))
        events = match.get("events") or []
        if events:
            lines.append("")
            lines.append("| Время | Счёт | Команда | Автор / ассист / привоз | Гол |")
            lines.append("|---|---|---|---|---|")
            for event in events:
                tags = []
                if event.get("is_clutch_goal"):
                    tags.append("клатч")
                if event.get("is_doublet_goal"):
                    tags.append("дуплет")
                excerpt = (event.get("voice_excerpt") or "").replace("\n", " ")
                if len(excerpt) > 140:
                    excerpt = excerpt[:137] + "…"
                lines.append(
                    f"| {_youtube_link(url, int(event.get('video_sec', 0)), event.get('video_timestamp_str', ''))} "
                    f"| {_md_escape(event.get('score_snapshot'))} "
                    f"| {'Войс' if event.get('scoring_team') == config.VOICE_TEAM else 'Соперник'} "
                    f"| {_md_escape(_event_identity(event, names))} "
                    f"| {_md_escape(', '.join(tags) or '—')} |"
                )
            lines.append("")
            for event in events:
                if event.get("voice_excerpt"):
                    lines.append(
                        f"> {config.hhmmss(event.get('video_sec'))}: "
                        + event["voice_excerpt"].replace("\n", " ")
                    )
                    lines.append("")
        else:
            lines.append("- Голов не зафиксировано.")
        if match.get("issues"):
            lines.append("- ⚠ " + "; ".join(str(x) for x in match["issues"]))
        if match.get("mvp_player_id"):
            lines.append(f"- MVP матча: **{names.get(match['mvp_player_id'], match['mvp_player_id'])}**")
        if match.get("blunder_player_id"):
            lines.append(f"- Главный привоз матча: **{names.get(match['blunder_player_id'], match['blunder_player_id'])}**")

    lines.append("")
    lines.append("## Итоги серии")
    lines.append("")
    fatigue = metrics.get("fatigue") or []
    if fatigue:
        lines.append("**Динамика по номеру матча**")
        lines.append("")
        lines.append("| Номер матча | Матчей | Победы | Ничьи | Поражения | Winrate |")
        lines.append("|---|---|---|---|---|---|")
        for row in fatigue:
            lines.append(
                f"| {row.get('match_num_in_minibattle')} | {row.get('matches', 0)} | "
                f"{row.get('wins', 0)} | {row.get('draws', 0)} | {row.get('losses', 0)} | "
                f"{row.get('winrate_pct', 0):.1f}% |"
            )
        lines.append("")
    lines.append(f"Средний Self-Tilt Index по серии: **{summary.get('avg_self_tilt_index', 0):.2f}**")
    if summary.get("series_note"):
        lines.append("")
        lines.append(summary["series_note"])
    lines.append("")
    lines.append("---")
    lines.append("*Отчёт сформирован локальным конвейером Goal 3 Analytics.*")
    return "\n".join(lines)
