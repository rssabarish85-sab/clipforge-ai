#!/bin/bash
# Double-click to start ClipForge AI, then it opens http://localhost:8000
cd "$(dirname "$0")/backend" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
echo "== ClipForge AI =="
if ! command -v ffmpeg >/dev/null; then
  if command -v brew >/dev/null; then echo "Installing ffmpeg…"; brew install ffmpeg; else echo "Please install ffmpeg (https://ffmpeg.org) and try again"; read -n1; exit 1; fi
fi
# YouTube now needs a JavaScript runtime for yt-dlp (avoids bot-checks / missing formats)
if ! command -v deno >/dev/null && [ ! -x bin/deno ]; then
  echo "Downloading deno (needed for YouTube links)…"
  case "$(uname -m)" in arm64) DA=aarch64-apple-darwin;; *) DA=x86_64-apple-darwin;; esac
  mkdir -p bin && curl -fsSL -o bin/deno.zip "https://github.com/denoland/deno/releases/latest/download/deno-$DA.zip" \
    && unzip -oq bin/deno.zip -d bin && rm -f bin/deno.zip && chmod +x bin/deno || echo "Could not download deno — YouTube links may fail (file uploads still work)."
fi
[ -d venv ] || python3 -m venv venv
source venv/bin/activate
echo "Checking Python packages…"
pip install -q -r requirements.txt && pip install -q -U "yt-dlp[default]"
[ -f ../.env ] && export $(grep -v '^#' ../.env | sed 's/ *#.*//' | grep '=' | xargs)
PIDS=$(lsof -ti tcp:8000); if [ -n "$PIDS" ]; then echo "Stopping old app on port 8000…"; kill $PIDS
  for i in 1 2 3 4 5 6 7 8 9 10; do sleep 1; lsof -ti tcp:8000 >/dev/null || break; done
  PIDS=$(lsof -ti tcp:8000); [ -n "$PIDS" ] && kill -9 $PIDS && sleep 1; fi
(sleep 4; open "http://localhost:8000") &
echo "Server running at http://localhost:8000  (close this window to stop)"
uvicorn main:app --port 8000
