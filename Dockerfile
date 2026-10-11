FROM python:3.12-slim

WORKDIR /app

# requirements.txt копируется ДО основного COPY: слой pip пересобирается только
# при смене зависимостей, а не при любом изменении кода. Ставим строго из
# requirements.txt (в нём и pyyaml, и readability-lxml) — образ самодостаточен,
# ручной список пакетов не дрейфует от репозитория.
COPY requirements.txt /app/requirements.txt

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    # httpx/openai доверяют бандлу certifi; подменяем его симлинком на полный
    # системный стор Debian (актуальнее и покрывает цепочки z.ai)
    && ln -sf /usr/lib/ssl/cert.pem /usr/local/lib/python3.12/site-packages/certifi/cacert.pem

COPY entrypoint.sh /app/entrypoint.sh
# exec-бит задаем при каждом старте: монтируемая папка Windows
# может сбросить права после git reset --hard
ENTRYPOINT ["/bin/sh", "-c", "chmod +x /app/entrypoint.sh && exec /app/entrypoint.sh"]

ENV PYTHONUNBUFFERED=1
