# ClipForge AI — container for online hosting (Hugging Face Spaces / Render / Railway)
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 app
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt && pip install --no-cache-dir -U yt-dlp
COPY --chown=app:app backend/ backend/
RUN mkdir -p /app/data /app/.cache && chown -R app:app /app
USER app
ENV DATA_DIR=/app/data HF_HOME=/app/.cache YT_TOKEN_PATH=/app/data/youtube_token.json \
    WHISPER_MODEL=base PORT=7860 PYTHONUNBUFFERED=1
WORKDIR /app/backend
EXPOSE 7860
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
