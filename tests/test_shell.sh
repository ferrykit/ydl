#!/usr/bin/env bash
# Проверки shell-функций установщика и скрипта ydl (валидация ввода, определение частных адресов).
set -u
cd "$(dirname "$0")/.."
fail=0
check() { # check "описание" ожидаемый_код команда...
  desc="$1"; want="$2"; shift 2
  "$@" >/dev/null 2>&1; got=$?
  if [ "$got" = "$want" ]; then echo "  ok   $desc"; else echo "  FAIL $desc (код $got, ожидали $want)"; fail=1; fi
}
eval "$(sed -n '/^valid_pass()/,/^free_gb()/p' install.sh | sed '$d')"
eval "$(sed -n '/^valid_host()/,/^}/p' ydl)"
eval "$(sed -n '/^is_private_ip()/,/^}/p' ydl)"
eval "$(sed -n '/^cert_reason()/,/^}/p' ydl)"

echo "пароль (установщик):"
check "обычный пароль" 0 valid_pass 'Str0ng.Pass-1'
check "со знаком \$ (ломает .env)" 1 valid_pass 'pa$word1'
check "с пробелом" 1 valid_pass 'my pass12'
check "с решёткой" 1 valid_pass 'a#bcdef'
check "с кавычкой" 1 valid_pass "it's-secret1"
check "слишком короткий" 1 valid_pass 'sh0rt'
check "кириллица (задаётся командой ydl password)" 1 valid_pass 'пароль123'
echo "папка (установщик):"
check "обычная" 0 valid_dir '/mnt/disk/ydl'
check "с пробелом" 0 valid_dir '/home/user/my files'
check "со знаком \$" 1 valid_dir '/data/$HOME'
check "с решёткой" 1 valid_dir '/data/a#b'
check "с кавычкой" 1 valid_dir "/data/it's"
echo "домен/адрес (ydl):"
check "домен" 0 valid_host ydl.example.com
check "IPv4" 0 valid_host 203.0.113.5
check "punycode" 0 valid_host xn--e1afmkfd.xn--p1ai
check "инъекция команды" 1 valid_host 'ydl.example.com; rm -rf /'
check "пробел" 1 valid_host 'a b.com'
check "двойная точка" 1 valid_host 'bad..com'
check "без точки" 1 valid_host single
check "кириллица" 1 valid_host 'пример.рф'
echo "причины отказа сертификата (ydl):"
RL='{"level":"error","msg":"HTTP 429 urn:ietf:params:acme:error:rateLimited - too many certificates (5) already issued, retry after 2026-10-16 05:18:51 UTC: see https"}'
TO='{"level":"warn","msg":"HTTP request failed","url":"https://acme-v02.api.letsencrypt.org/directory","error":"context deadline exceeded"}'
CL='{"level":"error","msg":"challenge failed","error":"Timeout during connect (likely firewall problem)"}'
check "лимит Let's Encrypt узнаётся" 0 bash -c 'eval "$(sed -n "/^cert_reason()/,/^}/p" ydl)"; printf "%s" "$1" | cert_reason | grep -q "2026-10-16 05:18:51 UTC"' _ "$RL"
check "таймаут до Let's Encrypt узнаётся" 0 bash -c 'eval "$(sed -n "/^cert_reason()/,/^}/p" ydl)"; printf "%s" "$1" | cert_reason | grep -q "блокирует"' _ "$TO"
check "закрытые порты узнаются" 0 bash -c 'eval "$(sed -n "/^cert_reason()/,/^}/p" ydl)"; printf "%s" "$1" | cert_reason | grep -q "порты 80 и 443"' _ "$CL"
check "неизвестный лог — код 1" 1 bash -c 'eval "$(sed -n "/^cert_reason()/,/^}/p" ydl)"; echo "всё хорошо" | cert_reason'
echo "частные адреса (ydl):"
for ip in 10.1.2.3 192.168.0.5 172.16.0.1 172.31.255.1 169.254.1.1 127.0.0.1 100.64.0.1; do check "$ip — частный" 0 is_private_ip "$ip"; done
for ip in 8.8.8.8 203.0.113.5 172.32.0.1 100.128.0.1 1.1.1.1; do check "$ip — публичный" 1 is_private_ip "$ip"; done
echo "синтаксис:"
for f in install.sh ydl entrypoint.sh; do check "bash -n $f" 0 bash -n "$f"; done
[ "$fail" = 0 ] && echo "ВСЕ ПРОВЕРКИ ПРОШЛИ" || { echo "ЕСТЬ ОШИБКИ"; exit 1; }
