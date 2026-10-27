# Goal 3 Minibattle Analytics

Локальный конвейер для автоматической оцифровки, разбора и визуализации стримов по **Goal 3 / Kunio-kun no Nekketsu Soccer League (NES)**.

Одно исходное видео скачивается один раз, проходит **один OpenCV-проход**, автоматически делится на один или несколько минибатлов, после чего для каждой серии отдельно извлекаются ростер, расстановка, голос, голы и статистика. Результаты сохраняются в SQLite, Markdown и Streamlit-дашборд.

> **Новое в структуре:** до 12 игроков (6 на сторону), до 9 матчей в минибатле, поддержка нескольких минибатлов в одном видео и строгий белый список из 14 сборных.

## Возможности

- **Скачивание:** yt-dlp получает видео 360p/480p и отдельную аудиодорожку; исходники удаляются в `finally`.
- **OpenCV:** multi-scale поиск якоря табло (`np.linspace(0.5, 1.5, 25)`), Template Matching цифр, анти-дребезг счёта, стабильность гола 3 секунды, confidence > 0.80.
- **Матчи и минибатлы:** сегментация по игровому полю/счёту, split длинного видео на несколько минибатлов по длине ролика и паузам, лимит 9 матчей, финальные скриншоты табло.
- **Ростер:** easyocr (`ru + en`) читает правую плашку, lineup-экран и перекличку в войсе; до 6 игроков на сторону, контроллеры 1–6, позиции GK/DF/MF/FW.
- **Голос:** faster-whisper локально, VAD, hotwords только из активного ростера + сборные + сленг.
- **Разбор:** детерминированные авторы голов, ассисты, привозы, тайминги, цитаты, `needs_review`.
- **Метрики:** Drama Score, Self-Tilt, клатч-голы, дуплеты, камбеки, упущенное преимущество, тех-жалобы, усталость по ходу серии.
- **SQLite:** полная схема, авто-миграции, views карьеры/H2H/связок/сборных, merge игроков.
- **Streamlit:** очередь, карточки минибатлов, Review Mode, профиль игрока, мета-аналитика, управление алиасами.
- **Локальность:** без платных API, без YouTube `.vtt`, без `pwsh.exe` в коде.

## Ограничения продукта

| Параметр | Значение |
|---|---|
| Игроков на сторону | до 6 |
| Всего игроков в минибатле | до 12 |
| Матчей в минибатле | до 9 |
| Минибатлов в одном видео | автоматически 1–3 (чаще 2) |
| Стартовая сторона войса | `1P_LEFT` или `2P_RIGHT` |
| Смена стороны | каждые 2 матча внутри минибатла |

### Разрешённые сборные

Только эти 14 команд:

`Япония 1`, `Япония 2`, `Италия`, `Германия`, `Аргентина`, `Бразилия`, `Камерун`, `Мексика`, `Монголия`, `Сенегал`, `Гвинея`, `Англия`, `Тайланд`, `Корея`.

`Япония` без номера неоднозначна и не принимается автоматически — матч уходит в проверку.

## Архитектура

```text
YouTube URL
  └─ core/downloader.py
       ├─ parse_source() -> DB.videos (очередь)
       └─ download_video() -> temp .mp4 + .m4a

worker.py
  └─ process_video()
       ├─ cv_tracker.analyze_video()          # 1 полный проход
       ├─ cv_tracker.split_matches_into_minibattles()
       ├─ для каждой серии:
       │    ├─ audio_slicer.process_minibattle_audio()
       │    ├─ roster_extractor.extract_roster()
       │    ├─ event_parser.parse_minibattle()
       │    ├─ metrics_engine.apply_minibattle_metrics()
       │    └─ event_parser.build_minibattle_report()
       ├─ database: videos + minibattles + matches + lineups + events
       └─ finally: удаление медиа и temp
```

Подробная карта модулей и инвариантов — в [docs/AI_HANDOFF.md](docs/AI_HANDOFF.md).
Действия оператора — в [docs/HUMAN_GUIDE.md](docs/HUMAN_GUIDE.md).

