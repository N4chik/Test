# 🤖 AI Reminder & Habit Tracker Bot (aiogram 3.x + OpenRouter + SOCKS5 proxy)

Telegram-бот — ИИ-напоминалка и трекер привычек с обходом блокировок РФ через прокси.

## Быстрый старт

```bash
cp .env.example .env      # заполните токены
docker compose up -d --build
docker compose logs -f    # проверить запуск
```

Данные SQLite хранятся в `./data/bot.db` (volume `/app/data`) и переживают перезапуски.

## Переменные окружения (.env)
| Переменная | Описание |
|---|---|
| TELEGRAM_BOT_TOKEN | Токен от @BotFather |
| ADMIN_ID | Ваш числовой ID (узнать: @userinfobot). Только он имеет доступ |
| OPENROUTER_API_KEY | Ключ с openrouter.ai/keys |
| OPENROUTER_MODEL | Модель (по умолчанию google/gemini-flash-1.5-8b) |
| PROXY_URL | socks5:// / socks5h:// / http:// прокси. Пусто = напрямую |
| TZ | Часовой пояс напоминаний (Europe/Moscow) |

## Команды бота
- `/start` — инлайн-меню (напоминания / привычки / промпт ИИ)
- `/remind 08:30 Выпить воды` — ежедневное напоминание (текст генерирует ИИ)
- `/show_prompt` — текущий системный промпт ИИ (хранится в SQLite)
- `/edit_prompt <текст>` — поменять «личность» ИИ на лету
- `/ai <вопрос>` — прямой вопрос нейросети
- `/habits` — трекер привычек с инлайн-отметками дней
- `/help` — справка

## Как это работает
APScheduler по cron-расписанию будит задачу → бот шлёт скрытый запрос в OpenRouter
(системный промпт берётся из таблицы `settings`) → креативный ответ доставляется в Telegram.

## Без Docker (локально)
```bash
pip install -r requirements.txt
python bot.py                 # запуск
python bot.py --selftest      # оффлайн-проверка БД/планировщика/прокси-сессии
```
