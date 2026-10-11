FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --upgrade certifi discord.py requests youtube-transcript-api openai \
    # httpx/openai доверяют бандлу certifi; подменяем его симлинком на полный
    # системный стор Debian (актуальнее и покрывает цепочки z.ai)
    && ln -sf /usr/lib/ssl/cert.pem /usr/local/lib/python3.12/site-packages/certifi/cacert.pem

COPY entrypoint.sh /app/entrypoint.sh
# exec-бит задаем при каждом старте (см. ENTRYPOINT): монтируемая папка Windows
# может сбросить права после git reset --hard
ENTRYPOINT ["/bin/sh", "-c", "chmod +x /app/entrypoint.sh && exec /app/entrypoint.sh"]

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/app/entrypoint.sh"]
