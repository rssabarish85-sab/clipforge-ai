"""ClipForge AI — FastAPI backend with live (SSE) progress and a built-in web UI at http://localhost:8000"""
import asyncio, json, os, re, sys, uuid, zipfile, subprocess, threading, time
from pathlib import Path
from typing import Optional, List
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi import Request
from fastapi.responses import FileResponse, StreamingResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import pipeline as P
import youtube as YT
import queue
import functools; print = functools.partial(print, flush=True)  # show errors in the terminal immediately

BASE = Path(__file__).parent
DATA = Path(os.getenv("DATA_DIR", BASE.parent/"data")); MAX_MB = int(os.getenv("MAX_UPLOAD_MB", 2048))
for d in ("uploads","downloads","clips","exports"): (DATA/d).mkdir(parents=True, exist_ok=True)
app = FastAPI(title="ClipForge AI")
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS","*").split(","), allow_methods=["*"], allow_headers=["*"])

# Optional password for a public deployment: set APP_PASSWORD (any username works in the browser prompt)
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
if APP_PASSWORD:
    import base64, secrets
    from fastapi.responses import Response
    @app.middleware("http")
    async def _password(request: Request, call_next):
        h = request.headers.get("authorization", "")
        try: ok = h.startswith("Basic ") and secrets.compare_digest(base64.b64decode(h[6:]).decode().split(":", 1)[1], APP_PASSWORD)
        except Exception: ok = False
        if not ok: return Response("Password required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="ClipForge"'})
        return await call_next(request)

JOBS: dict[str, dict] = {}; LOCK = threading.Lock()
STEPS = ["Importing video","Reading video info","Extracting audio","Transcribing speech","Finding viral moments","Rendering clips","Writing titles & hashtags"]
WEIGHTS = [20, 2, 3, 30, 3, 38, 4]   # share of the overall progress bar per step
LENGTHS = {"auto":(20,90), "short":(15,30), "medium":(30,60), "long":(60,90)}

class UrlIn(BaseModel):
    url: str; fmt: str = "9:16"; length: str = "auto"; count: int = 5; captions: bool = True; autopost: bool = False; privacy: str = "private"; capcolor: str = "#FFFFFF"; hicolor: str = "#FFE500"; capsize: str = "m"
class PublishIn(BaseModel):
    privacy: str = "private"
class MetaIn(BaseModel):
    title: Optional[str] = None; description: Optional[str] = None; hashtags: Optional[List[str]] = None

# ---------- job state helpers ----------
def upd(jid, **kw):
    with LOCK: JOBS[jid].update(kw); JOBS[jid]["rev"] += 1

def stage(jid, i, frac=0.0, detail=""):
    """i = 0-based step index; frac = progress inside this step (0..1)."""
    frac = max(0.0, min(1.0, frac)); done = sum(WEIGHTS[:i]) + WEIGHTS[i]*frac
    upd(jid, step_index=i, step=STEPS[i], detail=detail, progress=round(100*done/sum(WEIGHTS), 1))

def fmt_t(s): s=int(s); return f"{s//60}:{s%60:02d}"

def ai_metadata(text):
    if not os.getenv("ANTHROPIC_API_KEY"): return None
    try:
        import anthropic
        m = anthropic.Anthropic().messages.create(model=os.getenv("AI_MODEL","claude-sonnet-4-6"), max_tokens=800,
            system='You write YouTube Shorts metadata. Reply ONLY JSON: {"title":str,"description":str,"hashtags":[str]}. title: catchy, max 70 chars, same language as the transcript. description: 2-3 engaging sentences summarising what is said plus a call to action to follow for more; no invented facts. hashtags: 6-10 relevant tags starting with #, include #shorts. Base everything strictly on the transcript.',
            messages=[{"role":"user","content":text[:5000]}])
        return json.loads(re.search(r"\{.*\}", m.content[0].text, re.S).group())
    except Exception as e: print("metadata failed:", e); return None

def fallback_meta(hook, text):
    words = re.sub(r"\s+"," ",hook or text).strip().split()
    title = " ".join(words[:9]).rstrip(",.;:") + ("…" if len(words) > 9 else "")
    tags = sorted({w.lower().strip(".,!?\"'") for w in text.split() if len(w) > 6}, key=len, reverse=True)[:5]
    sents, desc = re.split(r"(?<=[.!?])\s+", text.strip()), ""
    for x in sents:
        if len(desc) + len(x) > 280: break
        desc = f"{desc} {x}".strip()
    desc = desc or text[:260].rsplit(" ",1)[0] + "…"
    return {"title": title[:1].upper()+title[1:], "description": f"{desc}\n\nFollow for more clips like this! 🔔", "hashtags": ["#shorts","#viral"]+["#"+re.sub(r"\W","",t) for t in tags if re.sub(r"\W","",t)]}

# ---------- download with live progress ----------
def _js_runtime():
    """YouTube needs a JavaScript runtime to unlock video formats. Use deno if present, else node/bun."""
    import shutil
    path = os.pathsep.join([os.environ.get("PATH",""), "/opt/homebrew/bin", "/usr/local/bin", str(Path.home()/".deno"/"bin")])
    bundled = Path(__file__).parent/"bin"/("deno.exe" if os.name == "nt" else "deno")   # deno kept inside ClipForge (backend/bin)
    if bundled.exists() and os.access(bundled, os.X_OK): return ["--js-runtimes", f"deno:{bundled}"]
    if shutil.which("deno", path=path): return []          # yt-dlp uses deno by default
    for rt in ("node", "bun"):
        exe = shutil.which(rt, path=path)
        if exe: return ["--js-runtimes", f"{rt}:{exe}"]
    return []

def download(jid, url, cookies=None):
    dest = DATA/"downloads"/jid
    cmd = [sys.executable,"-m","yt_dlp","--no-playlist","--newline","--no-simulate","--progress",
           "--print","before_dl:META:%(title)s|||%(thumbnail)s|||%(duration)s",
           "--progress-template","download:PROG|%(info.format_id)s|%(progress._percent_str)s|%(progress._total_bytes_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
           "-f","bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b","--merge-output-format","mp4","--max-filesize",f"{MAX_MB}M",
           "-o",f"{dest}.%(ext)s"] + (["--cookies-from-browser", cookies] if cookies else []) + _js_runtime() + ["--",url]
    stage(jid, 0, 0, "Connecting to video source…")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    fids, last, tail = [], None, []
    for line in proc.stdout:
        line = line.strip(); tail = (tail+[line])[-15:]
        if line.startswith("META:"):
            t, th, du = (line[5:].split("|||")+["","",""])[:3]
            try: du = float(du)
            except ValueError: du = None
            upd(jid, source={"title": t, "thumbnail": th if th.startswith("http") else "", "duration": du})
            stage(jid, 0, 0, "Starting download…")
        elif line.startswith("PROG|"):
            _, fid, pct, size, speed, eta = (line.split("|")+[""]*6)[:6]
            if fid not in fids: fids.append(fid)
            try: p = float(pct.strip().rstrip("%"))
            except ValueError: continue
            part = len(fids); label = "video" if part == 1 else "audio"
            frac = 0.85*p/100 if part == 1 else 0.85 + 0.14*p/100
            key = (part, int(p))
            if key != last:
                last = key; size, speed, eta = size.strip(), speed.strip(), eta.strip()
                stage(jid, 0, frac, f"Downloading {label} · {p:.0f}%" + (f" of {size}" if size and size != "N/A" else "")
                      + (f" · {speed}" if speed and "N/A" not in speed and "Unknown" not in speed else "") + (f" · ETA {eta}" if eta and eta not in ("N/A","Unknown") else ""))
        elif "[Merger]" in line: stage(jid, 0, 0.99, "Merging video and audio…")
    proc.wait()
    f = next((p for p in DATA.joinpath("downloads").glob(jid+".*") if p.suffix in (".mp4",".mkv",".webm",".mov")), None)
    if proc.returncode or not f:
        print("yt-dlp output:\n" + "\n".join(tail))
        blocked = any(("confirm you" in l and "bot" in l) or "Sign in to confirm" in l for l in tail)
        browser = os.getenv("YT_COOKIES_BROWSER", "chrome")
        if blocked and not cookies and browser != "none":   # YouTube bot-check: retry using your own browser's YouTube login (stays on this Mac)
            stage(jid, 0, 0, "YouTube asked for a check — retrying with your browser's YouTube login…")
            try: return download(jid, url, cookies=browser)
            except ValueError as e2: raise ValueError(str(e2) + " (Tip: open youtube.com in " + browser.title() + " and make sure you're signed in, then try again.)")
        err = next((l for l in reversed(tail) if "ERROR" in l), "")
        raise ValueError("Couldn't download this link. " + (re.sub(r"^\[\w+\]\s*\S+:\s*","",err.split("ERROR:",1)[-1].split("; please report")[0].strip())[:180] if err else "It may be private, age-restricted or unsupported."))
    return f

# ---------- main pipeline ----------
def process(jid, src, opts):
    try:
        if isinstance(src, str): src = download(jid, src)
        stage(jid, 0, 1, "Video ready")
        info = P.probe(src); stage(jid, 1, 1, f"{info['width']}×{info['height']} · {fmt_t(info['duration'])} long")
        src_meta = JOBS[jid].get("source") or {}; src_meta.setdefault("duration", info["duration"]); upd(jid, source=src_meta)
        if not info["has_audio"]: raise ValueError("This video has no audio track, so it can't be transcribed.")
        work = DATA/"clips"/jid; work.mkdir(parents=True, exist_ok=True)
        stage(jid, 2, 0.2, "Extracting audio…"); wav = P.extract_audio(src, work/"audio.wav"); stage(jid, 2, 1, "Audio ready")
        stage(jid, 3, 0, "Loading speech model… (can take a minute)")
        segs = P.transcribe(wav, cb=lambda f, t, T: stage(jid, 3, f, f"Transcribing… {fmt_t(t)} / {fmt_t(T)}"), total=info["duration"])
        if not segs: raise ValueError("No speech was detected in this video.")
        upd(jid, transcript_words=sum(len(s["text"].split()) for s in segs), language=getattr(P, "LAST_LANGUAGE", "en") or "en")
        lo, hi = LENGTHS.get(opts["length"], (20,90))
        stage(jid, 4, 0.3, "Scoring hooks, energy and pacing…")
        hls = P.pick_highlights(segs, opts["count"], lo, hi)
        if not hls and opts["length"] != "auto": hls = P.pick_highlights(segs, opts["count"], 15, 90)
        if not hls: raise ValueError("Couldn't find a complete clip. Try a longer video or a different clip length.")
        stage(jid, 4, 1, f"Found {len(hls)} viral moments")
        clips = [dict(index=n, status="queued", score=h["score"], start=h["start"], end=h["end"], hook=h["hook"], reason=h["reason"], topic=h.get("topic","")) for n,h in enumerate(hls)]
        upd(jid, clips=clips)
        fmt = opts["fmt"]; total = len(hls)
        from concurrent.futures import ThreadPoolExecutor
        def make_clip(n, h):
            def cb(f, n=n):
                clips[n]["render"] = int(f*100)
                done = sum(c.get("render", 0) if c["status"] != "ready" else 100 for c in clips) / (100*total)
                stage(jid, 5, done, f"Rendering {total} clips · {int(done*100)}%")
            clips[n]["status"] = "rendering"; upd(jid, clips=clips)
            cs = P.clip_segments(segs, h["start"], h["end"]); out = work/f"clip{n+1}"
            final = P.render(src, h["start"], h["end"], cs, out, fmt, info, burn=opts["captions"], cb=cb, text_color=opts.get("capcolor", "#FFFFFF"), hi_color=opts.get("hicolor", "#FFE500"), cap_size=opts.get("capsize", "m"))
            text = " ".join(s["text"] for s in segs if h["start"] <= s["start"] < h["end"])
            meta = ai_metadata(text) or fallback_meta(h["hook"], text)
            (out/"metadata.json").write_text(json.dumps({**meta, "start":h["start"], "end":h["end"], "score":h["score"], "transcript":text}, indent=2))
            thumb = None
            try: thumb = P.make_thumbnail(final, out/"thumbnail.jpg", meta.get("title",""))
            except Exception as e: print("thumbnail error:", e)
            clips[n].update(status="ready", file=str(final), title=meta.get("title"), description=meta.get("description"), hashtags=meta.get("hashtags",[]),
                            duration=round(h["end"]-h["start"],1), url=f"/api/clip/{jid}/{n}", transcript=text,
                            thumb=f"/api/clip/{jid}/{n}/thumbnail.jpg" if thumb else None, yt={"status": "idle"})
            upd(jid, clips=clips)
            if opts.get("autopost") and YT.status().get("connected"): enqueue_upload(jid, n, opts.get("privacy","private"))   # each clip appears in the UI the moment it's ready

        with ThreadPoolExecutor(max_workers=int(os.getenv("RENDER_WORKERS", "2"))) as ex:   # render clips in parallel
            for fut in [ex.submit(make_clip, n, h) for n, h in enumerate(hls)]: fut.result()
        stage(jid, 6, 1, "Done")
        upd(jid, status="completed", progress=100, step="Completed", detail=f"{total} clips ready")
    except Exception as e:
        print(f"[job {jid}] {type(e).__name__}: {e}")
        upd(jid, status="failed", error=str(e) if isinstance(e, ValueError) else "Processing failed. Check the server log for details.")

# ---------- YouTube upload worker (one upload at a time, rendering keeps going) ----------
UPQ: "queue.Queue" = queue.Queue()
def enqueue_upload(jid, n, privacy):
    c = JOBS[jid]["clips"][n]
    if c.get("yt", {}).get("status") in ("queued", "uploading", "done"): return
    c["yt"] = {"status": "queued", "stage": "Waiting to upload", "pct": 0, "privacy": privacy}; upd(jid)
    UPQ.put((jid, n, privacy))

def _uploader():
    while True:
        jid, n, privacy = UPQ.get(); j = JOBS[jid]; c = j["clips"][n]
        def cb(stage, pct): c["yt"].update(status="uploading", stage=stage, pct=pct); upd(jid)
        try:
            cb("Uploading video", 0); f = Path(c["file"])
            meta = {"title": c.get("title"), "description": c.get("description"), "hashtags": c.get("hashtags", [])}
            r = YT.upload(f, f.parent/"thumbnail.jpg", f.parent/"clip.srt", meta, privacy, j.get("language","en"), j.get("source_url"), cb)
            c["yt"] = {"status": "done", "url": r["url"], "id": r["id"], "warnings": r["warnings"], "privacy": privacy}
        except Exception as e:
            print(f"[upload {jid}/{n}]", type(e).__name__, e); msg = str(e)
            if "quota" in msg.lower(): msg = "YouTube daily upload quota reached — try again tomorrow."
            elif "uploadLimitExceeded" in msg: msg = "Your channel hit YouTube's upload limit for today."
            else: msg = "Upload failed: " + msg[:160]
            c["yt"] = {"status": "error", "error": msg}
        upd(jid); UPQ.task_done()
threading.Thread(target=_uploader, daemon=True).start()

def new_job(src, opts, bg):
    if opts["fmt"] not in P.SIZES: raise HTTPException(400, "Unsupported format")
    opts["count"] = max(1, min(10, int(opts["count"])))
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = dict(id=jid, status="processing", progress=0, step="Queued", step_index=0, detail="Starting…", steps=STEPS, clips=[], source={}, created=time.time(), rev=0, opts=opts)
    if isinstance(src, Path): JOBS[jid]["source"] = {"title": src.name}
    else: JOBS[jid]["source_url"] = src
    threading.Thread(target=process, args=(jid, src, opts), daemon=True).start()
    return {"job_id": jid}

# ---------- API ----------
@app.post("/api/video/from-url")
def from_url(body: UrlIn, bg: BackgroundTasks):
    url = body.url.strip()
    if not re.match(r"^https?://[^\s]+$", url): raise HTTPException(400, "Enter a valid link starting with http:// or https://")
    opts = dict(fmt=body.fmt, length=body.length, count=body.count, captions=body.captions, autopost=body.autopost, privacy=body.privacy, capcolor=body.capcolor, hicolor=body.hicolor, capsize=body.capsize)
    with LOCK:   # same link already processing (double click / second tab) -> reuse that job instead of starting a duplicate
        dup = next((j["id"] for j in JOBS.values() if j.get("source_url") == url and j["status"] == "processing" and j.get("opts") == opts), None)
    if dup: return {"job_id": dup}
    return new_job(url, dict(opts), bg)   # returns instantly; download runs in the job

@app.post("/api/video/upload")
async def upload(bg: BackgroundTasks, file: UploadFile = File(...), fmt: str = Form("9:16"), length: str = Form("auto"), count: int = Form(5), captions: bool = Form(True), autopost: bool = Form(False), privacy: str = Form("private"), capcolor: str = Form("#FFFFFF"), hicolor: str = Form("#FFE500"), capsize: str = Form("m")):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in {".mp4",".mov",".avi",".mkv",".webm"}: raise HTTPException(400, "Unsupported file type. Use MP4, MOV, AVI, MKV or WEBM.")
    dest = DATA/"uploads"/f"{uuid.uuid4().hex}{ext}"; size = 0
    with open(dest,"wb") as f:
        while chunk := await file.read(1<<20):
            size += len(chunk)
            if size > MAX_MB<<20: f.close(); dest.unlink(); raise HTTPException(413, f"File exceeds {MAX_MB} MB")
            f.write(chunk)
    try: P.probe(dest)
    except Exception: dest.unlink(); raise HTTPException(400, "This file isn't a readable video.")
    r = new_job(dest, dict(fmt=fmt, length=length, count=count, captions=captions, autopost=autopost, privacy=privacy, capcolor=capcolor, hicolor=hicolor, capsize=capsize), bg)
    JOBS[r["job_id"]]["source"] = {"title": file.filename}
    return r

def _job(jid):
    if jid not in JOBS: raise HTTPException(404, "Unknown job")
    return JOBS[jid]

def public(j):
    out = dict(j)
    out["clips"] = [{k: v for k, v in c.items() if k != "file"} for c in j.get("clips", [])]
    return out

@app.get("/api/job/{jid}")
def job(jid: str): return public(_job(jid))

@app.get("/api/job/{jid}/progress")
async def progress(jid: str):
    _job(jid)
    async def gen():
        last = -1
        while True:
            j = JOBS[jid]
            if j["rev"] != last: last = j["rev"]; yield f"data: {json.dumps(public(j))}\n\n"
            if j["status"] != "processing": break
            await asyncio.sleep(0.3)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

def _clip(jid, n):
    if jid in JOBS:
        cl = JOBS[jid].get("clips", [])
        if 0 <= n < len(cl) and cl[n].get("status") == "ready" and Path(cl[n]["file"]).exists(): return cl[n], Path(cl[n]["file"])
    # job not in memory (server was restarted) -> read the finished clip straight from disk
    if not re.fullmatch(r"[0-9a-f]{12}", jid) or n < 0: raise HTTPException(404, "Clip not found")
    d = DATA/"clips"/jid/f"clip{n+1}"; f = next(iter(sorted(d.glob("clip_*.mp4"))), None) if d.exists() else None
    if not f: raise HTTPException(404, "Clip not found — it may have been deleted. Please process the video again.")
    meta = {}
    try: meta = json.loads((d/"metadata.json").read_text())
    except Exception: pass
    return {**meta, "index": n, "file": str(f), "status": "ready"}, f

# ---------- save to Desktop/Short YouTube (one copy per clip & quality, never duplicated) ----------
SAVE_DIR = Path(os.getenv("SAVE_DIR", str(Path.home()/"Desktop"/"Short YouTube")))
QUALITY = {"1080": "Full HD 1080p", "720": "HD 720p", "480": "SD 480p"}
_save_locks: dict = {}; _save_guard = threading.Lock()

def _safe(name):
    name = re.sub(r'[\\/:*?"<>|#\n\r\t…]+', " ", name or "").strip(" .")
    return re.sub(r"\s+", " ", name)[:70] or "Short"

def save_clip(jid, n, q="1080"):
    if q not in QUALITY: raise HTTPException(400, "Unknown quality")
    c, f = _clip(jid, n)
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    dest = SAVE_DIR/f"{_safe(c.get('title') or f'Clip {n+1}')} [{jid[:6]}-{n+1}] {q}p.mp4"
    with _save_guard: lock = _save_locks.setdefault(str(dest), threading.Lock())
    with lock:
        if dest.exists() and dest.stat().st_size > 0: return {"path": str(dest), "name": dest.name, "existed": True}
        tmp = dest.with_suffix(".part.mp4")
        if q == "1080":   # master is already 1080p: hard-link = same file on disk, zero extra storage
            try: os.link(f, dest)
            except OSError: import shutil; shutil.copy2(f, tmp); tmp.replace(dest)
        else:
            w, h = P.probe(f)["width"], P.probe(f)["height"]; short = int(q)
            sc = f"scale={short}:-2" if w <= h else f"scale=-2:{short}"
            vc = ["-c:v","h264_videotoolbox","-b:v","6M" if q == "720" else "3M","-allow_sw","1"] if sys.platform == "darwin" else ["-c:v","libx264","-preset","veryfast","-crf","20"]
            r = subprocess.run(["ffmpeg","-y","-hide_banner","-loglevel","error","-i",str(f),"-vf",sc,*vc,"-pix_fmt","yuv420p","-c:a","copy","-movflags","+faststart",str(tmp)], capture_output=True, text=True)
            if r.returncode and sys.platform == "darwin":
                r = subprocess.run(["ffmpeg","-y","-hide_banner","-loglevel","error","-i",str(f),"-vf",sc,"-c:v","libx264","-preset","veryfast","-crf","20","-pix_fmt","yuv420p","-c:a","copy","-movflags","+faststart",str(tmp)], capture_output=True, text=True)
            if r.returncode: tmp.unlink(missing_ok=True); raise HTTPException(500, "Could not convert the clip: " + r.stderr[-200:])
            tmp.replace(dest)
        return {"path": str(dest), "name": dest.name, "existed": False}

@app.post("/api/clip/{jid}/{n}/save")
def save_one(jid: str, n: int, q: str = "1080"): return {**save_clip(jid, n, q), "folder": str(SAVE_DIR)}

@app.post("/api/save-all/{jid}")
def save_all(jid: str, q: str = "1080"):
    ready = [c["index"] for c in JOBS[jid]["clips"] if c.get("status") == "ready"] if jid in JOBS else \
            [int(p.name[4:])-1 for p in sorted((DATA/"clips"/jid).glob("clip*")) if p.name[4:].isdigit() and any(p.glob("clip_*.mp4"))]
    if not ready: raise HTTPException(409, "No clips ready yet")
    res = [save_clip(jid, n, q) for n in ready]
    return {"saved": sum(not r["existed"] for r in res), "already": sum(r["existed"] for r in res), "folder": str(SAVE_DIR)}

@app.post("/api/open-folder")
def open_folder():
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin": subprocess.Popen(["open", str(SAVE_DIR)])
    elif os.name == "nt": os.startfile(str(SAVE_DIR))
    else: subprocess.Popen(["xdg-open", str(SAVE_DIR)])
    return {"folder": str(SAVE_DIR)}

@app.api_route("/api/clip/{jid}/{n}", methods=["GET", "HEAD"])
def clip_video(jid: str, n: int, dl: int = 0):
    c, f = _clip(jid, n)
    return FileResponse(f, media_type="video/mp4", filename=f"clipforge_{jid}_clip{n+1}.mp4" if dl else None)

@app.get("/api/clip/{jid}/{n}/{name}")
def clip_file(jid: str, n: int, name: str):
    if name not in {"clip.srt","clip.vtt","metadata.json","thumbnail.jpg"}: raise HTTPException(404)
    c, f = _clip(jid, n); return FileResponse(f.parent/name, filename=f"clip{n+1}_{name}")

@app.get("/api/download/{jid}")
def download_all(jid: str):
    j = _job(jid); ready = [c for c in j.get("clips", []) if c.get("status") == "ready"]
    if not ready: raise HTTPException(409, "No clips ready yet")
    z = DATA/"exports"/f"{jid}.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for c in ready:
            f = Path(c["file"]); p = f"clip{c['index']+1}"
            zf.write(f, f"{p}/{f.name}")
            for n in ("clip.srt","clip.vtt","metadata.json"):
                if (f.parent/n).exists(): zf.write(f.parent/n, f"{p}/{n}")
    return FileResponse(z, filename=f"clipforge_{jid}.zip")

# ---------- YouTube ----------
PRIV = {"private","unlisted","public"}
@app.get("/api/youtube/status")
def yt_status(): return YT.status()

@app.get("/api/youtube/connect")
def yt_connect():
    if not YT.configured(): raise HTTPException(400, "Add client_secret.json to the backend folder first (see README).")
    return RedirectResponse(YT.auth_url())

@app.get("/api/youtube/callback")
def yt_callback(request: Request, state: str = "", error: str = ""):
    if error: return RedirectResponse("/?yt=denied")
    try: YT.finish(state, str(request.url))
    except Exception as e: print("oauth error:", e); return RedirectResponse("/?yt=error")
    return RedirectResponse("/?yt=connected")

@app.post("/api/youtube/disconnect")
def yt_disconnect(): YT.disconnect(); return {"ok": True}

@app.post("/api/clip/{jid}/{n}/publish")
def publish(jid: str, n: int, body: PublishIn):
    _clip(jid, n)
    if body.privacy not in PRIV: raise HTTPException(400, "Bad privacy value")
    if not YT.status().get("connected"): raise HTTPException(400, "Connect your YouTube channel first.")
    enqueue_upload(jid, n, body.privacy); return {"ok": True}

@app.post("/api/clip/{jid}/{n}/meta")
def edit_meta(jid: str, n: int, body: MetaIn):
    c, f = _clip(jid, n)
    for k in ("title","description","hashtags"):
        v = getattr(body, k)
        if v is not None: c[k] = v
    if body.title is not None:   # keep the thumbnail text in sync with the edited title
        try: P.make_thumbnail(f, f.parent/"thumbnail.jpg", body.title); c["thumb"] = f"/api/clip/{jid}/{n}/thumbnail.jpg?v={int(time.time())}"
        except Exception as e: print("thumbnail error:", e)
    upd(jid); return {"ok": True}

# ---------- web UI ----------
app.mount("/", StaticFiles(directory=BASE/"static", html=True), name="ui")
