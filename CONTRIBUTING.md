# Как помочь проекту / Contributing

Спасибо за интерес! Проект маленький, поэтому правила простые.

## Прежде чем отправить изменение
```bash
python -m unittest discover -s tests -v   # Python 3.12+ (как в образе), без сторонних пакетов
bash tests/test_shell.sh
```
- Меняете поведение защиты (вход, проверка `X-YDL`/`Origin`/`Host`, раздача файлов, пути) — добавьте или обновите тест.
- Интерфейс (`app/page.html`, `app/files.html`) и серверная часть (`app/ydl.py`) намеренно без зависимостей и без сборки: открывается и правится как обычный файл.
- Описание изменений — в `CHANGELOG.md`; версия — в `app/VERSION`.

## Об уязвимостях
Не создавайте публичный issue, см. [SECURITY.md](SECURITY.md).

## Что не принимается
Обход ограничений и капчи Яндекса, массовое «выкачивание» чужих публичных ссылок, сбор данных о пользователях.

## English
Run the two test commands above before opening a pull request. Security-relevant changes need a test. Please report vulnerabilities privately (see SECURITY.md). Out of scope: bypassing Yandex's rate limits or captcha, bulk scraping of other people's links.
