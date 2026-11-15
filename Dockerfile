# Process Annotation - Work Standardization & Kaizen. One container: web app + background analysis.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WORKSTUDY_DATA=/data \
    WORKSTUDY_AUTH=1 \
    PORT=8000

# system ffmpeg: works on x86 and ARM (Oracle's free Ampere servers); imageio-ffmpeg has no ARM binary
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
ENV FFMPEG_BIN=/usr/bin/ffmpeg

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.py check_study.py ./

# /data must be a persistent disk: studies, videos, station templates and accounts live there
VOLUME ["/data"]
EXPOSE 8000
# one worker: analyses run as background tasks in this process
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
