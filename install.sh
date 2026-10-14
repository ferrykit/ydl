#!/usr/bin/env bash
# Установка ydl: несколько вопросов (на все есть разумный ответ по умолчанию — достаточно Enter),
# затем сборка и запуск. Запускать из папки проекта: ./install.sh
# Без вопросов можно задать переменными: YDL_DOWNLOADS=/путь YDL_PORT=8085 YDL_PASSWORD=... ./install.sh
set -e
cd "$(dirname "$0")"

say() { printf '%s\n' "$*"; }
ask() { # ask "вопрос" "по умолчанию" -> ответ в $REPLY
  REPLY=""
  if [ -t 0 ]; then printf '%s [%s]: ' "$1" "$2"; read -r REPLY || REPLY=""; fi
  [ -n "$REPLY" ] || REPLY="$2"
}
valid_pass() { # допустимые символы: безопасные для .env и docker compose (без $ # пробелов, кавычек и т.п.)
  case "$1" in ''|*[!A-Za-z0-9._@%+=:,/~^!*-]*) return 1 ;; esac
  [ "${#1}" -ge 6 ]
}
valid_dir() { case "$1" in *[\$\#\"\'\`\\]*|*"
"*) return 1 ;; esac; return 0; }
free_gb() { df -Pk "$1" 2>/dev/null | awk 'NR==2{printf "%d", $4/1048576}'; }
port_busy() { (ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -q "[:.]$1 "; }

have_compose() { docker compose version >/dev/null 2>&1; }

ensure_docker() {
  command -v docker >/dev/null 2>&1 && have_compose && return 0
  say ""
  if [ "$(uname)" = "Darwin" ]; then
    say "Нужен Docker Desktop: https://www.docker.com/products/docker-desktop/ . Установите его, запустите и повторите ./install.sh"
    exit 1
  fi
  if command -v docker >/dev/null 2>&1; then say "Docker найден, но нет плагина «docker compose» (нужна версия 2+)."; else say "Docker на этом компьютере не найден."; fi
  if [ "$YDL_INSTALL_DOCKER" = "0" ]; then
    say "Установите вручную: https://docs.docker.com/engine/install/ и запустите ./install.sh снова."; exit 1
  fi
  ask "Установить Docker автоматически сейчас? (y/n)" "y"
  case "$REPLY" in [nNнН]*) say "Установите вручную: https://docs.docker.com/engine/install/ и запустите ./install.sh снова."; exit 1 ;; esac

  SUDO=""
  if [ "$(id -u)" != 0 ]; then
    command -v sudo >/dev/null 2>&1 || { say "Для установки Docker нужны права администратора (запустите от root или установите sudo)."; exit 1; }
    SUDO="sudo"
  fi
  OSID=""; OSLIKE=""
  [ -r /etc/os-release ] && . /etc/os-release && OSID="$ID" && OSLIKE="$ID_LIKE"

  case " $OSID $OSLIKE " in
    *" debian "*|*" ubuntu "*)
      say "Ставлю Docker из репозитория системы (1–3 минуты)…"
      $SUDO env DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1 || true
      $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose-v2 docker-buildx >/dev/null 2>&1 || true
      ;;
  esac
  if ! { command -v docker >/dev/null 2>&1 && have_compose; }; then
    say "Ставлю Docker официальным скриптом get.docker.com…"
    command -v curl >/dev/null 2>&1 || { say "Нужен curl: установите его (например, apt install curl) и повторите."; exit 1; }
    curl -fsSL https://get.docker.com | $SUDO sh >/dev/null 2>&1 || true
  fi
  if ! { command -v docker >/dev/null 2>&1 && have_compose; }; then
    say "Не удалось установить Docker автоматически. Установите вручную: https://docs.docker.com/engine/install/"
    exit 1
  fi
  $SUDO systemctl enable --now docker >/dev/null 2>&1 || $SUDO service docker start >/dev/null 2>&1 || true
  say "Docker установлен: $(docker --version 2>/dev/null)"

  # обычному пользователю нужна группа docker; применяем её сразу, без выхода из системы
  if [ -n "$SUDO" ] && ! docker info >/dev/null 2>&1; then
    $SUDO usermod -aG docker "$USER" 2>/dev/null || true
    if command -v sg >/dev/null 2>&1; then
      say "Добавил вас в группу docker, продолжаю установку…"
      exec sg docker -c "cd '$PWD' && YDL_INSTALL_DOCKER=0 '$0'"
    fi
  fi
}

say ""
say "ydl — ранняя версия ($(cat app/STATUS 2>/dev/null || echo alpha)). Написана быстро и выпущена как есть: возможны ошибки."
say "Пользуйтесь на свой страх и риск, не храните на ней единственные копии данных. Подробнее: README, раздел «Дисклеймер»."
say ""
ensure_docker
docker info >/dev/null 2>&1 || { say "Docker не отвечает. Если пишет «permission denied» — запустите через sudo или добавьте себя в группу docker:  sudo usermod -aG docker \$USER  (потом перелогиньтесь). Если сервис не запущен: sudo systemctl start docker"; exit 1; }

NEWENV=0
if [ -f .env ]; then
  say "Найден .env — использую его настройки (удалите файл, чтобы пройти вопросы заново)."
else
  NEWENV=1
  say "=== Настройка ydl (Enter — вариант по умолчанию) ==="; say ""

  # ---- где будет работать сервис: дома или в интернете, с HTTPS или без
  LAN="$(./ydl _lanip)"; PUB=""
  HTTPS_MODE="${YDL_HTTPS:-}"; HTTPS_HOST="${YDL_HTTPS_HOST:-}"
  if [ -z "$HTTPS_MODE" ]; then
    DEFSEL=1
    if [ -n "$LAN" ] && ! ./ydl _isprivate "$LAN"; then DEFSEL=2; PUB="$LAN"; fi      # внешних запросов за адресом не делаем, пока это не нужно
    say "Где будет работать ydl?"
    say "  1) Дома или в локальной сети (адрес вида 192.168.x.x), без шифрования. Проще всего."
    say "  2) Сервер в интернете (VPS) с публичным IP: HTTPS по IP-адресу${PUB:+ ($PUB)}, бесплатный сертификат Let's Encrypt."
    say "  3) У меня есть домен (например ydl.example.com): HTTPS по домену (сервер должен быть доступен из интернета)."
    ask "Выбор" "$DEFSEL"
    case "$REPLY" in 2) HTTPS_MODE=ip ;; 3) HTTPS_MODE=domain ;; *) HTTPS_MODE=off ;; esac
    say ""
  fi
  case "$HTTPS_MODE" in
    ip)
      [ -n "$HTTPS_HOST" ] || HTTPS_HOST="${PUB:-$(./ydl _pubip)}"
      if [ -z "$HTTPS_HOST" ]; then ask "Публичный IP этого сервера" ""; HTTPS_HOST="$REPLY"; fi
      if [ -n "$HTTPS_HOST" ] && ! ./ydl _validhost "$HTTPS_HOST"; then say "«$HTTPS_HOST» не похоже на IP-адрес."; HTTPS_HOST=""; fi
      if [ -z "$HTTPS_HOST" ] || ./ydl _isprivate "$HTTPS_HOST"; then
        say "Публичный IP не определён или адрес частный (Let's Encrypt выдаёт сертификаты только на публичные IP)."
        say "Продолжаю без HTTPS (позже, если сервер получит публичный адрес: ./ydl https ip)."
        HTTPS_MODE=off
      fi ;;
    domain)
      if [ -z "$HTTPS_HOST" ]; then ask "Ваш домен (например ydl.example.com)" ""; HTTPS_HOST="$REPLY"; fi
      if [ -n "$HTTPS_HOST" ] && ! ./ydl _validhost "$HTTPS_HOST"; then
        say "«$HTTPS_HOST» не похоже на домен (допустимы латинские буквы, цифры, точка, дефис; кириллический домен укажите как xn--…)."; HTTPS_HOST=""
      fi
      if [ -z "$HTTPS_HOST" ]; then
        say "Домен не указан или некорректен, продолжаю без HTTPS (позже: ./ydl https domain ИМЯ)."; HTTPS_MODE=off
      else
        PUB="${PUB:-$(./ydl _pubip)}"
        DNS_ST=0; ./ydl _dnscheck "$HTTPS_HOST" "$PUB" || DNS_ST=$?     # ненулевой код — это ответ, а не авария (set -e)
        if [ "$DNS_ST" -ne 0 ]; then
          if [ "$DNS_ST" -eq 1 ]; then
            say "  Домен $HTTPS_HOST пока ни на что не указывает. Добавьте у регистратора A-запись на ${PUB:-IP этого сервера}."
          else
            say "  Домен $HTTPS_HOST указывает не на этот сервер (адрес сервера: $PUB)."
          fi
          if [ -t 0 ]; then
            ask "Подождать, пока DNS обновится (до 10 минут)? (y/n)" "y"
            case "$REPLY" in [nNнН]*) say "  Продолжаю: пока адрес не совпадёт, сертификат не выдастся." ;;
              *) printf '  Жду'
                 if ./ydl _dnswait "$HTTPS_HOST" "$PUB"; then say ""; say "  DNS обновился."; else say ""; say "  DNS пока не обновился: продолжаю, Caddy будет пробовать выпустить сертификат сам."; fi ;;
            esac
          else
            say "  Продолжаю: пока адрес не совпадёт, сертификат не выдастся (Caddy будет пробовать сам)."
          fi
        fi
        if ./ydl _dnsv6 "$HTTPS_HOST"; then
          say "  ВНИМАНИЕ: у домена есть AAAA-запись (IPv6), которая не указывает на этот сервер. Let's Encrypt проверяет и по IPv6, поэтому выпуск может не пройти: удалите AAAA-запись у регистратора или направьте её на этот сервер."
        fi
      fi ;;
    *) HTTPS_MODE=off ;;
  esac
  if [ "$HTTPS_MODE" != off ]; then
    if ./ydl _portbusy 443; then
      say "  Порт 443 на этом сервере занят другой программой (веб-сервер nginx/apache?). Для HTTPS он обязателен."
      ask "Продолжить без HTTPS? (y/n)" "y"
      case "$REPLY" in [nNнН]*) say "Освободите порт 443 и запустите ./install.sh снова."; exit 1 ;; esac
      HTTPS_MODE=off; HTTPS_HOST=""
    elif ./ydl _portbusy 80; then
      say "  Порт 80 занят другой программой: оставляю её в покое. HTTPS будет работать только на порту 443 (сертификат подтверждается через него)."
      say ""
    fi
  fi

  # ---- куда складывать файлы
  if [ -z "$YDL_DOWNLOADS" ]; then
    say "Куда скачивать файлы? Свободное место на дисках этого компьютера:"
    OPTS="$HOME/ydl-downloads"; i=1
    say "  $i) $HOME/ydl-downloads   (свободно $(free_gb "$HOME") ГБ)"
    for m in $(df -Pk 2>/dev/null | awk 'NR>1 && $1 ~ /^\/dev\// && $6 !~ /^\/(boot|snap|System|private|Volumes\/Recovery)/ && $4 > 20971520 {print $6}' | sort -u); do
      [ "$m" = "/" ] && continue
      [ -w "$m" ] || [ "$(id -u)" = 0 ] || continue
      i=$((i+1)); OPTS="$OPTS|$m/ydl-downloads"
      say "  $i) $m/ydl-downloads   (свободно $(free_gb "$m") ГБ)"
    done
    say "  Или введите свой путь (папка будет создана)."
    ask "Номер или путь" "1"
    case "$REPLY" in
      [0-9]*) DL="$(printf '%s' "$OPTS" | cut -d'|' -f"$REPLY")"; [ -n "$DL" ] || DL="$HOME/ydl-downloads" ;;
      *)      DL="$REPLY" ;;
    esac
  else
    DL="$YDL_DOWNLOADS"
  fi
  case "$DL" in "~"*) DL="$HOME${DL#\~}" ;; esac
  case "$DL" in /*) ;; *) DL="$(pwd)/$DL" ;; esac
  valid_dir "$DL" || { say "В пути к папке есть символы \$ # кавычки или обратный слэш: они ломают настройки. Выберите другую папку."; exit 1; }
  mkdir -p "$DL" 2>/dev/null || { say "Не могу создать папку $DL (нет прав?). Выберите другую или запустите через sudo."; exit 1; }
  [ -w "$DL" ] || { say "В папку $DL нельзя писать. Выберите другую."; exit 1; }
  FG="$(free_gb "$DL")"
  say "→ Файлы будут в: $DL (свободно ${FG:-?} ГБ)"
  [ -z "$FG" ] || [ "$FG" -ge 20 ] || say "  ВНИМАНИЕ: свободного места мало. Папка Яндекс.Диска может весить сотни ГБ."
  say ""

  # ---- порт
  PORT="${YDL_PORT:-8085}"
  while port_busy "$PORT"; do PORT=$((PORT+1)); done
  if [ "$HTTPS_MODE" = off ]; then ask "Порт страницы управления" "$PORT"; PORT="$REPLY"; fi

  # ---- слоты
  say ""
  say "Слоты: сколько файлов скачивается одновременно. 8 подходит большинству."
  say "Если интернет медленный (до ~50 Мбит/с) или Яндекс часто просит притормозить, поставьте 4. Позже: ./ydl slots 6"
  SLOTS="${YDL_SLOTS:-8}"
  ask "Количество слотов (1–32)" "$SLOTS"; SLOTS="$REPLY"
  case "$SLOTS" in ''|*[!0-9]*) SLOTS=8 ;; esac
  [ "$SLOTS" -ge 1 ] 2>/dev/null || SLOTS=8
  [ "$SLOTS" -le 32 ] || SLOTS=32

  # ---- пароль
  say ""
  say "Страницу можно закрыть паролем (страница входа). Рекомендуется всегда, особенно если сервер виден из"
  say "интернета: на странице вы вводите пароли от своих ссылок. Включить или выключить можно и потом (./ydl password)."
  GEN="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n' | head -c 16)"
  if [ -n "$YDL_PASSWORD" ]; then
    PASS="$YDL_PASSWORD"
  else
    ask "Закрыть страницу паролем? (y/n)" "y"
    case "$REPLY" in
      [nNнН]*) PASS="" ;;
      *) ask "Пароль" "$GEN"; PASS="$REPLY" ;;
    esac
  fi
  if [ -n "$PASS" ] && ! valid_pass "$PASS"; then
    say "  Пароль не подошёл: нужно от 6 символов, только латинские буквы, цифры и . _ @ % + = : , / ~ ^ ! * -"
    say "  (любой другой пароль, в том числе с кириллицей, можно задать потом: ./ydl password ...). Использую случайный."
    PASS="$GEN"
  fi

  # ---- автообновление
  AUTO=n
  if [ -d .git ] || [ -n "$YDL_UPDATE_URL" ] || [ -n "$YDL_IMAGE" ]; then
    say ""
    say "Автообновление скачивает и запускает новый код из источника, откуда вы взяли ydl (доверяйте автору)."
    ask "Обновлять ydl автоматически по ночам? (y/n)" "n"; AUTO="$REPLY"
  fi

  # Сервис не должен работать от root: если установщик запущен от root, используем обычного пользователя 1000
  RUN_UID="$(id -u)"; RUN_GID="$(id -g)"
  if [ "$RUN_UID" = 0 ]; then RUN_UID=1000; RUN_GID=1000; fi
  TZ_NOW="$( [ -f /etc/timezone ] && cat /etc/timezone || echo Europe/Moscow )"
  {
    echo "YDL_DOWNLOADS=$DL"
    echo "YDL_PORT=$PORT"
    echo "YDL_PASSWORD=$PASS"
    echo "YDL_SLOTS=$SLOTS"
    echo "YDL_SCOUT=1"
    echo "YDL_HTTPS=$HTTPS_MODE"
    echo "YDL_HTTPS_HOST=$HTTPS_HOST"
    if [ "$HTTPS_MODE" != off ]; then echo "YDL_BIND=127.0.0.1"; echo "YDL_BEHIND_PROXY=1"; echo "COMPOSE_PROFILES=https"; fi
    echo "PUID=$RUN_UID"
    echo "PGID=$RUN_GID"
    echo "TZ=$TZ_NOW"
    [ -z "$YDL_IMAGE" ] || echo "YDL_IMAGE=$YDL_IMAGE"
    [ -z "$YDL_UPDATE_URL" ] || echo "YDL_UPDATE_URL=$YDL_UPDATE_URL"
    [ -z "$YDL_ACME_STAGING" ] || echo "YDL_ACME_STAGING=$YDL_ACME_STAGING"   # для тестов: тестовый сертификат Let's Encrypt
  } > .env
  chmod 600 .env
fi

MODE_NOW="$(grep '^YDL_HTTPS=' .env | cut -d= -f2-)"
if [ -n "$MODE_NOW" ] && [ "$MODE_NOW" != off ]; then ./ydl _httpmap; ./ydl _caddyfile; if [ "$(grep '^YDL_HTTPS_NO80=' .env | cut -d= -f2-)" = 1 ]; then ./ydl _fw "443"; else ./ydl _fw "80 443"; fi; else ./ydl _fw "$(grep '^YDL_PORT=' .env | cut -d= -f2)"; fi

say ""
if grep -q '^YDL_IMAGE=' .env 2>/dev/null; then
  say "Скачиваю готовый образ…"; docker compose pull
else
  say "Собираю образ (первый раз 3–5 минут, дальше быстро)…"; docker compose build
fi
docker compose up -d

case "$AUTO" in [yYдД]*) ./ydl autoupdate on ;; esac

PORT="$(grep '^YDL_PORT=' .env | cut -d= -f2)"
DL="$(grep '^YDL_DOWNLOADS=' .env | cut -d= -f2-)"
PASSNOW="$(grep '^YDL_PASSWORD=' .env | cut -d= -f2-)"

say ""
printf 'Жду, пока сервис запустится'
ready=0; i=0
while [ $i -lt 45 ]; do
  if docker compose exec -T ydl python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8085/healthz', timeout=2)" >/dev/null 2>&1; then ready=1; break; fi
  printf '.'; sleep 1; i=$((i+1))
done
say ""

CERTOK=""
if [ "$MODE_NOW" = ip ] || [ "$MODE_NOW" = domain ]; then
  say "Жду, пока Let's Encrypt выдаст сертификат (до 2 минут)…"
  ./ydl _waitcert && CERTOK=1
fi

URLS="$(./ydl urls)"
FIRST="$(printf '%s\n' "$URLS" | head -1)"
IPONLY="${FIRST#*://}"; IPONLY="${IPONLY%%:*}"
say ""
say "=============================================================="
if [ "$ready" = 1 ]; then say "  ydl установлен и запущен"; else say "  ydl установлен, но пока не ответил за 45 секунд"; fi
say "=============================================================="
say ""
say "  Откройте в браузере:"
say ""
say "      $FIRST"
say ""
if [ "$(printf '%s\n' "$URLS" | wc -l)" -gt 1 ]; then
  say "  Другие адреса этого сервера (если первый не подходит):"
  printf '%s\n' "$URLS" | tail -n +2 | sed 's/^/      /'
  say ""
fi
say "  С самого сервера: http://localhost:$PORT"
say ""
case "$MODE_NOW" in
  ip)
    if [ "$(grep '^YDL_HTTPS_NO80=' .env | cut -d= -f2-)" = 1 ]; then say "  Порт 80 на сервере занят другой программой, поэтому HTTPS работает только на порту 443."; fi
    if [ -n "$CERTOK" ]; then say "  HTTPS работает: сертификат Let's Encrypt на IP получен."; else say "  ВНИМАНИЕ: сертификат пока не получен. Проверьте, что порты 80 и 443 открыты (в том числе в файрволе провайдера): ./ydl logs caddy"; fi
    if [ "$(grep '^YDL_HTTPS_NO80=' .env | cut -d= -f2-)" = 1 ]; then say "  Такие сертификаты живут 6 дней и продлеваются сами: порт 443 должен оставаться открытым."; else say "  Такие сертификаты живут 6 дней и продлеваются сами: порты 80 и 443 должны оставаться открытыми."; fi
    say "" ;;
  domain)
    if [ -n "$CERTOK" ]; then say "  HTTPS работает: сертификат Let's Encrypt для домена получен."; else say "  ВНИМАНИЕ: сертификат пока не получен. Проверьте, что домен указывает на этот сервер и порты 80/443 открыты: ./ydl logs caddy"; fi
    say "" ;;
esac
if [ -n "$PASSNOW" ] && [ "$NEWENV" = 1 ]; then
  say "  Пароль для входа (логина нет):"
  say ""
  say "      $PASSNOW"
  say ""
  say "  Запишите его. Забыли или хотите сменить: ./ydl password"
elif [ -n "$PASSNOW" ]; then
  say "  Пароль для входа остался прежним (его могли сменить командой ./ydl password). Забыли: ./ydl password"
else
  say "  Пароль не задан: вход без пароля (только для домашней сети)."
  say "  Включить: ./ydl password"
fi
say ""
say "  Файлы будут лежать в: $DL"
if [ "$(grep '^PUID=' .env | cut -d= -f2-)" = 1000 ] && [ "$(id -u)" = 0 ]; then
  say "  Сервис работает не от root, а от обычного пользователя (uid 1000), поэтому файлы принадлежат ему; для вас (root) они доступны как обычно."
fi
say "  Слотов (одновременных скачиваний): $(grep '^YDL_SLOTS=' .env | cut -d= -f2-)"
say ""
if [ "$ready" != 1 ]; then
  say "  Если страница не открывается через минуту: ./ydl logs"
fi
if [ "$MODE_NOW" = off ]; then
  say "  Страница не открывается с другого компьютера? Откройте порт $PORT в файрволе сервера"
  say "  (например: sudo ufw allow $PORT/tcp)."
else
  if [ "$(grep '^YDL_HTTPS_NO80=' .env | cut -d= -f2-)" = 1 ]; then
    say "  Страница не открывается снаружи? Откройте порт 443 в файрволе сервера и у провайдера"
    say "  (например: sudo ufw allow 443/tcp)."
  else
    say "  Страница не открывается снаружи? Откройте порты 80 и 443 в файрволе сервера и у провайдера"
    say "  (например: sudo ufw allow 80/tcp && sudo ufw allow 443/tcp)."
  fi
fi
say ""
say "  Забрать скачанное: кнопка «Файлы» на странице или по сети:"
say "      rsync -avP $(id -un)@${IPONLY}:'$DL'/ ./папка-на-вашем-компьютере/"
say "  Все команды: ./ydl help      Показать этот адрес снова: ./ydl info"
say ""
