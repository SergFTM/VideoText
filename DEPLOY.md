# VideoText — deployment / production checklist

Тулза задумана как **single-user localhost-приложение** (личный YouTube-обработчик
+ live-news пайплайн). Если запускаешь "в продакшен" — это значит на твоей рабочей
машине так, чтобы не падало и не требовало внимания. Этот документ описывает
минимальный set-up для такого режима.

## 1. Системные зависимости

| Что | Зачем | Установка (Windows) |
|---|---|---|
| Python 3.11+ | бекенд | `winget install Python.Python.3.11` |
| Node.js | используется только Prisma CLI | `winget install OpenJS.NodeJS.LTS` |
| ffmpeg | разрезание live-стримов | `winget install Gyan.FFmpeg` |
| Ollama (опц.) | локальный LLM-провайдер для ассистента | `winget install Ollama.Ollama` |
| NVIDIA driver + CUDA 12 (опц.) | ускорение faster-whisper | стандартный driver + `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` |

## 2. Установка проекта

```cmd
cd C:\path\to\VideoText
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
prisma generate
prisma db push
```

## 3. .env (секреты)

```ini
SUPADATA_API_KEY=sd_...
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-proj-...     # опционально для assistant/enrichment/dedup
PEXELS_API_KEY=...             # опционально для hybrid-картинок новостей
WEBHOOK_TOKEN=...              # опционально, защита POST /briefs
OUTPUT_DIR=./output            # опционально, дефолт ./output
YTDLP_COOKIES_PATH=./cookies.txt   # опционально, путь к cookies для yt-dlp
```

Все ключи также можно ввести через UI: **Настройки → Интеграции** →
кнопка «Изменить» на карточке коннектора. После сохранения они дописываются
в `.env` и сразу применяются без рестарта.

## 4. Запуск

### Dev (с auto-reload):
```cmd
.venv\Scripts\python -m uvicorn server:app --reload --port 8000
```

### Production-style (без reload, фоном):
```cmd
.venv\Scripts\python -m uvicorn server:app --host 0.0.0.0 --port 8000 --workers 1 --log-level warning
```

> ⚠️ `--workers > 1` пока **не поддерживается**: Prisma и orchestrator держат
> локальное состояние (активные ffmpeg-процессы), которое не делится между
> процессами. Один воркер достаточно — нагрузка приходит от ffmpeg/whisper,
> а не от HTTP.

### Автозапуск при логине Windows
1. Открой `Win+R` → `shell:startup`.
2. Создай `videotext.bat`:
   ```bat
   @echo off
   cd /d C:\Users\user\Desktop\VideoText
   start /B .venv\Scripts\python -m uvicorn server:app --port 8000 --log-level warning
   ```
3. Готово — стартует в фоне после логина.

## 5. Production checklist

### Обязательно перед использованием
- [ ] `.env` создан и содержит хотя бы `ANTHROPIC_API_KEY`
- [ ] `prisma db push` выполнен (создаёт `prisma/videotext.db`)
- [ ] `ffmpeg -version` работает в той же консоли где запускается сервер
- [ ] Открыть http://localhost:8000 → перейти **Настройки → Интеграции**
  → нажать «проверить» на каждом нужном коннекторе. Все green = ok.

### Опционально, но рекомендую
- [ ] Включить **Ассистент → Кэш Q&A** — экономит токены OpenAI/Anthropic
- [ ] Настроить **retention policy** в Хранилище (по умолчанию аудио чанков
      хранятся 7 дней, остальное — навсегда; SSD заполнится через ~6 месяцев
      активного мониторинга 3 стримов)
- [ ] Включить **авто-очистку** там же если не хочешь думать о месте на диске
- [ ] Установить Ollama + `ollama pull nomic-embed-text` если хочешь полностью
      локальный dedup без OpenAI-расходов

### Безопасность
- Если открываешь порт 8000 наружу (`--host 0.0.0.0`) — **обязательно**
  выстави `WEBHOOK_TOKEN` и используй заголовок `X-Webhook-Token` на POST
  `/briefs`. Остальные endpoint'ы пока без auth — рассчитаны на trusted-only LAN.
- `cookies.txt` (если загружен) хранится в корне проекта в **plain text**,
  не push в git. `.gitignore` уже это покрывает.

## 6. Известные ограничения

- **Один пользователь.** Конкурентные сессии с разными `WEBHOOK_TOKEN` не поддерживаются.
- **Один процесс.** См. выше про `--workers`.
- **Нет HTTPS из коробки.** Если нужен — поставь reverse-proxy (Caddy / nginx).
- **Тяжёлые операции (ffmpeg + faster-whisper) блокируют** Python event loop
  только на десятки мс — не критично для UI, но если хочется вычистить —
  всё уже завёрнуто в `asyncio.to_thread`.

## 7. Проверка после deploy

```bash
# health
curl http://localhost:8000/health

# integrations status
curl http://localhost:8000/config/integrations | jq

# storage
curl http://localhost:8000/storage/stats | jq

# GPU (опц., если nvidia-smi есть)
curl http://localhost:8000/system/gpu | jq

# assistant smoke
curl -N -X POST http://localhost:8000/assistant/chat \
  -H "Content-Type: application/json" \
  -d '{"question":"какие коннекторы настроены?"}'
```

## 8. Бэкап

Один файл — `prisma/videotext.db` (SQLite). Снапшоты:

```bash
# Hot-копия (SQLite WAL safe — пока никто не пишет в эту наносекунду)
copy prisma\videotext.db backups\videotext-%date:~-4,4%%date:~-7,2%%date:~-10,2%.db

# Cold-копия (надёжнее — остановить сервер на 5 секунд)
sqlite3 prisma/videotext.db ".backup backups/videotext-snapshot.db"
```

Файлы (`./chunks/`, `./output/`, `./images/`) — отдельно, если важны для
архива. Иначе они регенерируемы.

## 9. Обновление

```cmd
git pull
.venv\Scripts\activate
pip install -r requirements.txt
prisma generate
prisma db push
# рестарт сервера
```

Schema-миграции backwards-compatible (никогда не дроплю колонки/таблицы),
данные не теряются.

## 10. Удаление

```cmd
# Полное обнуление
del prisma\videotext.db
rmdir /s /q chunks output images
# После рестарта prisma db push заново создаст пустую БД.
```
