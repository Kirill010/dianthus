# Диагностика: приложение не видит базу данных

## Что показывают выводы команд

1.  **Файл `.env` на сервере:**
    ```bash
    root@cv7998847:/var/www/dianthus# grep -E '^(ENV|SECRET_KEY|DATABASE_URL|REDIS_URL|POSTGRES_|INTEGRATION_PASSWORD)' .env | sed -E 's/(PASSWORD|KEY|URL)=.*(.{4})$/\1=...\2/'
    ENV=prod
    SECRET_KEY=...8c57
    POSTGRES_USER=dianthus_user
    POSTGRES_PASSWORD=...hjkm
    POSTGRES_DB=dianthus
    DATABASE_URL=...thus
    REDIS_URL=...79/0
    INTEGRATION_PASSWORD=...Reko
    ```
    В этом masked-выводе видны только последние символы значений. Для `DATABASE_URL` это `...thus`. Это может означать, что строка заканчивается на `/dianthus`, но при этом **не содержит** хоста `db` (например, осталось старое значение `postgresql://dianthus_user:...@localhost:5432/dianthus`). Именно с таким URL приложение и пыталось подключиться в предыдущем логе ошибки (`connection to server at "localhost"`).

2.  **Запущенный контейнер БД:**
    ```bash
    root@cv7998847:/var/www/dianthus# docker compose exec db sh -c 'echo user=$POSTGRES_USER db=$POSTGRES_DB pass_len=${#POSTGRES_PASSWORD}'
    user=dianthus_user db=dianthus pass_len=14
    ```
    Контейнер `db` запущен, его переменные окружения корректны. Проблема на стороне приложения.

## Причина ошибки

В Docker-контейнере `app` служба PostgreSQL доступна **не по адресу `localhost`**, а по имени сервиса из `docker-compose.yml` — то есть по хосту **`db`**. Приложение читает адрес базы из переменной `DATABASE_URL` в файле `.env`, и если там указан `localhost`, оно пытается подключиться к самому себе (внутрь контейнера app), где база не работает. Отсюда ошибка `Connection refused`.

Аналогично Redis должен быть доступен по хосту **`redis`**, а не `127.0.0.1`.

## Точное решение

1.  **Исправить `.env`**:
    ```bash
    cd /var/www/dianthus
    nano .env
    ```
    Убедись, что строки выглядят **именно так** (подставь свой пароль, который сейчас в `POSTGRES_PASSWORD`):
    ```env
    DATABASE_URL=postgresql://dianthus_user:<ПАРОЛЬ_ИЗ_POSTGRES_PASSWORD>@db:5432/dianthus
    REDIS_URL=redis://:<ПАРОЛЬ_ИЗ_REDIS_PASSWORD>@redis:6379/0
    ```
    Ключевые изменения: `@localhost` → `@db`, `@127.0.0.1` → `@redis`.

2.  **Проверка после сохранения**:
    Выполни команду без маскировки, чтобы точно увидеть хост:
    ```bash
    grep '^DATABASE_URL' .env
    ```
    Там должно читаться `@db:5432`.

3.  **Перезапуск стека**:
    ```bash
    docker compose down
    docker compose up -d
    ```

4.  **Контроль результата**:
    ```bash
    docker compose ps          # все сервисы должны стать healthy
    docker compose logs app --tail=20   # ждём «🌸 Диантус готов (prod)»
    curl -s -o /dev/null -w "%{http_code}\n" http://localhost/login   # ожидаем 200
    ```

## Важное замечание про пароль

Пароль в `DATABASE_URL` должен **совпадать** со значением `POSTGRES_PASSWORD` в том же `.env`. Если ты менял пароль в `.env` после того, как том `dianthus_postgres_data` уже был инициализирован, то внутри базы остался старый пароль. Тогда либо верни в `DATABASE_URL` тот пароль, который был при первом успешном импорте дампа, либо пересоздай том (с потерей данных):
```bash
docker compose down
docker volume rm dianthus_postgres_data
docker compose up -d db redis
# затем заново импортируй /root/db.dump
cat /root/db.dump | docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```
