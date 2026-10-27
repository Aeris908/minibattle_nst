"""Streamlit dashboard for the local Goal 3 minibattle analytics pipeline.

Run:
    streamlit run app.py
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import config
from core.downloader import DownloadError, parse_source
from database import Database

st.set_page_config(
    page_title="Goal 3 Minibattle Analytics",
    page_icon="⚽",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_resource(show_spinner=False)
def get_db() -> Database:
    return Database()


def youtube_url(url: str, seconds: int | float) -> str:
    if not url:
        return ""
    joiner = "&" if "?" in url else "?"
    return f"{url}{joiner}t={max(0, int(seconds))}s"


def fmt_score(mb: dict) -> str:
    return (
        f"{mb.get('series_wins_voice', 0)}–"
        f"{mb.get('series_draws', 0)}–"
        f"{mb.get('series_losses_voice', 0)}"
    )


def roster_frame(roster: list[dict], role: str) -> pd.DataFrame:
    rows = []
    for player in roster:
        if player.get("team_role") != role:
            continue
        rows.append(
            {
                "Игрок": player.get("canonical_name"),
                "Позиция": player.get("field_position"),
                "Контроллер": player.get("controller_num"),
            }
        )
    return pd.DataFrame(rows)


def status_label(value: str | None) -> str:
    return {
        "pending": "⏳ в очереди",
        "processing": "⚙️ в работе",
        "completed": "✅ готово",
        "failed": "❌ ошибка",
        "needs_review": "🔎 нужна проверка",
        "auto_verified": "✅ авто-проверка",
        "manual_verified": "✅ проверено вручную",
    }.get(value or "", value or "—")


def _render_series_matches(mb: dict, db: Database) -> None:
    matches = db.get_matches(mb["video_id"])
    if not matches:
        st.info("Матчи ещё не сохранены.")
        return
    for match in matches:
        num = match["match_num_in_minibattle"]
        title = (
            f"Матч #{num} · {match.get('team_voice_country', '?')} — "
            f"{match.get('team_opp_country', '?')} · "
            f"{match.get('score_voice', 0)}:{match.get('score_opp', 0)} "
            f"({match.get('match_outcome')})"
        )
        with st.expander(title):
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Drama", f"{match.get('drama_score', 0):.2f}")
            c2.metric("Self-Tilt", f"{match.get('self_tilt_index', 0):.2f}")
            c3.metric("Тех. жалобы", int(match.get("tech_complaints_count") or 0))
            c4.metric("Длительность", config.hhmmss(match.get("duration_sec")))
            st.caption(
                f"Сторона войса: {match.get('voice_side')} · "
                f"старт {config.hhmmss(match.get('start_video_sec'))} · "
                f"финал {config.hhmmss(match.get('end_video_sec'))} · "
                f"статус: {status_label(match.get('verification_status'))}"
            )
            events = db.get_match_events(match["match_id"])
            if events:
                rows = []
                for event in events:
                    url = youtube_url(mb.get("url", ""), event["video_sec"])
                    rows.append(
                        {
                            "Время": event["video_timestamp_str"],
                            "Матч-минута": event["game_minute_bucket"],
                            "Тайм": event["half"],
                            "Счёт": event["score_snapshot"],
                            "Команда": "Войс" if event["scoring_team"] == config.VOICE_TEAM else "Соперник",
                            "Автор": event.get("author_name") or "—",
                            "Ассист": event.get("assist_name") or "—",
                            "Привоз": event.get("blunder_name") or "—",
                            "Ссылка": url,
                        }
                    )
                st.dataframe(
                    pd.DataFrame(rows),
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Ссылка": st.column_config.LinkColumn("Ссылка", display_text="▶ YouTube")
                    },
                )
            else:
                st.caption("Голы не найдены.")


# ---------------------------------------------------------------------------
# Tab 1: overview + queue
# ---------------------------------------------------------------------------
def tab_overview(db: Database) -> None:
    st.header("Обзор минибатлов и очередь")
    stats = db.get_dashboard_stats()
    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
    c1.metric("Видео", stats["videos"])
    c2.metric("Минибатлов", stats["minibattles"])
    c3.metric("Готово", stats["completed"])
    c4.metric("Матчей", stats["matches"])
    c5.metric("Голов", stats["goals"])
    c6.metric("Игроков", stats["players"])
    c7.metric("На проверку", stats["review"])

    st.subheader("Добавить видео или плейлист")
    with st.form("add_source_form"):
        url_value = st.text_input(
            "URL YouTube-плейлиста или отдельного видео",
            placeholder="https://www.youtube.com/playlist?list=...",
        )
        side = st.selectbox(
            "Исходная сторона команды войса в первом матче",
            options=config.VALID_SIDES,
            index=1,
            help="Далее стороны меняются автоматически каждые 2 матча.",
        )
        submitted = st.form_submit_button("Добавить в очередь", type="primary")
    if submitted:
        if not url_value.strip():
            st.error("Введите URL.")
        else:
            queued_ok = False
            with st.spinner("yt-dlp читает плейлист…"):
                try:
                    result = parse_source(url_value.strip(), db, initial_voice_side=side)
                    st.success(
                        f"Добавлено видео: {len(result['items'])} "
                        f"(новых: {sum(1 for item in result['items'] if item['created'])})."
                    )
                    queued_ok = True
                except DownloadError as exc:
                    st.error(str(exc))
                except Exception as exc:
                    st.exception(exc)
            if queued_ok:
                st.rerun()

    videos = db.list_videos()
    if videos:
        queue_df = pd.DataFrame(
            [
                {
                    "video_id": video["video_id"],
                    "Название": video["title"],
                    "Статус": status_label(video["status"]),
                    "Длительность": config.hhmmss(video.get("duration_sec")),
                    "Найдено минибатлов": video.get("minibattles_found", 0),
                    "Сторона": video["initial_voice_side"],
                    "Обработано": video["processed_at"] or "—",
                    "Ошибка": (video.get("error_msg") or "")[:200],
                    "Ссылка": video["url"],
                }
                for video in videos
            ]
        )
        with st.expander(f"Очередь видео ({len(queue_df)})", expanded=True):
            st.dataframe(
                queue_df,
                width="stretch",
                hide_index=True,
                column_config={
                    "Ссылка": st.column_config.LinkColumn("Ссылка", display_text="YouTube"),
                },
            )

    st.subheader("Карточка минибатла")
    minibattles = db.list_minibattles()
    selectable = [
        mb for mb in minibattles if mb["status"] in ("completed", "processing", "failed")
    ]
    if not selectable:
        st.info("Пока нет обработанных минибатлов. Добавьте URL и запустите `python worker.py`.")
        return
    labels = {
        f"#{mb['source_video_id'] or mb['video_id']} · серия {mb.get('series_index', 1)} · "
        f"{mb.get('title') or ''} · {status_label(mb['status'])}": mb
        for mb in selectable
    }
    selected_label = st.selectbox("Выберите минибатл", list(labels.keys()))
    mb = labels[selected_label]
    roster = db.get_roster(mb["video_id"])
    st.markdown(f"### {mb.get('title') or mb['video_id']}")
    if mb.get("source_video_id"):
        st.caption(
            f"Источник: `{mb['source_video_id']}` · серия "
            f"{mb.get('series_index', 1)} · фрагмент "
            f"{config.hhmmss(mb.get('series_start_sec'))}–{config.hhmmss(mb.get('series_end_sec'))}"
        )
    if mb.get("url"):
        st.markdown(f"[Открыть источник]({mb['url']})")
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Серия W–D–L", fmt_score(mb))
    m2.metric("Голы войса", mb.get("total_goals_voice", 0))
    m3.metric("Голы соперника", mb.get("total_goals_opp", 0))
    m4.metric("Средний Self-Tilt", f"{(mb.get('avg_self_tilt_index') or 0):.2f}")
    m5.metric("Матчей", mb.get("total_matches", 0))
    mvps = {p["player_id"]: p["canonical_name"] for p in roster}
    c_left, c_right = st.columns(2)
    with c_left:
        st.markdown("#### Команда войса")
        df_voice = roster_frame(roster, config.VOICE_TEAM)
        if df_voice.empty:
            st.caption("Нет данных")
        else:
            st.dataframe(df_voice, hide_index=True, width="stretch")
        if mb.get("minibattle_mvp_player_id"):
            st.success(
                "MVP серии: "
                + mvps.get(mb["minibattle_mvp_player_id"], str(mb["minibattle_mvp_player_id"]))
            )
    with c_right:
        st.markdown("#### Соперник")
        df_opp = roster_frame(roster, config.OPPONENT_TEAM)
        if df_opp.empty:
            st.caption("Нет данных")
        else:
            st.dataframe(df_opp, hide_index=True, width="stretch")
        if mb.get("minibattle_blunder_player_id"):
            st.error(
                "Антигерой серии: "
                + mvps.get(
                    mb["minibattle_blunder_player_id"],
                    str(mb["minibattle_blunder_player_id"]),
                )
            )
    if mb.get("verification_status"):
        st.caption("Проверка: " + status_label(mb["verification_status"]))
    if mb.get("minibattle_report_md"):
        with st.expander("Полный сводный отчёт минибатла", expanded=False):
            st.markdown(mb["minibattle_report_md"])
    with st.expander("Все матчи внутри минибатла", expanded=False):
        _render_series_matches(mb, db)


# ---------------------------------------------------------------------------
# Tab 2: review mode
# ---------------------------------------------------------------------------
def tab_review(db: Database) -> None:
    st.header("Верификация матчей")
    all_mb = db.list_minibattles()
    review = db.get_review_queue()
    if not all_mb:
        st.info("База пуста.")
        return
    ordered = review + [mb for mb in all_mb if mb["video_id"] not in {r["video_id"] for r in review}]
    only_review = st.checkbox("Показывать только needs_review", value=False)
    visible = review if only_review else ordered
    if not visible:
        st.success("Очередь проверки пуста.")
        return
    labels = {
        f"{'🔎 ' if mb['verification_status'] == 'needs_review' else ''}"
        f"{mb.get('source_video_id') or mb['video_id']} · серия {mb.get('series_index', 1)} · "
        f"{mb.get('title') or ''} · {status_label(mb['status'])}": mb
        for mb in visible
    }
    selected = st.selectbox("Минибатл для проверки", list(labels.keys()))
    mb = labels[selected]
    roster = db.get_roster(mb["video_id"])
    matches = db.get_matches(mb["video_id"])
    if not matches:
        st.info("У этого минибатла ещё нет сохранённых матчей.")
        return
    name_by_id = {int(p["player_id"]): p["canonical_name"] for p in roster}
    options_ids = [None] + sorted(name_by_id)
    labels_by_id = {pid: ("—" if pid is None else name_by_id[pid]) for pid in options_ids}

    h1, h2, h3 = st.columns(3)
    h1.metric("Матчей", len(matches))
    h2.metric("Серия", fmt_score(mb))
    h3.metric("Статус", status_label(mb.get("verification_status")))
    st.caption("Сначала показаны минибатлы со статусом needs_review. Правки авторов применяются сразу к БД.")

    for match in matches:
        st.divider()
        st.subheader(
            f"Матч #{match['match_num_in_minibattle']} — "
            f"{match.get('team_voice_country', '?')} vs {match.get('team_opp_country', '?')} "
            f"· {match.get('score_voice', 0)}:{match.get('score_opp', 0)} "
            f"({match.get('match_outcome')})"
        )
        top_left, top_right = st.columns([1.15, 1])
        with top_left:
            screenshot = match.get("final_score_crop_path")
            if screenshot and Path(screenshot).exists():
                st.image(screenshot, caption="Финальное табло", width=620)
            else:
                st.warning("Скриншот финального табло недоступен.")
        with top_right:
            st.write(
                f"**Сторона войса:** `{match.get('voice_side')}`  \n"
                f"**Старт:** {config.hhmmss(match.get('start_video_sec'))}  \n"
                f"**Конец:** {config.hhmmss(match.get('end_video_sec'))}  \n"
                f"**Drama:** {match.get('drama_score', 0):.2f}  \n"
                f"**Self-Tilt:** {match.get('self_tilt_index', 0):.2f}  \n"
                f"**Тех. жалобы:** {match.get('tech_complaints_count', 0)}"
            )
            if mb.get("url"):
                st.markdown(
                    f"[▶ Открыть начало матча на YouTube]({youtube_url(mb['url'], match.get('start_video_sec', 0))})"
                )

        events = db.get_match_events(match["match_id"])
        if events:
            st.markdown("#### Таймлайн голов")
            for event in events:
                author = event.get("author_name") or "автор не распознан"
                assist = event.get("assist_name") or "—"
                blunder = event.get("blunder_name") or "—"
                link = (
                    f"[{event['video_timestamp_str']}]({youtube_url(mb['url'], event['video_sec'])})"
                    if mb.get("url")
                    else event["video_timestamp_str"]
                )
                st.markdown(
                    f"- {link} · **{event['score_snapshot']}** · "
                    f"{'Войс' if event['scoring_team'] == config.VOICE_TEAM else 'Соперник'} · "
                    f"⚽ {author} · 🅰 {assist} · ❌ {blunder}"
                )
            st.markdown("#### Быстрая правка атрибуции")
            for event in events:
                with st.form(f"event_edit_{event['event_id']}"):
                    cols = st.columns([1, 2, 2, 2, 1])
                    cols[0].markdown(f"**{event['video_timestamp_str']}**  \n{event['score_snapshot']}")
                    author = cols[1].selectbox(
                        "Автор",
                        options=options_ids,
                        index=options_ids.index(event.get("author_player_id"))
                        if event.get("author_player_id") in options_ids
                        else 0,
                        format_func=lambda pid: labels_by_id[pid],
                        key=f"author_{event['event_id']}",
                    )
                    assist = cols[2].selectbox(
                        "Ассист",
                        options=options_ids,
                        index=options_ids.index(event.get("assist_player_id"))
                        if event.get("assist_player_id") in options_ids
                        else 0,
                        format_func=lambda pid: labels_by_id[pid],
                        key=f"assist_{event['event_id']}",
                    )
                    blunder = cols[3].selectbox(
                        "Привоз",
                        options=options_ids,
                        index=options_ids.index(event.get("blunder_player_id"))
                        if event.get("blunder_player_id") in options_ids
                        else 0,
                        format_func=lambda pid: labels_by_id[pid],
                        key=f"blunder_{event['event_id']}",
                    )
                    if cols[4].form_submit_button("💾"):
                        db.update_event_attribution(event["event_id"], author, assist, blunder)
                        st.success("Сохранено.")
                        st.rerun()
        else:
            st.info("В матче не найдено событий-голов.")
    st.divider()
    if st.button("✅ Подтвердить весь минибатл → manual_verified", type="primary"):
        db.mark_minibattle_verified(mb["video_id"])
        st.success("Минибатл и все его матчи подтверждены.")
        st.rerun()


# ---------------------------------------------------------------------------
# Tab 3: player profile
# ---------------------------------------------------------------------------
def tab_player(db: Database) -> None:
    st.header("Персональное досье игрока")
    players = db.get_all_players()
    if not players:
        st.info("Игроков пока нет.")
        return
    by_name = {p["canonical_name"]: p for p in players}
    selected_name = st.selectbox("Игрок", list(by_name.keys()))
    player = by_name[selected_name]
    career = db.get_player_career(int(player["player_id"]))
    if not career:
        st.warning("Нет карьерной статистики.")
        return
    st.markdown(f"### {player['canonical_name']}")
    if player.get("aliases"):
        st.caption("Алиасы: " + ", ".join(player["aliases"]))
    r1 = st.columns(4)
    r1[0].metric("Минибатлов", career["minibattles_played"])
    r1[1].metric("Матчей", career["matches_played"])
    r1[2].metric("Винрейт", f"{career['winrate_pct']:.1f}%")
    r1[3].metric("Голы", career["goals"])
    r2 = st.columns(4)
    r2[0].metric("Ассисты", career["assists"])
    r2[1].metric("Гол + Пас", career["goal_plus_pass"])
    r2[2].metric("MVP", career["mvp_count"])
    r2[3].metric("Привозы", career["blunders_count"])
    r3 = st.columns(4)
    r3[0].metric("Blunder Impact Rate", f"{career['blunder_impact_rate']:.1f}%")
    r3[1].metric("Сухие матчи", career["clean_sheets"])
    r3[2].metric("Клатч-голы", career["clutch_goals"])
    r3[3].metric("Любимая позиция", career.get("favorite_position") or "—")
    st.caption(f"Любимая сборная: {career.get('favorite_country') or '—'}")

    c_left, c_right = st.columns([1, 1])
    with c_left:
        st.subheader("Эффективность по позициям")
        positions = pd.DataFrame(db.get_player_position_stats(int(player["player_id"])))
        if positions.empty:
            st.info("Нет данных.")
        else:
            st.dataframe(positions, hide_index=True, width="stretch")
    with c_right:
        st.subheader("Тайминг голов")
        timing = pd.DataFrame(db.get_player_goal_timing(int(player["player_id"])))
        if timing.empty:
            st.info("Голов пока нет.")
        else:
            fig = px.bar(
                timing,
                x="game_minute_bucket",
                y="goals",
                labels={"game_minute_bucket": "Минута", "goals": "Голы"},
                color="goals",
                color_continuous_scale="Blues",
            )
            fig.update_layout(height=320, margin=dict(l=10, r=10, t=20, b=10), showlegend=False)
            st.plotly_chart(fig, width="stretch")

    st.subheader("Журнал минибатлов и матчей")
    history = pd.DataFrame(db.get_player_history(int(player["player_id"])))
    if history.empty:
        st.info("Нет сыгранных матчей.")
        return
    history["Ссылка"] = [
        youtube_url(row.get("url") or "", row.get("start_video_sec") or 0)
        for _, row in history.iterrows()
    ]
    display = history[
        [
            "title", "match_num_in_minibattle", "team_role", "country_played",
            "field_position", "controller_num", "score_voice", "score_opp",
            "match_outcome", "match_result", "goals", "assists", "blunders",
            "is_clean_sheet", "Ссылка",
        ]
    ].rename(
        columns={
            "title": "Минибатл",
            "match_num_in_minibattle": "Матч #",
            "team_role": "Команда",
            "country_played": "Сборная",
            "field_position": "Позиция",
            "controller_num": "Контроллер",
            "score_voice": "Войс",
            "score_opp": "Соперник",
            "match_outcome": "Итог",
            "match_result": "Результат игрока",
            "goals": "Голы",
            "assists": "Ассисты",
            "blunders": "Привозы",
            "is_clean_sheet": "Сухой",
            "Ссылка": "Ссылка",
        }
    )
    st.dataframe(
        display,
        width="stretch",
        hide_index=True,
        column_config={"Ссылка": st.column_config.LinkColumn("Ссылка", display_text="▶")},
    )


# ---------------------------------------------------------------------------
# Tab 4: advanced analytics
# ---------------------------------------------------------------------------
def tab_analytics(db: Database) -> None:
    st.header("Продвинутая аналитика и мета")
    tier_df = pd.DataFrame(db.get_country_tier_list())
    synergy_df = pd.DataFrame(db.get_synergy_duos(100))
    h2h_df = pd.DataFrame(db.get_h2h_matrix())
    timing_df = pd.DataFrame(db.get_global_goal_timings())
    drama_df = pd.DataFrame(db.get_top_drama_matches(20))
    tilt_df = pd.DataFrame(db.get_self_tilt_matches(20))
    corr_df = pd.DataFrame(db.get_tech_complaint_correlation())
    fatigue_df = pd.DataFrame(db.get_fatigue_series())

    st.subheader("Тир-лист сборных")
    if tier_df.empty:
        st.info("Нет данных по сборным.")
    else:
        left, right = st.columns([1, 1.2])
        with left:
            st.dataframe(tier_df, hide_index=True, width="stretch")
        with right:
            fig = px.bar(
                tier_df.sort_values("winrate_pct", ascending=False).head(20),
                x="country_name",
                y="winrate_pct",
                color="matches_played",
                labels={"country_name": "Сборная", "winrate_pct": "Winrate %", "matches_played": "Матчи"},
                title="Винрейт сборных",
            )
            fig.update_layout(height=380, margin=dict(l=10, r=10, t=40, b=10))
            st.plotly_chart(fig, width="stretch")

    st.subheader("Химия и связки")
    if synergy_df.empty:
        st.info("Пока нет распознанных пар «ассист → автор».")
    else:
        st.dataframe(
            synergy_df.rename(
                columns={
                    "assist_player_name": "Ассист",
                    "author_player_name": "Автор",
                    "goals_together": "Голы",
                    "matches_together": "Матчи вместе",
                    "joint_wins": "Победы",
                    "joint_winrate_pct": "Совместный винрейт %",
                }
            )[
                ["Ассист", "Автор", "Голы", "Матчи вместе", "Победы", "Совместный винрейт %"]
            ],
            hide_index=True,
            width="stretch",
        )

    st.subheader("Матрица противостояний H2H")
    if h2h_df.empty:
        st.info("Недостаточно матчей для H2H.")
    else:
        min_matches = st.slider("Минимум личных матчей", 1, 10, 1)
        filtered = h2h_df[h2h_df["matches"] >= min_matches]
        activity = pd.concat([filtered["player_a_name"], filtered["player_b_name"]]).value_counts()
        top_players = list(activity.head(24).index)
        filtered = filtered[
            filtered["player_a_name"].isin(top_players)
            & filtered["player_b_name"].isin(top_players)
        ]
        if filtered.empty:
            st.info("Нет пар под выбранный фильтр.")
        else:
            pivot = filtered.pivot_table(
                index="player_a_name",
                columns="player_b_name",
                values="winrate_a_pct",
                aggfunc="mean",
                fill_value=0,
            )
            fig = go.Figure(
                data=go.Heatmap(
                    z=pivot.values,
                    x=list(pivot.columns),
                    y=list(pivot.index),
                    colorscale="RdYlGn",
                    zmin=0,
                    zmax=100,
                    colorbar=dict(title="Winrate A, %"),
                )
            )
            fig.update_layout(
                height=max(420, 26 * len(pivot.index)),
                margin=dict(l=10, r=10, t=20, b=10),
                xaxis_title="Игрок B",
                yaxis_title="Игрок A",
            )
            st.plotly_chart(fig, width="stretch")
            st.caption("Строка A — игрок, столбец B — его соперник. Значение: winrate A в личных встречах.")

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Голы по минутам")
        if timing_df.empty:
            st.info("Нет голов.")
        else:
            fig = px.bar(
                timing_df,
                x="game_minute_bucket",
                y="goals",
                labels={"game_minute_bucket": "Минута", "goals": "Голы"},
                color="goals",
                color_continuous_scale="Turbo",
            )
            fig.update_layout(height=330, margin=dict(l=10, r=10, t=20, b=10), showlegend=False)
            st.plotly_chart(fig, width="stretch")
    with col2:
        st.subheader("Жалобы на лаги и исходы")
        if corr_df.empty:
            st.info("Нет данных.")
        else:
            fig = px.bar(
                corr_df,
                x="complaint_bucket",
                y="matches",
                color="match_outcome",
                barmode="group",
                labels={
                    "complaint_bucket": "Тех. жалобы",
                    "matches": "Матчи",
                    "match_outcome": "Исход",
                },
            )
            fig.update_layout(height=330, margin=dict(l=10, r=10, t=20, b=10))
            st.plotly_chart(fig, width="stretch")
            corr_pivot = corr_df.pivot_table(
                index="complaint_bucket",
                columns="match_outcome",
                values="matches",
                aggfunc="sum",
                fill_value=0,
            )
            for outcome in ("WIN", "DRAW", "LOSS"):
                if outcome not in corr_pivot.columns:
                    corr_pivot[outcome] = 0
            corr_pivot["Всего"] = corr_pivot[["WIN", "DRAW", "LOSS"]].sum(axis=1)
            corr_pivot["Винрейт %"] = (
                100.0 * corr_pivot["WIN"] / corr_pivot["Всего"].replace(0, 1)
            ).round(1)
            corr_pivot = corr_pivot.rename(
                columns={"WIN": "Победы", "DRAW": "Ничьи", "LOSS": "Поражения"}
            )
            st.dataframe(corr_pivot, width="stretch")

    st.subheader("Топ валидольных матчей")
    if drama_df.empty:
        st.info("Нет матчей.")
    else:
        st.dataframe(
            drama_df[
                [
                    "title", "match_num_in_minibattle", "team_voice_country",
                    "team_opp_country", "score_voice", "score_opp", "match_outcome",
                    "drama_score", "comeback_flag", "lead_blown_flag",
                ]
            ].rename(
                columns={
                    "title": "Минибатл",
                    "match_num_in_minibattle": "Матч #",
                    "team_voice_country": "Войс",
                    "team_opp_country": "Соперник",
                    "score_voice": "Голы войса",
                    "score_opp": "Голы соперника",
                    "match_outcome": "Исход",
                    "drama_score": "Drama",
                    "comeback_flag": "Камбэк",
                    "lead_blown_flag": "Упустили",
                }
            ),
            hide_index=True,
            width="stretch",
        )

    st.subheader("Рейтинг Self-Tilt")
    if tilt_df.empty:
        st.info("Нет матчей.")
    else:
        st.dataframe(
            tilt_df[
                [
                    "title", "match_num_in_minibattle", "team_voice_country",
                    "team_opp_country", "score_voice", "score_opp", "match_outcome",
                    "self_tilt_index", "tech_complaints_count",
                ]
            ].rename(
                columns={
                    "title": "Минибатл",
                    "match_num_in_minibattle": "Матч #",
                    "team_voice_country": "Войс",
                    "team_opp_country": "Соперник",
                    "score_voice": "Голы войса",
                    "score_opp": "Голы соперника",
                    "match_outcome": "Исход",
                    "self_tilt_index": "Self-Tilt",
                    "tech_complaints_count": "Жалобы",
                }
            ),
            hide_index=True,
            width="stretch",
        )

    st.subheader("Усталость внутри минибатла")
    if fatigue_df.empty:
        st.info("Нет данных.")
    else:
        fig = px.line(
            fatigue_df,
            x="match_num_in_minibattle",
            y="winrate_pct",
            markers=True,
            labels={"match_num_in_minibattle": "Номер матча", "winrate_pct": "Winrate %"},
            title="Винрейт от 1-го к 8-му матчу серии",
        )
        fig.update_layout(height=340, margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig, width="stretch")
        st.dataframe(fatigue_df, hide_index=True, width="stretch")


# ---------------------------------------------------------------------------
# Tab 5: players, aliases, merge
# ---------------------------------------------------------------------------
def tab_players(db: Database) -> None:
    st.header("Управление базой игроков")
    players = db.get_all_players()
    if not players:
        st.info("Игроков пока нет.")
        return
    df = pd.DataFrame(
        [
            {
                "player_id": p["player_id"],
                "canonical_name": p["canonical_name"],
                "aliases": ", ".join(p.get("aliases") or []),
            }
            for p in players
        ]
    )
    st.subheader("Игроки, имена и алиасы")
    edited = st.data_editor(
        df,
        width="stretch",
        hide_index=True,
        disabled=["player_id"],
        key="players_editor",
        column_config={
            "player_id": st.column_config.NumberColumn("ID", width="small"),
            "canonical_name": st.column_config.TextColumn("Каноническое имя", width="medium"),
            "aliases": st.column_config.TextColumn(
                "Алиасы (через запятую)", width="large"
            ),
        },
    )
    if st.button("💾 Сохранить изменения игроков", type="primary"):
        errors = []
        for _, row in edited.iterrows():
            pid = int(row["player_id"])
            original = next((p for p in players if int(p["player_id"]) == pid), None)
            if not original:
                continue
            try:
                if row["canonical_name"].strip() != original["canonical_name"]:
                    db.rename_player(pid, row["canonical_name"])
                aliases = [
                    value.strip()
                    for value in str(row["aliases"]).replace(";", ",").split(",")
                    if value.strip()
                ]
                db.set_aliases(pid, aliases)
            except Exception as exc:
                errors.append(f"{pid}: {exc}")
        if errors:
            st.error("Не все изменения сохранены: " + "; ".join(errors))
        else:
            st.success("Изменения сохранены.")
            st.rerun()

    st.divider()
    st.subheader("Добавить игрока")
    with st.form("add_player_form"):
        name = st.text_input("Имя")
        alias_text = st.text_input("Алиасы через запятую")
        if st.form_submit_button("Добавить"):
            if not name.strip():
                st.error("Имя обязательно.")
            else:
                try:
                    pid = db.add_player(
                        name,
                        [a.strip() for a in alias_text.split(",") if a.strip()],
                    )
                    st.success(f"Игрок добавлен (ID {pid}).")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

    st.divider()
    st.subheader("Merge Players — объединение дублей")
    st.warning(
        "Исходный игрок будет удалён. Все ростера, матчи, голы, ассисты, привозы, "
        "линейки и MVP переедут на целевого игрока."
    )
    names = {f"{p['player_id']} · {p['canonical_name']}": p for p in players}
    c1, c2 = st.columns(2)
    source_label = c1.selectbox("Источник (дубль, будет удалён)", list(names))
    target_label = c2.selectbox("Цель (оставить)", list(names))
    confirm = st.checkbox("Я понимаю, что действие необратимо")
    if st.button("Объединить игроков"):
        source = names[source_label]
        target = names[target_label]
        if int(source["player_id"]) == int(target["player_id"]):
            st.error("Источник и цель совпадают.")
        elif not confirm:
            st.error("Подтвердите действие.")
        else:
            try:
                result = db.merge_players(int(source["player_id"]), int(target["player_id"]))
                st.success(
                    f"{result['moved_name']} объединён с {result['into']}."
                )
                st.rerun()
            except (ValueError, sqlite3.Error) as exc:
                st.error(f"Не удалось объединить: {exc}")

    st.divider()
    st.subheader("Диагностика")
    st.json(db.get_dashboard_stats())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    config.ensure_directories()
    db = get_db()
    st.title("⚽ Goal 3 (NES) — аналитика минибатлов")
    st.caption(
        "Полностью локальный конвейер: OpenCV · easyocr · faster-whisper · SQLite · Streamlit. "
        "До 12 игроков (6 на сторону), до 9 матчей в минибатле."
    )
    with st.sidebar:
        st.header("Состояние")
        stats = db.get_dashboard_stats()
        st.write(f"**Видео:** {stats['videos']} (готово {stats['completed_videos']})")
        st.write(f"**Минибатлов:** {stats['minibattles']} (готово {stats['completed']})")
        st.write(f"**Матчей:** {stats['matches']}")
        st.write(f"**Голов:** {stats['goals']}")
        st.write(f"**На проверку:** {stats['review']}")
        st.divider()
        if st.button(
            f"♻️ Сбросить processing старше {config.STALE_PROCESSING_HOURS:.0f} ч"
        ):
            count = db.reset_stale_processing()
            st.success(f"Сброшено: {count}")
        st.caption("Воркер запускается отдельно: `python worker.py --loop`")
        st.divider()
        st.caption(f"БД: `{config.DB_PATH}`")

    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        [
            "📺 Минибатлы и очередь",
            "🔎 Верификация",
            "👤 Игрок",
            "📊 Аналитика и мета",
            "🧩 Игроки и алиасы",
        ]
    )
    with tab1:
        tab_overview(db)
    with tab2:
        tab_review(db)
    with tab3:
        tab_player(db)
    with tab4:
        tab_analytics(db)
    with tab5:
        tab_players(db)


if __name__ == "__main__":
    main()
