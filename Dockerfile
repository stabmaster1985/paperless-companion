# Version (Commit) aus dem .git-Verzeichnis ablesen – ohne git im Image
FROM python:3.12-slim AS version
WORKDIR /src
COPY .git ./.git
COPY app/gitinfo.py ./gitinfo.py
RUN python -c "import json, sys; from pathlib import Path; sys.path.insert(0, '.'); \
from gitinfo import read_git_version; print(json.dumps(read_git_version(Path('.'))))" > /version.json

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY --from=version /version.json ./version.json

RUN useradd --create-home --uid 1000 companion \
    && mkdir -p /data && chown companion:companion /data
USER companion
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

# Nur ein Worker-Prozess: der Hintergrund-Thread darf nicht doppelt laufen
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
