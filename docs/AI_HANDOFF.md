# AI Handoff: Goal 3 Minibattle Analytics

Ты принимаешь локальный Python-проект для оцифровки стримов Goal 3 (NES). Эта инструкция — карта кода, инварианты и правила безопасного продолжения. Сначала прочитай её, затем `README.md` и `docs/HUMAN_GUIDE.md`.

## 1. Главная миссия

Для каждого **исходного видео**:

1. скачать один раз видео + отдельную аудиодорожку;
2. один раз просканировать всё видео OpenCV;
3. определить все матчи, голы, таймкоды и финальные табло;
4. разбить видео на **один или несколько минибатлов** (максимум **9 матчей** в каждом), используя длину ролика и длинные паузы;
5. для каждого минибатла независимо извлечь ростер (до **6 игроков на сторону / 12 всего**), позиции, контроллеры, транскрипты;
6. детерминированно разобрать авторов/ассистов/привозы, посчитать метрики, записать SQLite и Markdown;
7. удалить исходные `.mp4/.m4a` и temp в `finally`.

Внешних платных API нет. Субтитры YouTube `.vtt` запрещены. `pwsh.exe` в коде не вызывается; ffmpeg/ffprobe — только `subprocess.run` с прямым executable, yt-dlp — Python API.

## 2. Жёсткие правила продукта

- Команды строго из белого списка (14):
  `Япония 1`, `Япония 2`, `Италия`, `Германия`, `Аргентина`, `Бразилия`, `Камерун`, `Мексика`, `Монголия`, `Сенегал`, `Гвинея`, `Англия`, `Тайланд`, `Корея`.
  Не добавляй другие сборные без прямого указания владельца.
- `Япония 1` и `Япония 2` — разные команды. Голое «Япония» неоднозначно и должно уходить в `needs_review`.
- До 6 игроков на сторону, до 12 в минибатле. `CONTROLLERS_PER_TEAM = 6`.
- До 9 матчей в одном минибатле.
- В одном исходном видео может быть несколько минибатлов; они хранятся как **разные строки `minibattles`** с синтетическим `video_id = f"{source_video_id}__mb{series_index}"`.
- Очередь скачивания — таблица `videos`. Не путать:
  - `videos.video_id` = реальный YouTube ID;
  - `minibattles.video_id` = логический ID серии;
  - `minibattles.source_video_id` = YouTube ID;
  - `matches/events/lineups.video_id` = логический ID серии.
- Сторона войса: `side_shifted = ((match_num - 1) // 2) % 2`; матчи 1–2 — исходная сторона, 3–4 — противоположная и т.д.
- Не выдумывать атрибуцию. Нет автора/привоза в войсе — поле пустое, матч в `needs_review`.
- Все изменения — локальные и идемпотентные по возможности. Не коммить токены, `data/`, модели и медиа.

## 3. Карта файлов

| Файл | Ответственность |
|---|---|
| `config.py` | пути, лимиты, белый список сборных, пороги OpenCV, окна аудио, маркеры речи, настройки Whisper |
| `database.py` | схема SQLite, миграции, views, очередь `videos`, серии `minibattles`, CRUD, merge игроков |
| `tools/calibrate_ui.py` | OpenCV-калибровка anchor/цифр/overlay/lineup |
| `core/downloader.py` | yt-dlp parsing в `videos`, скачивание видео+аудио, очистка |
| `core/cv_tracker.py` | сквозной CV-проход, score templates, сегментация матчей, split на минибатлы, скриншоты |
| `core/roster_extractor.py` | easyocr правой плашки и расстановки, до 12 игроков, контроллеры 1–6 |
| `core/audio_slicer.py` | нарезка ffmpeg, faster-whisper, individual series opening window |
| `core/event_parser.py` | авторы/ассисты/привозы, страны, Markdown-отчёт |
| `core/metrics_engine.py` | Drama, Self-Tilt, doublet, clutch, MVP, fatigue |
| `worker.py` | одна исходная видео-итерация, multi-series orchestration, persist, cleanup |
| `app.py` | 5 вкладок Streamlit |
| `docs/HUMAN_GUIDE.md` | действия оператора |
| `README.md` | публичное описание проекта |

## 4. Поток данных

```text
URL
 └─ downloader.parse_source()
      └─ DB.videos(status=pending)

worker.process_video(video_row)
 ├─ downloader.download_video()          # один раз
 ├─ cv_tracker.analyze_video()           # один CV-проход
 │    └─ matches[]
 ├─ cv_tracker.split_matches_into_minibattles(matches, duration)
 │    └─ groups[{series_index, matches[], start, end, reason}]
 ├─ для каждой group:
 │    ├─ audio_slicer.process_minibattle_audio(..., opening_start_sec)
 │    │    ├─ аудио-окна цели/прематч/постгейм
 │    │    ├─ opening transcription
 │    │    └─ roster_callback -> roster_extractor.extract_roster()
 │    ├─ event_parser.parse_minibattle()
 │    ├─ metrics_engine.apply_minibattle_metrics()
 │    ├─ verification gate
 │    └─ event_parser.build_minibattle_report()
 ├─ DB.clear_video_analysis(source_id)
 ├─ для каждой series:
 │    ├─ DB.create_minibattle_series() -> logical video_id
 │    ├─ DB.replace_roster()
 │    ├─ DB.insert_match()/insert_lineup()/insert_event()
 │    └─ DB.finalize_minibattle()
 ├─ DB.set_video_status(completed, minibattles_found=N)
 └─ finally: cleanup media + temp
```

