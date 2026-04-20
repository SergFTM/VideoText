# VideoText — руководство для AI-ассистента

Это документ, на котором ассистент строит ответы. Каждый `## раздел` индексируется
отдельно; при ответе подтягиваются только релевантные разделы (cherry-pick по TF-IDF).

---

## обзор проекта

VideoText — локальное приложение для обработки YouTube-контента на Windows. Два пайплайна:

1. **Single video**: URL → транскрипт (Supadata или yt-dlp) → бриф через Claude → сохранение в SQLite.
2. **Live stream**: ffmpeg берёт HLS-поток → нарезает по 5-минутным чанкам → faster-whisper транскрибирует (CUDA/CPU) → Claude извлекает новостные элементы → модерация → одобренные в ленту → опциональное обогащение (expanded text + Pexels/DALL-E картинка).

Стек: FastAPI + Prisma (SQLite) + Alpine.js/Tailwind фронт. 4 вкладки: Видео, Стримы, Новости, Настройки.

---

## supadata — настройка

**Зачем:** YouTube-транскрипты без возни с yt-dlp / cookies / PO-Token. Путь 2 проекта.

**Где взять ключ:** [supadata.ai](https://supadata.ai/) → зарегистрируйся → Dashboard → API key (формат `sd_...`).

**Куда вставить:** Настройки → коннектор Supadata → поле `SUPADATA_API_KEY` → Save. Или в `.env` вручную строкой `SUPADATA_API_KEY=sd_...`

**Тест:** `curl http://127.0.0.1:8000/config/test?provider=supadata` или кнопка «проверить» на карточке.

**Проблемы:** 503 часты — их сервис нестабилен. Подожди минуту и повтори.

---

## anthropic — настройка

**Зачем:** Генерация брифов (single video) и извлечение новостей (live streams). Основной LLM проекта.

**Где взять ключ:** [console.anthropic.com](https://console.anthropic.com/settings/keys) → Settings → API keys → Create key. Формат `sk-ant-api03-...`.

**Куда вставить:** Настройки → Anthropic → Save.

**Модель по умолчанию:** `claude-sonnet-4-6` (baланс цена/качество). Меняется в настройке `default_brief_model`.

**Биллинг:** Anthropic даёт $5 стартовых credits. На реальные стримы (5 часов эфира = ≈60 чанков = ≈60 вызовов) уходит $1-3.

---

## openai — настройка

**Зачем:**
1. Эмбеддинги (`text-embedding-3-small`) для semantic dedup новостей.
2. Обогащение expanded text (`gpt-4o-mini`).
3. Генерация иллюстраций (`dall-e-3`, `gpt-image-1`, `dall-e-2`).
4. Сам AI-ассистент (по умолчанию `gpt-4o`).

**Где взять ключ:** [platform.openai.com/api-keys](https://platform.openai.com/api-keys). Формат `sk-proj-...` или `sk-...`.

**Биллинг:** Обязательный. Бесплатного тира для GPT-4o нет. Пополни минимум $5.

**Куда вставить:** Настройки → OpenAI → Save.

---

## pexels — настройка

**Зачем:** Стоковые фото для иллюстраций новостей. Используется в `hybrid`-режиме обогащения (стоковое фото + AI-обработка = новостной эффект без галлюцинаций).

**Где взять ключ:**
1. Зайди на [pexels.com/api](https://www.pexels.com/api/)
2. Нажми Get Started → зарегистрируйся (email + пароль, 1 минута)
3. Описание проекта: «personal media enrichment» (любое, они не придираются)
4. Скопируй API key с Dashboard

**Формат ключа:** строка длиной ~56 символов, без префикса. Пример: `563492ad6f91700001000001abcdef...`

**Лимиты:** 200 запросов/час, 20000/месяц. Для типичного новостного флоу этого хватает с запасом.

**Куда вставить:** Настройки → Pexels → Save.

---

## ollama — настройка

**Зачем:** Локальный LLM-runner. Можно использовать для:
- embeddings через `nomic-embed-text` (semantic dedup без OpenAI-расходов)
- полноценных моделей (llama 3.1, mistral) если хочется офлайн-ассистента

**Установка:**
1. `winget install Ollama.Ollama` (или [ollama.com/download](https://ollama.com/download))
2. Перезапусти терминал → `ollama serve` (или autostart через Services)
3. Скачай модель: `ollama pull nomic-embed-text` (280MB, для эмбеддингов)
4. Для чата-ассистента офлайн: `ollama pull llama3.1:8b` (4.7GB)

**Endpoint:** `http://localhost:11434` (меняется в настройках если нужно).

**Как проверить:** `curl http://localhost:11434/api/tags` → список скачанных моделей.

**Куда указать в UI:** Настройки → Ollama → кнопка «проверить». Модель выбирается в настройках dedup (для эмбеддингов) и assistant (для чата).

---

## fastembed — настройка

**Зачем:** Полностью локальные эмбеддинги для semantic dedup. Альтернатива OpenAI и Ollama. Не требует сервиса — работает как Python-библиотека.

**Установка:** `pip install fastembed` (уже в requirements.txt).

**Модель по умолчанию:** `paraphrase-multilingual-MiniLM-L12-v2` — хорошо работает с русским и английским.

**Куда указать:** Настройки → dedup_embedding_provider = `fastembed`.

**Важно:** Первый запуск качает ONNX-модель (~110MB) в `~/.cache/fastembed/`. Это нормально.

---

## dedup — семантический дедуп новостей

**Проблема:** Стрим BBC News за 2 часа выдаст 30+ новостей, но 10 из них — повторы одной и той же темы.

**Решение:** При извлечении каждой новости считается эмбеддинг, сравнивается с эмбеддингами новостей из того же стрима за последние N часов. Если косинус > threshold — ссылаемся на оригинал, скрываем дубль из ленты «Новости».

**Настройки:**
- `dedup_embedding_provider`: `fastembed` / `ollama` / `openai` / `none`
- `dedup_window_hours`: 4 (по умолчанию) — окно сравнения
- `dedup_similarity_threshold`: 0.82 — порог косинуса

**Как подобрать threshold:** начни с 0.82. Если много ложных дублей — 0.87. Если пропускает очевидные — 0.78.

---

## enrichment — обогащение новостей

**Три режима иллюстраций (`enrich_image_source`):**

1. **generate** — чистая генерация через DALL-E 3 / gpt-image-1. $0.04-0.08 за картинку. Иногда галлюцинирует.
2. **stock** — только Pexels. $0. Иногда фото не идеально по теме.
3. **hybrid** (по умолчанию) — Pexels-фото + gpt-image-1 дорабатывает. $0.03 за картинку. Лучший новостной эффект.

**Concept-based dedup картинок:** если концепт новости похож на уже сгенерированную («crude oil barrel» vs «oil prices»), переиспользуем существующий PNG. Поле `reuseCount` считает сколько раз.

**Настройки:**
- `enrich_image_enabled`: toggle
- `enrich_image_source`: `hybrid` / `stock` / `generate`
- `enrich_image_model`: `gpt-image-1` / `dall-e-3` / `dall-e-2`
- `enrich_image_dedup_threshold`: 0.85

---

## pipeline стримов

**Запуск:** Настройки стрима → URL YouTube live + channel name + interval_min → Start.

**Фазы (каждые N минут):**
1. `capture.py`: ffmpeg читает HLS, пишет chunk-{i}.m4a
2. `transcribe.py`: faster-whisper (CUDA fp16 → CPU int8 fallback) → transcriptText
3. `news_extractor.py`: Claude извлекает JSON-массив новостей → NewsItem[status=draft]
4. `dedup.py`: semantic dedup → дубли маркируются duplicateOfId
5. (опц) `enrich.py`: expanded text + иллюстрация

**Модерация:** пользователь во вкладке «Стримы» одобряет/отклоняет драфты → попадают в «Новости» (status=approved).

**Retention:** `cleanup.py` (по умолчанию выключен) удаляет аудио-файлы старше N дней, обнуляет `audioPath`. Новости остаются.

---

## API endpoints

- `POST /transcribe` — single video URL → brief
- `POST /streams` — создать стрим
- `POST /streams/{id}/stop` — остановить
- `GET /streams/{id}/news` — лента новостей стрима
- `POST /news-items/{id}/approve` — одобрить
- `POST /news-items/{id}/enrich` — обогатить
- `GET /config/settings` — все настройки
- `POST /config/settings` — сохранить
- `GET /config/integrations` — статус 6 коннекторов
- `POST /config/test?provider=X` — ping коннектор
- `POST /assistant/chat` — этот ассистент (SSE)
- `POST /assistant/refresh-kb` — пересобрать knowledge base

---

## типовые сценарии

### «не могу настроить X»
Ассистент смотрит: (а) какая карточка открыта на скрине, (б) какой ключ отсутствует в settings, (в) соответствующий раздел выше, (г) может автоматически проверить коннектор через `test_integration` tool.

### «почему упал стрим»
Ассистент читает `Chunk.error` / `Run.error` за последние 10 минут через tool `get_recent_errors`, мэтчит по `errors.yaml`, выдаёт готовый fix.

### «какие новости за сегодня»
Ассистент через `list_news_items(status=approved, since=today)` берёт снапшот и суммаризирует.

### «что стоит обогатить»
Ассистент через `list_news_items(status=approved, enriched=false)` находит кандидатов, предлагает `enrich_news_item(id)` пакетом.
