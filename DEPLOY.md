# Deploy ClipForge AI online (free) — Hugging Face Spaces

1. Sign in at huggingface.co → **New Space** → name `clipforge-ai`, SDK **Docker** (Blank), hardware **CPU basic (free)**, visibility **Private** is fine.
2. **Files → Add file → Upload files**: drag in `Dockerfile`, `.dockerignore`, `README.md` and the `backend` folder
   (do NOT upload `backend/venv`, `data`, `client_secret.json`). Commit.
3. Add this line at the very top of the Space's README.md (HF needs it):
   ```
   ---
   sdk: docker
   app_port: 7860
   ---
   ```
4. **Settings → Variables and secrets** → add *Secrets*:
   - `APP_PASSWORD` = a password (stops strangers posting to your channel)
   - `YT_REDIRECT_URI` = `https://<your-username>-clipforge-ai.hf.space/api/youtube/callback`
   - `YT_CLIENT_SECRET_JSON` = paste the full contents of the downloaded client_secret JSON
   - `ANTHROPIC_API_KEY` = (optional) for AI titles/ranking
5. Google Cloud Console → Credentials → your OAuth client → **Authorised redirect URIs**: add the same
   `https://<your-username>-clipforge-ai.hf.space/api/youtube/callback` (keep the localhost one too). Save.
6. Wait for the build (~5–10 min) → open `https://<your-username>-clipforge-ai.hf.space` → enter the password
   → **Connect YouTube** → sign in as sabarish3092004@gmail.com → Allow.

Notes
- Free Spaces sleep after inactivity and wipe files on restart → you may need to click Connect YouTube again.
- YouTube often blocks link downloads from cloud servers ("confirm you're not a bot"). If a link fails, use **upload a file**.
- Free CPU is slow: transcription uses WHISPER_MODEL=base.
