FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# FFmpeg/FFprobe: объединение потоков и подготовка MP4 для iPhone.
# Сборка остановится, если в пакете нет кодировщика libx264.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && ffmpeg -hide_banner -encoders 2>/dev/null | grep -q 'libx264' \
    && ffprobe -version \
    && rm -rf /var/lib/apt/lists/*

# Deno — JavaScript runtime, используемый yt-dlp для некоторых сайтов.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

CMD ["python", "main.py"]