Ключевая гарантия: **сначала успешно анализируются все серии**, и только потом очищаются старые результаты. Если анализ любой серии падает, старые подтверждённые данные этого источника сохраняются, а видео получает `failed`.

## 5. Схема БД и миграции

Основные таблицы:

- `players(player_id, canonical_name UNIQUE COLLATE NOCASE, aliases_json)`
- `videos(video_id PK, url, title, duration_sec, initial_voice_side, status, error_msg, processed_at, minibattles_found, split_note)`
- `minibattles(video_id PK, source_video_id FK videos, series_index, series_start_sec, series_end_sec, source_video_duration_sec, series_detection_reason, url, title, ..., status, verification_status, ...)`
- `minibattle_rosters`, `matches`, `match_lineups`, `match_events` — `video_id` ссылается на логический `minibattles.video_id`.

View:

- `v_player_career_stats`
- `v_h2h_matrix`
- `v_synergy_duos`
- `v_country_tier_list`

Миграция:

- `SCHEMA_VERSION = 2`.
- `init_schema()` выполняет `SCHEMA_SQL`, затем аддитивные `ALTER TABLE`, затем создаёт индексы и views.
- `videos` — новая очередь. Старые строки `minibattles` без `source_video_id` автоматически получают `videos`-строку и `source_video_id = video_id`, `series_index = 1`.
- Уникальный индекс `(source_video_id, series_index)` не даёт создать дубль серии.
- Индексы, зависящие от новых колонок, создаются **после** ALTER, иначе старая БД сломается на старте.

Если меняешь схему:
1. добавь колонки в `MIGRATION_COLUMNS`;
2. не добавляй в `SCHEMA_SQL` индексы на новые колонки — создавай их после миграции;
3. подними `SCHEMA_VERSION`;
4. протестируй fresh DB и legacy DB;
5. не ломай `v_*` views.

## 6. Разбиение на минибатлы

`cv_tracker.split_matches_into_minibattles()`:

1. **Длинные паузы** между матчами `>= SERIES_BREAK_GAP_SEC` (10 минут) — граница.
2. **Лимит 9 матчей** — блок режется по сильному сбалансированному разрыву.
3. **Длинное видео** `>= MULTI_MINIBATTLE_VIDEO_DURATION_SEC` (2 часа) и минимум `2 * MIN_MATCHES_PER_SERIES` матчей — дополнительный split по лучшему сбалансированному разрыву, но только если этот разрыв `>= SERIES_DURATION_SPLIT_MIN_GAP_SEC` (4 минуты).
4. Результат ограничен `MAX_MINIBATTLES_PER_VIDEO` (3) по возможности, но **матчи не выбрасываются**: после merge повторно применяется лимит 9.
5. Каждой серии присваивается локальная нумерация матчей 1..N и сохраняется `source_match_num`.

Важно: это эвристика. На реальных данных проверяй `split_note` и карточки серий. Если меняешь пороги — обнови `docs/HUMAN_GUIDE.md` и README.

## 7. Границы и контракты модулей

### CV

`analyze_video(video_path, video_id, calibration_path=None) -> dict`:

```python
{
  "video_id": str,
  "duration_sec": float,
  "matches": [
    {
      "match_num_in_minibattle": int,  # глобальный номер до split
      "start_video_sec": int,
      "end_video_sec": int,
      "duration_sec": int,
      "score_left": int,
      "score_right": int,
      "goals": [{"video_sec": int, "scoring_side": "LEFT|RIGHT", ...}],
      "final_score_crop_path": str | None,
      "needs_review": bool,
      "issues": [str],
    }
  ],
  "integrity_ok": bool,
  "warnings": [str],
}
```

Гол принимается только как **+1**, стабильный **3 секунды**, confidence **> 0.80**. Прыжок больше +1 не расширяется в фейковые события — матч уходит в review.

`split_matches_into_minibattles(matches, duration) -> list[group]`
где `group = {series_index, series_count, start_video_sec, end_video_sec, match_count, matches, detection_reason}`.

### Roster

`extract_roster(video_path, video_id, db, first_match_start, opening_transcript, calibration, scan_start_sec, scan_end_sec)`.

`video_id` — **логический ID серии**, чтобы скриншоты не перезаписывались. Возвращает `players` с полями `player_id, canonical_name, team_role, controller_num, field_position, aliases`, плюс `voice_team_label`, `opponent_team_label`, screenshots, warnings.

