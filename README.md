# ClipForge AI — Free AI YouTube Shorts Maker

Paste a YouTube link **or** upload a video → AI transcribes it, finds the most viral moments and renders
ready-to-post 9:16 Shorts with word-by-word captions (your colours & size), saved in Full HD 1080p.

## Run it
| Computer | What to do |
|---|---|
| **Mac** | Double-click **`Start ClipForge.command`** |
| **Windows** | Double-click **`Start ClipForge (Windows).bat`** |

The first start installs everything it needs (5–10 min, needs internet), then opens **http://localhost:8000**.
Keep the black/Terminal window open while you use the app — closing it stops ClipForge.

### Needs (install once on a new computer)
- **Python 3.11–3.13** — https://www.python.org/downloads/ (Windows: tick **"Add python.exe to PATH"**)
- **Mac only:** Homebrew — https://brew.sh (the start file uses it to install ffmpeg)
- ffmpeg, deno and all Python packages are installed automatically by the start file.

## Where clips go
**Desktop › Short YouTube** — Full HD 1080p / HD 720p / SD 480p. Saving the same clip twice never makes a copy.

## Optional settings
Copy `.env.example` to `.env` and add `ANTHROPIC_API_KEY=` for AI-written titles & hashtags.

## Speed
- Apple-Silicon Mac: speech recognition runs on the GPU — about 2–5 min per video.
- Windows / Intel Mac: runs on the CPU — slower (set `WHISPER_MODEL=base` or `tiny` in `.env` for speed).

## Connect YouTube (optional, for auto-posting)
Click **Connect YouTube** in the app and follow the steps. Your `client_secret.json` and login token are
**never** uploaded to GitHub (see `.gitignore`) — copy `client_secret.json` to each computer by hand.

Only process videos you own or have permission to use.
