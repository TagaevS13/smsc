# SMSC Reporting Interface

Веб-интерфейс для загрузки CDR с двух серверов SMSC в собственную БД и отчетности с фильтрами.

## Что умеет

- Импорт CDR из двух источников (`smsc1`, `smsc2`);
- Источники: локальные папки или SFTP;
- Фильтры: дата/время, номер, статус, сервер;
- История импортов;
- Экспорт текущей выборки в CSV.

## Быстрый старт

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Открой в браузере: `http://127.0.0.1:8080`

## Авторизация

- Доступ к интерфейсу только через login/password.
- Пользователи хранятся в БД `cdr_reporting.db` (таблица `users`).
- При первом запуске автоматически создается пользователь `admin/admin123`.
- Можно задать дефолт через переменные окружения:
  - `SMSC_ADMIN_USER`
  - `SMSC_ADMIN_PASSWORD`
- После входа доступны все страницы (`/`, `/import`, `/export.csv`).

### Управление пользователями

```bash
python manage_users.py list
python manage_users.py add operator1 --password "StrongPass123"
python manage_users.py set-password operator1 --password "NewStrongPass123"
python manage_users.py disable operator1
python manage_users.py enable operator1
```

## Конфиг источников

Файл: `config.yaml` (боевой SFTP вариант):

```yaml
sources:
  - name: smsc1
    mode: sftp
    host: "129.13.0.125"
    port: 22
    username: "parser"
    password: "131095Ss!"
    path: "/sftp/cdr/SMS"

  - name: smsc2
    mode: sftp
    host: "129.13.0.123"
    port: 22
    username: "parser"
    password: "131095Ss!"
    path: "/sftp/cdr/SMS"
```

## Использование

1. Нажми **Import CDR from configured servers**.
2. Задай фильтры:
  - Start/End datetime;
  - Number (MSISDN);
  - Status (success/failed/error);
  - Server (smsc1/smsc2).
3. Нажми **Apply filters**.
4. Для выгрузки нажми **Export CSV**.

## База данных

- Файл БД: `cdr_reporting.db`
- Таблицы:
  - `cdr_records` — CDR записи;
  - `import_jobs` — журнал импортов.

## Логи приложения

- `logs/app.log` — события веб-приложения (авторизация, запуск импорта, очистка данных).
- `logs/import.log` — детали процесса парсинга/импорта и ошибки по источникам.

## Важно

- Дедупликация выполняется по `(cdr_id, source_server)`.
- Некорректные строки пропускаются и учитываются как skipped.

