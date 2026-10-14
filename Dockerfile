FROM python:3.14-slim-bookworm

# Chromium (Playwright) нужен только для ссылок под паролем; остальное — чистая стандартная библиотека.
ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
    YDL_DATA=/data \
    YDL_CONFIG=/config \
    HOME=/tmp

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata util-linux ca-certificates \
 && pip install --no-cache-dir playwright==1.55.0 \
 && playwright install --with-deps chromium \
 && chmod -R a+rX /opt/pw-browsers \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app/ /app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8085
VOLUME ["/data", "/config"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('YDL_PORT','8085'), timeout=4)" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
