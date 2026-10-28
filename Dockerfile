
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DENO_INSTALL=/root/.deno \
    PATH="/root/.deno/bin:${PATH}"

# FFmpeg нужен для объединения звука и видео.
# Deno используется yt-dlp для поддержки JavaScript на YouTube.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg \
       curl \
       ca-certificates \
       unzip \
    && curl -fsSL https://deno.land/install.sh | sh \
    && ffmpeg -version \
    && ffprobe -version \
    && deno --version \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN python -m pip install --upgrade pip \
    && pip install -r requirements.txt \
    && python -m yt_dlp --version

COPY . .

CMD ["python", "main.py"]
