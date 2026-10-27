"""Autonomous worker: process one source video and all its minibattles.

A source video is downloaded once.  One OpenCV pass detects every match; the
worker then applies the length/gap heuristics to split the recording into one or
more logical minibattles (max 9 matches each), and runs the full audio/roster/
commentary pipeline for each series independently.

The downloaded .mp4/.m4a files are deleted in a ``finally`` block regardless of
success, failure, or Ctrl+C.  No shell / pwsh.exe is used; external tools are
called directly by Python.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from pathlib import Path

import config
from core import audio_slicer, cv_tracker, downloader, event_parser, metrics_engine, roster_extractor
from database import Database

log = logging.getLogger("goal3.worker")


class WorkerError(RuntimeError):
    pass


def _series_opening_start(first_match_start: float) -> float:
    return max(0.0, float(first_match_start) - config.ROSTER_OPENING_LOOKBACK_SEC)


def _series_scan_window(
    first_match_start: float,
    video_duration: float,
) -> tuple[float, float]:
    start = max(0.0, float(first_match_start) - config.ROSTER_SCAN_WINDOW_SEC)
    # A short pre-game intro must not limit OCR to 20 seconds; scan up to four
    # minutes from the window start, but keep the series-local anchor.
    end = max(
        float(first_match_start) + 30.0,
        start + float(config.ROSTER_SCAN_WINDOW_SEC),
    )
    if video_duration > 0:
        end = min(end, video_duration)
    return start, max(start + 1.0, end)


def _analyse_series(
    source_row: dict,
    source_id: str,
    group: dict,
    series_count: int,
    media: dict,
    calibration: dict,
    cv_result: dict,
    db: Database,
) -> dict:
    """Run audio, roster, commentary and metrics for one logical minibattle."""
    series_index = int(group["series_index"])
    logical_id = Database.make_minibattle_id(source_id, series_index)
    base_title = source_row.get("title") or source_id
    title = (
        f"{base_title} — Минибатл {series_index}/{series_count}"
        if series_count > 1
        else base_title
    )
    matches = group["matches"]
    screenshot_issues: list[str] = []
    for match in matches:
        source_shot = match.get("final_score_crop_path")
        if not source_shot or not Path(source_shot).exists():
            continue
        target_shot = config.SCREENSHOTS_DIR / (
            f"{logical_id}_m{int(match['match_num_in_minibattle'])}_final.jpg"
        )
        try:
            target_shot.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_shot, target_shot)
            match["final_score_crop_path"] = str(target_shot)
        except OSError as exc:
            screenshot_issues.append(
                f"Could not copy final scoreboard screenshot: {exc}"
            )
    first_match_start = float(matches[0].get("start_video_sec", 0))
    video_duration = float(cv_result.get("duration_sec") or 0)
    opening_start = _series_opening_start(first_match_start)
    scan_start, scan_end = _series_scan_window(first_match_start, video_duration)
    log.info(
        "Analyzing series %d/%d (%s): matches=%d, start=%s, end=%s, opening=%s",
        series_index,
        series_count,
        logical_id,
        len(matches),
        config.hhmmss(group.get("start_video_sec")),
        config.hhmmss(group.get("end_video_sec")),
        config.hhmmss(opening_start),
    )

    def on_opening(opening_results: dict[str, dict]) -> dict:
        opening = opening_results.get("opening") or {}
        segments = opening.get("segments") or []
        roster = roster_extractor.extract_roster(
            video_path=media["video_path"],
            video_id=logical_id,
            db=db,
            first_match_start=first_match_start,
            opening_transcript=segments,
            calibration=calibration,
            scan_start_sec=scan_start,
            scan_end_sec=scan_end,
        )
        log.info(
            "Series %d roster: %d players", series_index, len(roster.get("players") or [])
        )
        return roster

    transcripts, roster, slicer = audio_slicer.process_minibattle_audio(
        audio_path=media["audio_path"],
        video_id=logical_id,
        matches=matches,
        video_duration=video_duration,
        roster_callback=on_opening,
        opening_start_sec=opening_start,
    )
    try:
        if not roster or not roster.get("players"):
            raise WorkerError(f"Series {series_index}: roster extraction returned no players.")

        parsed = event_parser.parse_minibattle(
            matches=matches,
            roster=roster,
            transcripts=transcripts,
            initial_voice_side=(
                source_row.get("initial_voice_side") or config.SIDE_RIGHT
            ),
            video_url=source_row.get("url") or "",
        )
        enriched_matches = parsed["matches"]
        metrics = metrics_engine.apply_minibattle_metrics(enriched_matches, roster)
        summary = metrics["summary"]

        review_reasons: list[str] = list(screenshot_issues)
        review_reasons.extend(cv_result.get("warnings") or [])
        series_cv_ok = bool(matches) and all(
            not match.get("needs_review") for match in matches
        )
        if not series_cv_ok:
            review_reasons.append(
                f"Series {series_index}: CV integrity/goal sum issue"
            )
            for match in matches:
                for issue in match.get("issues") or []:
                    review_reasons.append(f"CV match #{match.get('match_num_in_minibattle')}: {issue}")
        if roster.get("needs_review"):
            review_reasons.extend(roster.get("warnings") or [])
        if parsed.get("issues"):
            review_reasons.extend(parsed["issues"])
        for key, transcript in transcripts.items():
            if transcript.get("error"):
                review_reasons.append(f"Audio {key}: {transcript['error']}")
        for match in enriched_matches:
            if match.get("verification_status") != config.VERIFICATION_AUTO:
                review_reasons.append(
                    f"Match #{match.get('match_num_in_minibattle')}: "
                    + "; ".join(
                        str(x) for x in (match.get("issues") or ["требует проверки"])
                    )
                )
        verification = (
            config.VERIFICATION_REVIEW if review_reasons else config.VERIFICATION_AUTO
        )

        report = event_parser.build_minibattle_report(
            video={
                "video_id": logical_id,
                "source_video_id": source_id,
                "title": title,
                "url": source_row.get("url"),
                "series_index": series_index,
                "series_count": series_count,
            },
            roster=roster,
            analysis=parsed,
            summary=summary,
            metrics=metrics,
        )
        config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        report_path = config.EXPORT_DIR / f"{logical_id}_minibattle.md"
        report_path.write_text(report, encoding="utf-8")
        return {
            "logical_id": logical_id,
            "series_index": series_index,
            "series_count": series_count,
            "title": title,
            "group": group,
            "roster": roster,
            "parsed": parsed,
            "matches": enriched_matches,
            "metrics": metrics,
            "summary": summary,
            "verification": verification,
            "review_reasons": review_reasons,
            "report": report,
            "report_path": report_path,
        }
    finally:
        slicer.cleanup()


def _persist_series(
    db: Database,
    source_row: dict,
    source_id: str,
    state: dict,
    video_duration: float,
) -> None:
    group = state["group"]
    logical_id = state["logical_id"]
    db.create_minibattle_series(
        source_video_id=source_id,
        series_index=int(state["series_index"]),
        source_url=source_row.get("url") or "",
        title=state["title"],
        initial_voice_side=source_row.get("initial_voice_side") or config.SIDE_RIGHT,
        series_start_sec=int(group.get("start_video_sec", 0)),
        series_end_sec=int(group.get("end_video_sec", 0)),
        video_duration_sec=video_duration,
        detection_reason=group.get("detection_reason"),
    )
    db.replace_roster(logical_id, state["roster"]["players"])
    for match in state["matches"]:
        match_verification = (
            match.get("verification_status") or config.VERIFICATION_REVIEW
        )
        match_id = db.insert_match(
            logical_id, {**match, "verification_status": match_verification}
        )
        match["match_id"] = match_id
        result_voice = match.get("match_outcome", "DRAW")
        for player in state["roster"]["players"]:
            role = player["team_role"]
            is_voice = role == config.VOICE_TEAM
            country = (
                match.get("team_voice_country")
                if is_voice
                else match.get("team_opp_country")
            )
            team_result = (
                result_voice if is_voice else config.opposite_outcome(result_voice)
            )
            conceded = (
                int(match.get("score_opp", 0))
                if is_voice
                else int(match.get("score_voice", 0))
            )
            db.insert_lineup(
                match_id,
                logical_id,
                {
                    "player_id": player["player_id"],
                    "team_role": role,
                    "country_played": country,
                    "controller_num": player.get("controller_num"),
                    "field_position": player.get("field_position") or "MF",
                    "match_result": team_result,
                    "is_clean_sheet": 1 if conceded == 0 else 0,
                },
            )
        for event in match.get("events") or []:
            db.insert_event(match_id, logical_id, event)
        db.update_match(
            match_id,
            drama_score=match.get("drama_score", 0.0),
            comeback_flag=match.get("comeback_flag", 0),
            lead_blown_flag=match.get("lead_blown_flag", 0),
            self_tilt_index=match.get("self_tilt_index", 0.0),
            tech_complaints_count=match.get("tech_complaints_count", 0),
            mvp_player_id=match.get("mvp_player_id"),
            blunder_player_id=match.get("blunder_player_id"),
            verification_status=match_verification,
        )
    db.finalize_minibattle(
        logical_id,
        state["summary"],
        state["report"],
        state["verification"],
    )


def _split_note(groups: list[dict]) -> str:
    parts = []
    for group in groups:
        parts.append(
            f"#{group['series_index']}: {group['match_count']} matches, "
            f"{group['detection_reason']}"
        )
    return f"{len(groups)} minibattle(s): " + "; ".join(parts)


def process_video(
    source_row: dict,
    db: Database,
    calibration_path: str | None = None,
) -> dict:
    """Download once, detect all minibattles in the video and persist them."""
    source_id = source_row["video_id"]
    title = source_row.get("title") or source_id
    log.info("=" * 78)
    log.info("START source video %s | %s", source_id, title)
    log.info("=" * 78)
    started = time.time()
    media: dict | None = None
    temp_dir = None
    try:
        calibration = cv_tracker.load_calibration(calibration_path)
        media = downloader.download_video(source_row["url"], source_id)
        temp_dir = media.get("temp_dir")

        cv_result = cv_tracker.analyze_video(
            media["video_path"],
            source_id,
            calibration_path=calibration_path,
        )
        matches = cv_result.get("matches") or []
        if not matches:
            raise WorkerError(
                "OpenCV did not detect any match. Check scoreboard calibration and "
                "FIELD_GREEN_RATIO_MIN / score templates."
            )
        video_duration = float(cv_result.get("duration_sec") or 0.0)
        db.update_video(source_id, duration_sec=video_duration)
        groups = cv_tracker.split_matches_into_minibattles(matches, video_duration)
        if not groups:
            raise WorkerError("Could not group detected matches into a minibattle.")
        split_note = _split_note(groups)
        log.info(
            "CV pass complete: %d matches, duration=%s, minibattles=%d, integrity=%s",
            len(matches),
            config.hhmmss(video_duration),
            len(groups),
            cv_result.get("integrity_ok"),
        )
        log.info("Split: %s", split_note)

        if not media.get("audio_path") or not Path(media["audio_path"]).exists():
            raise WorkerError("Audio track was not downloaded; cannot analyse voice.")

        # Analyse every series before touching the database.  A failed second
        # series therefore does not destroy previously verified results.
        states: list[dict] = []
        for group in groups:
            state = _analyse_series(
                source_row=source_row,
                source_id=source_id,
                group=group,
                series_count=len(groups),
                media=media,
                calibration=calibration,
                cv_result=cv_result,
                db=db,
            )
            states.append(state)

        # ---- replace all logical minibattles for this source atomically-ish
        db.clear_video_analysis(source_id)
        for state in states:
            _persist_series(db, source_row, source_id, state, video_duration)
        db.set_video_status(
            source_id,
            "completed",
            error_msg=None,
            minibattles_found=len(states),
            split_note=split_note,
        )
        elapsed = time.time() - started
        summary_payload = [
            {
                "video_id": state["logical_id"],
                "series_index": state["series_index"],
                "matches": state["summary"]["total_matches"],
                "series": [
                    state["summary"]["series_wins_voice"],
                    state["summary"]["series_draws"],
                    state["summary"]["series_losses_voice"],
                ],
                "goals": [
                    state["summary"]["total_goals_voice"],
                    state["summary"]["total_goals_opp"],
                ],
                "verification_status": state["verification"],
                "report_path": str(state["report_path"]),
            }
            for state in states
        ]
        log.info(
            "DONE source %s in %.1fs: %d minibattle(s), %d matches",
            source_id,
            elapsed,
            len(states),
            sum(s["summary"]["total_matches"] for s in states),
        )
        return {
            "video_id": source_id,
            "status": "completed",
            "minibattles_found": len(states),
            "split_note": split_note,
            "minibattles": summary_payload,
            "elapsed_sec": round(elapsed, 1),
        }
    except Exception as exc:
        log.exception("Source video %s failed", source_id)
        try:
            db.set_video_status(
                source_id, "failed", f"{type(exc).__name__}: {exc}"
            )
        except Exception:
            log.exception("Could not persist failure status for %s", source_id)
        raise
    except KeyboardInterrupt:
        try:
            db.set_video_status(source_id, "failed", "Interrupted by user")
        except Exception:
            log.exception("Could not persist interruption status for %s", source_id)
        raise
    finally:
        # Delete the downloaded source video/audio even if a series failed.
        if media:
            try:
                downloader.cleanup_media(
                    media.get("video_path"), media.get("audio_path")
                )
            except Exception:
                log.exception("Media cleanup failed for %s", source_id)
        if temp_dir:
            try:
                downloader.cleanup_temp_dir(temp_dir)
            except Exception:
                log.exception("Temp directory cleanup failed for %s", source_id)


def _select_job(db: Database, video_id: str | None, force: bool) -> dict | None:
    if video_id:
        row = db.get_video(video_id)
        if not row:
            raise WorkerError(f"Unknown source video_id: {video_id}")
        if row["status"] == "completed" and not force:
            log.info("Source video %s is already completed; use --force to reprocess", video_id)
            return None
        db.set_video_status(video_id, "processing")
        row["status"] = "processing"
        return row
    return db.claim_next_video()


def run(args: argparse.Namespace) -> int:
    config.configure_logging()
    db = Database()
    if args.cleanup_temp:
        removed = downloader.cleanup_stale_temp(args.temp_max_age_hours)
        log.info("Removed %d stale temp directories", removed)
    if args.reset_stale:
        count = db.reset_stale_processing(args.stale_hours)
        log.info("Reset %d stale source videos to pending", count)
    if args.add_url:
        for url in args.add_url:
            try:
                downloader.parse_source(url, db, initial_voice_side=args.side)
            except Exception:
                log.exception("Could not queue URL %s", url)
    if args.limit is None:
        # One source video per invocation by default.  --loop drains queue.
        args.limit = 0 if args.loop else 1
    processed = 0
    failed_count = 0
    idle_rounds = 0
    started = time.time()
    while True:
        if args.limit and processed >= args.limit:
            break
        try:
            job = _select_job(db, args.video_id, args.force)
        except WorkerError:
            log.exception("Job selection failed")
            return 2
        if job is None:
            if args.video_id:
                break
            idle_rounds += 1
            log.info("Queue is empty (idle round %d)", idle_rounds)
            if args.loop:
                time.sleep(max(5, args.poll_seconds))
                continue
            break
        processed += 1
        try:
            process_video(job, db, args.calibration)
        except KeyboardInterrupt:
            log.warning("Interrupted by user; media cleanup has run in finally.")
            return 130
        except Exception as exc:
            log.error("Source video %s failed: %s", job["video_id"], exc)
            failed_count += 1
            if args.stop_on_error:
                return 1
        if args.video_id:
            break
    log.info(
        "Worker finished: %d source video(s), %d failure(s), %.1fs",
        processed,
        failed_count,
        time.time() - started,
    )
    return 1 if failed_count else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video-id", help="Process a specific source video_id")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after N source videos (default: 1, or all with --loop; 0 = all)",
    )
    parser.add_argument("--loop", action="store_true", help="Keep polling the queue")
    parser.add_argument("--poll-seconds", type=int, default=60, help="Queue poll interval")
    parser.add_argument("--force", action="store_true", help="Reprocess a completed video_id")
    parser.add_argument("--calibration", help="Alternative calibration JSON path")
    parser.add_argument("--reset-stale", action="store_true", help="Move stale processing videos back to pending")
    parser.add_argument(
        "--stale-hours",
        type=float,
        default=config.STALE_PROCESSING_HOURS,
        help="Only reset processing videos older than this many hours (0 = all)",
    )
    parser.add_argument("--cleanup-temp", action="store_true", help="Remove stale temp directories first")
    parser.add_argument("--temp-max-age-hours", type=float, default=24.0)
    parser.add_argument("--add-url", action="append", help="Queue a playlist/video URL before processing")
    parser.add_argument("--side", choices=config.VALID_SIDES, default=config.SIDE_RIGHT,
                        help="Initial voice side for newly queued videos")
    parser.add_argument("--stop-on-error", action="store_true", help="Stop the loop after a failure")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        log.warning("Stopped by user")
        return 130


if __name__ == "__main__":
    sys.exit(main())