Ограничения:
- OCR `easyocr(['ru','en'])`;
- fuzzy-резолв `rapidfuzz.process.extractOne(score_cutoff=80)`;
- кап 6 на команду;
- `controller_num` всегда 1..6 или `None`;
- позиции fallback: `GK, DF, DF, MF, MF, FW`.

### Audio

`process_minibattle_audio(audio_path, video_id, matches, video_duration, roster_callback, opening_start_sec) -> (transcripts, roster, slicer)`.

- `opening_start_sec` для второй серии = `first_match_start - 180`, а не 0.
- Все клипы 16k mono WAV через ffmpeg.
- `faster-whisper` с `vad_filter=True`, `min_silence_duration_ms=500`.
- Один model load на одну серию; при двух сериях модель загружается дважды. Это допустимо, но можно оптимизировать shared-model, не меняя контракт transcripts.
- Key формат: `opening`, `m{n}_pre`, `m{n}_g{i}`, `m{n}_post`; `n` — **локальный** номер матча серии.

### Event parser

`parse_minibattle(matches, roster, transcripts, initial_voice_side, video_url)`.
- `match_num_in_minibattle` локальный.
- Возвращает enriched matches с `events`, `score_voice/opp`, `match_outcome`, `issues`, `verification_status`.
- Автор/ассист только `VOICE_TEAM`; привоз тоже VOICE_TEAM. Причины review не терять.

### Metrics

`apply_minibattle_metrics(matches, roster) -> {"summary":..., "fatigue":..., "matches":...}`.
- `summary` используется `finalize_minibattle`; не переименовывай ключи.
- `_player_points` / `_player_stats` — внутренние, в БД не пишутся.

### Worker / DB

`process_video(source_row, db, calibration_path) -> dict`.
- `source_row` из `videos`.
- Один download, N series.
- Cleanup в `finally`.

## 8. UI

`app.py`:
1. «Минибатлы и очередь» — добавление URL, таблица `videos`, карточки логических минибатлов.
2. «Верификация» — `needs_review` первыми, скриншоты, таймлайн, правка авторов, `manual_verified`.
3. «Игрок» — карьера, позиции, голы по минутам, история.
4. «Аналитика» — сборные, связки, H2H, Drama, Self-Tilt, жалобы, усталость.
5. «Игроки и алиасы» — редактор, merge.

Не меняй `@st.cache_resource` на кэш соединения с pickling: sqlite-connection не сериализуется.

## 9. Проверки перед коммитом

В репозитории нет обязательного pytest; используй минимум:

```cmd
python -m py_compile config.py database.py worker.py app.py core\*.py tools\*.py
```

Проверка SQL без запуска тяжёлых зависимостей — извлечь `SCHEMA_SQL`/`VIEWS_SQL` AST и выполнить в `sqlite3 :memory:`.

Smoke-тест БД (после установки `rapidfuzz`):

```python
from database import Database
db = Database("data/smoke.db")
db.queue_video("src", "https://youtu.be/src", "Test", duration_sec=7300)
db.claim_next_video()
lid = db.create_minibattle_series("src", 1, "https://youtu.be/src", "T", "2P_RIGHT", 0, 1000, 7300, "unit")
db.finalize_minibattle(lid, {...}, "# report", "auto_verified")
```

Тест split:
- 5 матчей/1ч => 1 серия;
- 10 матчей/1.5ч => 2 серии, каждая <=9;
- 4+4 с паузой 30м => 2 серии;
- 12 матчей => все матчи сохранены, каждая серия <=9.

## 10. Известные слабые места

- Split серий — эвристика; длинное одиночное видео может быть разрезано. Пороги в `config.py`.
- OCR позиций и контроллеров не гарантирует точность; при плохом кадре — fallback.
- Автор гола не всегда называется; это осознанно ведёт в review.
- Локализация сборных по речи может ошибаться на сарказме/шутках; review.
- Модель Whisper загружается на каждую серию.
- Все серии одного видео сейчас наследуют `initial_voice_side` исходного видео. Если вторая серия начинается с другой стороны, нужна отдельная per-series настройка/пересчёт — это осознанно не автоматизировано.
- Миграция legacy-БД поддерживает аддитивные изменения, но не пересчитывает старые метрики/отчёты.
- После ручной правки атрибуции `Database.update_event_attribution()` вызывает `rebuild_minibattle_artifacts()`: пересчитываются match/series MVP, сводка и Markdown. Кнопка в Review Mode делает то же вручную. После Merge Players отчёты не переписываются автоматически, пока не нажать пересборку.

## 11. Правила безопасных изменений

- Не добавляй shell/PowerShell вызовы.
- Не скачивай `.vtt`.
- Не отправляй данные во внешние API.
- Не храни токены/пароли в репозитории.
- Не удаляй исходные видео до завершения обработки; cleanup только в `finally`.
- Не меняй `minibattles.video_id` на реальный YouTube ID — сломаются FK и несколько серий на видео.
- Не удаляй `source_video_id` и `series_index`.
- Не увеличивай `MAX_MATCHES_PER_MINIBATTLE` без обновления split и UI.
- Не забывай обновлять `docs/HUMAN_GUIDE.md` при изменении порогов/калибровки.
