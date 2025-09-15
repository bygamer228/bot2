# DutyBot 2.0

Ежедневный пост в Telegram-группу: дата, дежурные (2 человека, Пн–Сб) и расписание уроков.
Все данные лежат в файлах (`students.txt`, `schedule.json`). Команды доступны только админам.

## Установка (Ubuntu)
```bash
cd /opt
unzip dutybot2.zip -d dutybot2
cd dutybot2
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip -r requirements.txt

cp .env.example .env
# отредактируй .env (BOT_TOKEN/GROUP_ID/ADMINS)

# тестовый запуск
python bot.py
```

## Автозапуск (systemd)
```bash
sudo cp dutybot.service /etc/systemd/system/dutybot.service
sudo systemctl daemon-reload
sudo systemctl enable --now dutybot
sudo systemctl status dutybot --no-pager
```

## Файлы
- `students.txt` — список учеников (Фамилия Имя [Отчество]) по одному в строке.
- `schedule.json` — расписание: по дням недели и точечные даты.
- `exceptions.json` — подмены на конкретные даты (создаётся автоматически).
- `debtors.json` — должники (создаётся автоматически).
- `start_date.txt` — старт ротации (создаётся автоматически).
- `sim_date.txt` — симулируемая дата для /next,/prev (создаётся автоматически).

## Команды
- `/test` — отправить и закрепить пост за сегодня
- `/today` / `/tomorrow` — дежурные + расписание
- `/schedule [YYYY-MM-DD]` — только расписание
- `/schedule_set <день> предметы через |` — обновить расписание дня (пн/вт/… или mon/tue/…)
- `/who [YYYY-MM-DD]` — кто дежурит
- `/send YYYY-MM-DD` — отправить пост на конкретную дату
- `/next` / `/prev` — листать симулируемую дату (для тестов)
- `/skip N` — сместить очередь на N рабочих дней (Пн–Сб)
- `/reset_all` — полный сброс базы
- `/debtors` — список должников
- `/come [YYYY-MM-DD]` — назначить должника (выбор/рандом) на дату
- `/reload_students` — перечитать `students.txt` без перезапуска
- `/seed ФИО1;ФИО2 [дата]` — сидирование базы (требует смежной пары по списку)
- `/seed_only ФИО1;ФИО2 [дата]` — разовая фиксация пары на дату
- `/say текст` — отправить сообщение в группу от бота

## Примечания
- Воскресенье пропускается.
- Имена парсятся по «Фамилия Имя» (отчество можно писать, бот игнорирует).
- При замене через `/come` снятый человек переносится на ближайший свободный рабочий день.