## Установка

Требования: Windows, Python **3.10+**, ffmpeg/ffprobe.

```cmd
git clone https://github.com/Aeris908/minibattle_nst.git
cd minibattle_nst
py -3.10 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

ffmpeg:

```cmd
set GOAL3_FFMPEG=C:\ffmpeg\bin\ffmpeg.exe
set GOAL3_FFPROBE=C:\ffmpeg\bin\ffprobe.exe
set GOAL3_FFMPEG_LOCATION=C:\ffmpeg\bin
```

Модели Whisper/easyocr при первом запуске скачиваются в локальный кэш. Для строго локального запуска:

```cmd
set GOAL3_WHISPER_MODEL=small
set GOAL3_WHISPER_DEVICE=cpu
set GOAL3_WHISPER_COMPUTE_TYPE=int8
set GOAL3_WHISPER_LOCAL_ONLY=1
```

## Калибровка

Один раз для конкретной раскладки стрима:

```cmd
python tools\calibrate_ui.py --video path\to\sample.mp4 --time 45
```

Выбираются playfield, scoreboard anchor, обе цифры счёта, roster overlay и lineup screen. Затем доберите шаблоны цифр 0–9 с кадров, где они видны:

```cmd
python tools\calibrate_ui.py --capture-digit 3 --video path\to\video.mp4 --time 610
```

Если верхняя команда на плашке — соперник:

```cmd
set GOAL3_TOP_TEAM_ROLE=OPPONENT_TEAM
```

Подробно: [docs/HUMAN_GUIDE.md](docs/HUMAN_GUIDE.md).

## Запуск

### Очередь

```cmd
python worker.py --add-url "https://www.youtube.com/playlist?list=..."
```

или добавьте URL в дашборде.

### Обработка

```cmd
python worker.py                 :: одно исходное видео
python worker.py --limit 0       :: вся очередь
python worker.py --loop           :: ждать новые видео
```

Полезно:

```cmd
python worker.py --video-id YOUTUBE_ID --force
python worker.py --reset-stale
python worker.py --reset-stale --stale-hours 0
python worker.py --cleanup-temp
```

Одна итерация = одно **исходное видео**. Внутри него может быть один или два минибатла; каждый получит отдельную запись и отчёт.

### Дашборд

```cmd
streamlit run app.py
```

Вкладки:

1. **Минибатлы и очередь** — URL, таблица исходных видео, найденные серии, карточки, отчёты.
2. **Верификация** — `needs_review`, финальные табло, таймлайн голов со ссылками `?t=`, правка атрибуции, подтверждение.
3. **Игрок** — карьера, позиции, голы по минутам, журнал.
4. **Аналитика и мета** — сборные, связки, H2H-heatmap, Drama, Self-Tilt, жалобы, усталость.
5. **Игроки и алиасы** — редактирование и Merge Players.

## Как работает разбиение видео на минибатлы

`core/cv_tracker.split_matches_into_minibattles()` использует:

1. разрыв между матчами **≥ 10 минут** — граница серии;
2. блок **> 9 матчей** — режется по сильному сбалансированному разрыву;
3. **длинное видео ≥ 2 часов**, достаточно матчей и пауза **≥ 4 минут** на выбранном split — дополнительная проверка длины;
4. результат по возможности ≤ 3 серий, но матчи не теряются;
5. локальная нумерация матчей внутри каждой серии.

Пороги в `config.py`:

- `SERIES_BREAK_GAP_SEC = 600`
- `MULTI_MINIBATTLE_VIDEO_DURATION_SEC = 7200`
- `SERIES_DURATION_SPLIT_MIN_GAP_SEC = 240`
- `MIN_MATCHES_PER_SERIES = 3`
- `MAX_MATCHES_PER_MINIBATTLE = 9`
- `MAX_MINIBATTLES_PER_VIDEO = 3`

Если видео разбито неверно, измените пороги и перезапустите `--force`; `split_note` сохраняется в таблице `videos`.

## Данные и структура

```text
minibattle_nst/
├── config.py
├── database.py
├── requirements.txt
├── worker.py
├── app.py
├── core/
│   ├── downloader.py
│   ├── cv_tracker.py
│   ├── roster_extractor.py
│   ├── audio_slicer.py
│   ├── event_parser.py
│   └── metrics_engine.py
├── tools/
│   └── calibrate_ui.py
├── docs/
│   ├── HUMAN_GUIDE.md
│   └── AI_HANDOFF.md
├── data/
│   ├── tournament.db
│   ├── temp/
│   ├── screenshots/
│   ├── templates/
│   ├── calibration/ui.json
│   ├── exports/<logical_id>_minibattle.md
│   └── logs/pipeline.log
└── README.md
```

### Ключевые таблицы

- `videos` — очередь исходных видео;
- `minibattles` — логическая серия: `video_id = <youtube_id>__mb<N>`, `source_video_id`, `series_index`, границы;
- `players`, `minibattle_rosters`;
- `matches`, `match_lineups`, `match_events`.

Views: `v_player_career_stats`, `v_h2h_matrix`, `v_synergy_duos`, `v_country_tier_list`.

## Алгоритмы

- **Гол:** только стабильный `+1` в течение 3 секунд с уверенностью > 0.80. Прыжок больше +1 не превращается в фейковые события; матч помечается `needs_review`.
- **Смена стороны:** `((match_num - 1) // 2) % 2`.
- **Ростер:** OCR правой плашки — ground truth; lineup — позиции; первые ~3 минуты аудио — контроллеры.
- **Аудио:** opening каждой серии, pre-match `[-60; 0]`, голы `[-20; +35]`, post-game `[0; +90]`.
- **Атрибуция:** rapidfuzz + маркеры речи + тайминги + позиции; никаких LLM.
- **Верификация:** `needs_review` при расхождении суммы голов со счётом, отсутствии автора/привоза, неизвестной сборной, плохом аудио или неполном ростере.

## Ограничения

- Автосплит серий — эвристика, требует проверки на длинных видео.
- Позиции/контроллеры зависят от OCR lineup; предусмотрен fallback.
- Автор гола может отсутствовать в войсе — остаётся пустым до ручной проверки.
- Модель Whisper загружается на каждую серию; это можно оптимизировать.
- Все серии одного видео пока наследуют одну стартовую сторону войса; смена стороны между минибатлами требует отдельной ручной настройки/пересчёта.
- При правке автора/ассиста/привоза отчёт, MVP и сводка пересобираются автоматически. После Merge Players старые Markdown-файлы не переписываются — нажми «Пересобрать отчёт и MVP» в Review Mode при необходимости.

## Диагностика

Лог: `data/logs/pipeline.log`.

| Проблема | Решение |
|---|---|
| Матчи не найдены | калибровка anchor, `FIELD_GREEN_RATIO_MIN`, шаблоны цифр |
| Не читается счёт | шаблоны 0–9, `SCORE_CONFIDENCE`, кадр табло |
| Два минибатла слиплись | `SERIES_BREAK_GAP_SEC`, `MULTI_MINIBATTLE_VIDEO_DURATION_SEC`, `split_note` |
| Мусорный ростер | ROI overlay, качество видео, ручная чистка/merge |
| Нет атрибуции | review, алиасы, hotwords |
| Зависло processing | `python worker.py --reset-stale --stale-hours 0` |

## Приватность и безопасность

Проект полностью локальный: видео и аудио не отправляются на сторонние API. Не коммитьте токены, `.env`, `data/`, скачанные видео и модели. Для GitHub push используйте отдельные credentials и никогда не сохраняйте токен в remote URL или файлах.

## Лицензия

Проект создан для личного использования. Если планируется публичное распространение, добавьте выбранную лицензию и проверьте права на используемые видео/музыку.
