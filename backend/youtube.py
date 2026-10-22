"""YouTube channel connection (OAuth 2.0) + Shorts upload with description, thumbnail and captions.

Setup once: put the OAuth client file from Google Cloud Console at backend/client_secret.json
(type "Web application", redirect URI http://localhost:8000/api/youtube/callback).
"""
import json, os, threading
from pathlib import Path

os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")   # allow http://localhost redirect during local use
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
BASE = Path(__file__).parent
SECRET = Path(os.getenv("YT_CLIENT_SECRET", BASE/"client_secret.json"))
if os.getenv("YT_CLIENT_SECRET_JSON") and not SECRET.exists():   # hosted: pass the JSON as a secret env var
    SECRET.parent.mkdir(parents=True, exist_ok=True); SECRET.write_text(os.environ["YT_CLIENT_SECRET_JSON"])
TOKEN = Path(os.getenv("YT_TOKEN_PATH", BASE/"youtube_token.json"))
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl", "https://www.googleapis.com/auth/youtube.upload"]
REDIRECT = os.getenv("YT_REDIRECT_URI", "http://localhost:8000/api/youtube/callback")
_flows = {}; _lock = threading.Lock(); _channel_cache = {}

def configured(): return SECRET.exists()

def _creds():
    if not TOKEN.exists(): return None
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    c = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if c and c.expired and c.refresh_token:
        c.refresh(Request()); TOKEN.write_text(c.to_json())
    return c if c and c.valid else None

def _api():
    from googleapiclient.discovery import build
    c = _creds()
    if not c: raise RuntimeError("YouTube is not connected")
    return build("youtube", "v3", credentials=c, cache_discovery=False)

def auth_url():
    from google_auth_oauthlib.flow import Flow
    f = Flow.from_client_secrets_file(str(SECRET), scopes=SCOPES, redirect_uri=REDIRECT)
    url, state = f.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
    _flows[state] = f
    return url

def finish(state, full_url):
    f = _flows.pop(state, None)
    if not f: raise ValueError("Login session expired, please try again.")
    f.fetch_token(authorization_response=full_url)
    TOKEN.write_text(f.credentials.to_json()); _channel_cache.clear()

def disconnect():
    if TOKEN.exists(): TOKEN.unlink()
    _channel_cache.clear()

def status():
    if not configured(): return {"configured": False, "connected": False}
    try:
        if not _creds(): return {"configured": True, "connected": False}
        if "ch" not in _channel_cache:
            r = _api().channels().list(part="snippet", mine=True).execute()
            it = (r.get("items") or [{}])[0].get("snippet", {})
            _channel_cache["ch"] = {"title": it.get("title", "Your channel"), "thumbnail": it.get("thumbnails", {}).get("default", {}).get("url", "")}
        return {"configured": True, "connected": True, "channel": _channel_cache["ch"]}
    except Exception as e:
        print("youtube status error:", e, flush=True)
        return {"configured": True, "connected": False, "error": "Connection expired, please reconnect."}

def build_description(meta, source_url=None):
    tags = [h if h.startswith("#") else "#"+h for h in meta.get("hashtags", [])]
    if "#shorts" not in [t.lower() for t in tags]: tags.insert(0, "#Shorts")
    parts = [meta.get("description", "").strip()]
    if source_url: parts.append(f"Full video: {source_url}")
    parts.append(" ".join(tags))
    return "\n\n".join(p for p in parts if p)[:4900]

def upload(video, thumb, srt, meta, privacy="private", language="en", source_url=None, cb=None):
    """Uploads one Short. cb(stage, pct). Returns dict(id, url, warnings)."""
    from googleapiclient.http import MediaFileUpload
    yt = _api(); warnings = []
    title = (meta.get("title") or "New Short").strip()
    if "#shorts" not in title.lower() and len(title) <= 90: title += " #Shorts"
    tags = [h.lstrip("#") for h in meta.get("hashtags", []) if h.strip("#")][:15]
    body = {"snippet": {"title": title[:100], "description": build_description(meta, source_url), "tags": tags, "categoryId": meta.get("category", "22"),
                        "defaultLanguage": language, "defaultAudioLanguage": language},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    req = yt.videos().insert(part="snippet,status", body=body, media_body=MediaFileUpload(str(video), mimetype="video/mp4", chunksize=4*1024*1024, resumable=True))
    resp = None
    while resp is None:
        st, resp = req.next_chunk()
        if st and cb: cb("Uploading video", int(st.progress()*100))
    vid = resp["id"]
    if thumb and Path(thumb).exists():
        if cb: cb("Setting thumbnail", 100)
        try: yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(thumb), mimetype="image/jpeg")).execute()
        except Exception as e:
            print("thumbnail failed:", e, flush=True)
            warnings.append("Thumbnail skipped — custom thumbnails need a phone-verified channel (youtube.com/verify).")
    if srt and Path(srt).exists():
        if cb: cb("Adding captions", 100)
        try: yt.captions().insert(part="snippet", body={"snippet": {"videoId": vid, "language": language, "name": "Subtitles", "isDraft": False}},
                                  media_body=MediaFileUpload(str(srt), mimetype="application/octet-stream")).execute()
        except Exception as e: print("captions failed:", e, flush=True); warnings.append("Captions upload failed.")
    return {"id": vid, "url": f"https://youtube.com/shorts/{vid}", "warnings": warnings}
