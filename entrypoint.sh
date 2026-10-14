#!/bin/sh
# Запускается от root только чтобы выставить владельца папок, дальше сервис работает от PUID:PGID —
# тогда скачанные файлы принадлежат вашему пользователю, а не root.
set -e
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
mkdir -p "$YDL_CONFIG" "$YDL_DATA"
chown -R "$PUID:$PGID" "$YDL_CONFIG" 2>/dev/null || true
chown "$PUID:$PGID" "$YDL_DATA" 2>/dev/null || true      # только верхний уровень, содержимое не трогаем
if [ ! -w "$YDL_DATA" ] && [ "$(id -u)" = "0" ]; then
  setpriv --reuid="$PUID" --regid="$PGID" --clear-groups test -w "$YDL_DATA" \
    || echo "ВНИМАНИЕ: пользователь $PUID:$PGID не может писать в папку данных. Проверьте права на неё на сервере." >&2
fi
exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups python /app/ydl.py
