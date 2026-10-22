"""ClipForge core pipeline: probe -> transcribe -> pick highlight -> reframe -> subtitles -> render -> validate."""
import json, os, re, subprocess, shutil, sys
from pathlib import Path
import cv2, numpy as np

LAST_LANGUAGE = "en"
SIZES = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)  # list args, never shell=True
    if r.returncode: raise RuntimeError(f"{cmd[0]} failed: {r.stderr[-800:]}")
    return r.stdout

def probe(path):
    d = json.loads(run(["ffprobe","-v","error","-show_format","-show_streams","-of","json",str(path)]))
    v = next((s for s in d["streams"] if s["codec_type"]=="video"), None)
    a = next((s for s in d["streams"] if s["codec_type"]=="audio"), None)
    if not v: raise ValueError("No video stream found")
    n,dn = v["r_frame_rate"].split("/")
    return dict(duration=float(d["format"]["duration"]), width=v["width"], height=v["height"],
        fps=round(int(n)/int(dn),3), codec=v["codec_name"], bitrate=int(d["format"].get("bit_rate",0)),
        size=int(d["format"]["size"]), has_audio=a is not None,
        audio_rate=int(a["sample_rate"]) if a else None, audio_channels=a["channels"] if a else None)

def extract_audio(video, out):
    run(["ffmpeg","-y","-i",str(video),"-vn","-ac","1","-ar","16000",str(out)]); return out

_WHISPER = {}
def _whisper():
    """Load the speech model once and reuse it for every job (loading takes a while)."""
    name = os.getenv("WHISPER_MODEL","base")
    if name not in _WHISPER:
        from faster_whisper import WhisperModel
        _WHISPER[name] = WhisperModel(name, compute_type="int8", cpu_threads=os.cpu_count() or 4)
    return _WHISPER[name]

def _mlx_ok():
    import platform, sys as _s
    if os.getenv("TRANSCRIBER", "auto") == "faster": return False
    if _s.platform != "darwin" or platform.machine() != "arm64": return False
    try: import mlx_whisper  # noqa
    except Exception as e: print("mlx-whisper unavailable, using CPU:", e, flush=True); return False
    return True

def _transcribe_mlx(audio, cb, total):
    """Apple-Silicon GPU transcription (10-20x faster than CPU). Progress is estimated while it runs."""
    import mlx_whisper, threading, time
    model = os.getenv("MLX_MODEL", "mlx-community/whisper-large-v3-turbo")
    done = threading.Event(); t0 = time.time(); est = max(20.0, (total or 300) / 12)
    def tick():
        while not done.wait(1):
            if cb and total: f = min(0.95, (time.time()-t0)/est); cb(f, f*total, total)
    threading.Thread(target=tick, daemon=True).start()
    try: r = mlx_whisper.transcribe(str(audio), path_or_hf_repo=model, word_timestamps=True, condition_on_previous_text=False)
    finally: done.set()
    global LAST_LANGUAGE; LAST_LANGUAGE = r.get("language") or "en"
    out = []
    for s in r.get("segments", []):
        t = s.get("text", "").strip()
        if not t: continue
        ws = [dict(start=w["start"], end=w["end"], w=w["word"].strip()) for w in s.get("words", []) if w.get("word", "").strip()]
        out.append(dict(start=s["start"], end=s["end"], text=t, words=ws))
    if cb and total: cb(1.0, total, total)
    return out

def transcribe(audio, cb=None, total=None):
    """WHISPER_PROVIDER=openai (API) or local (faster-whisper). Returns [{start,end,text}]."""
    prov = os.getenv("WHISPER_PROVIDER","local")
    if prov == "openai":
        from openai import OpenAI
        r = OpenAI().audio.transcriptions.create(model="whisper-1", file=open(audio,"rb"),
            response_format="verbose_json", timestamp_granularities=["segment"])
        return [dict(start=s.start,end=s.end,text=s.text.strip()) for s in r.segments]
    if _mlx_ok(): return _transcribe_mlx(audio, cb, total)
    segs,info = _whisper().transcribe(str(audio), vad_filter=True, word_timestamps=True)
    global LAST_LANGUAGE; LAST_LANGUAGE = getattr(info, "language", None) or "en"
    total = total or getattr(info, "duration", None) or 0
    out = []
    for s in segs:  # generator: report live progress as each segment is decoded
        ws = [dict(start=w.start, end=w.end, w=w.word.strip()) for w in (getattr(s, "words", None) or []) if w.word.strip()]
        out.append(dict(start=s.start,end=s.end,text=s.text.strip(),words=ws))
        if cb and total: cb(min(1.0, s.end/total), s.end, total)
    return out

# ---------- highlight engine ----------
HOOKS = re.compile(r"\b(secret|mistake|never|always|biggest|worst|best|why|how|nobody|truth|crazy|insane|unbelievable|shocking|surprising|important)\b", re.I)

def candidates(segs, lo=20, hi=90):
    lo, hi = float(lo), float(hi)
    out = []
    for i in range(len(segs)):
        for j in range(i, len(segs)):
            d = segs[j]["end"] - segs[i]["start"]
            if d > hi: break
            if d >= lo and re.search(r"[.!?]\s*$", segs[j]["text"]):  # end on sentence boundary
                out.append((i, j))
    return out

def heuristic_score(segs, i, j):
    t = " ".join(s["text"] for s in segs[i:j+1]); first = segs[i]["text"]; dur = segs[j]["end"]-segs[i]["start"]
    words = len(t.split())
    hook = min(1, len(HOOKS.findall(first))*0.5 + ("?" in first)*0.5)
    energy = min(1, (t.count("!")+t.count("?"))/4 + len(HOOKS.findall(t))/6)
    density = min(1, words/dur/3.0)  # ~3 wps = dense speech
    gaps = sum(max(0, segs[k+1]["start"]-segs[k]["end"]) for k in range(i, j))
    pace = 1 - min(1, gaps/dur*4)
    return round(100*(0.3*hook + 0.25*energy + 0.25*density + 0.2*pace))

def llm_rank(segs, cands, top):
    """Optional: rerank top candidates with Claude (structured JSON). Needs ANTHROPIC_API_KEY."""
    import anthropic
    items = [{"id":n,"start":segs[i]["start"],"end":segs[j]["end"],
              "text":" ".join(s["text"] for s in segs[i:j+1])[:1500]} for n,(i,j,_) in enumerate(top)]
    msg = anthropic.Anthropic().messages.create(model=os.getenv("AI_MODEL","claude-sonnet-4-6"), max_tokens=1500,
        system="You are a short-form video editor. Score each candidate 0-100 for hook, information value, emotional impact, context completeness and ending quality. Reply with ONLY JSON: [{\"id\":int,\"score\":int,\"reason\":str,\"hook\":str,\"topic\":str}]",
        messages=[{"role":"user","content":json.dumps(items)}])
    return {r["id"]: r for r in json.loads(re.search(r"\[.*\]", msg.content[0].text, re.S).group())}

def pick_highlights(segs, n=3, lo=20, hi=90):
    cs = sorted(((i,j,heuristic_score(segs,i,j)) for i,j in candidates(segs, lo, hi)), key=lambda c:-c[2])
    chosen = []
    for c in cs:  # non-overlapping
        if all(segs[c[1]]["end"]<=segs[o[0]]["start"] or segs[c[0]]["start"]>=segs[o[1]]["end"] for o in chosen): chosen.append(c)
        if len(chosen) >= max(n*2,6): break
    llm = {}
    if os.getenv("ANTHROPIC_API_KEY") and os.getenv("AI_PROVIDER","anthropic")=="anthropic":
        try: llm = llm_rank(segs, chosen, chosen)
        except Exception as e: print("LLM rerank failed, using heuristic:", e)
    res = []
    for k,(i,j,h) in enumerate(chosen):
        r = llm.get(k, {})
        res.append(dict(start=segs[i]["start"], end=segs[j]["end"], score=r.get("score",h),
            reason=r.get("reason","Heuristic: hook words, energy, speech density, pacing"), hook=r.get("hook",segs[i]["text"]), topic=r.get("topic","")))
    return sorted(res, key=lambda r:-r["score"])[:n]

# ---------- subtitles ----------
def _ts(t, sep=","): 
    h,m,s = int(t//3600), int(t%3600//60), t%60; return f"{h:02}:{m:02}:{int(s):02}{sep}{int(round((s%1)*1000)):03}"
def clip_segments(segs, a, b):
    out = []
    for s in segs:
        if s["end"] <= a or s["start"] >= b: continue
        ws = [dict(start=max(0, w["start"]-a), end=min(b, w["end"])-a, w=w["w"]) for w in s.get("words", []) if w["end"] > a and w["start"] < b]
        out.append(dict(start=max(0,s["start"]-a), end=min(b,s["end"])-a, text=s["text"], words=ws))
    return out
def wrap(text, width=28):
    lines, cur = [], ""
    for w in text.split():
        if len(cur)+len(w)+1 > width and cur: lines.append(cur); cur = w
        else: cur = f"{cur} {w}".strip()
    return lines+[cur] if cur else lines
def write_srt(cs, p): Path(p).write_text("".join(f"{i}\n{_ts(s['start'])} --> {_ts(s['end'])}\n{chr(10).join(wrap(s['text']))}\n\n" for i,s in enumerate(cs,1)))
def write_vtt(cs, p): Path(p).write_text("WEBVTT\n\n"+"".join(f"{_ts(s['start'],'.')} --> {_ts(s['end'],'.')}\n{chr(10).join(wrap(s['text']))}\n\n" for s in cs))
FONTS_DIR = Path(__file__).parent/"fonts"
CAPTION_FONT = os.getenv("CAPTION_FONT", "Anton")

def _words(seg):
    """Word timings for a segment; spreads words evenly if the transcriber gave none."""
    if seg.get("words"): return seg["words"]
    ws = seg["text"].split(); n = len(ws) or 1; d = (seg["end"]-seg["start"])/n
    return [dict(start=seg["start"]+i*d, end=seg["start"]+(i+1)*d, w=w) for i,w in enumerate(ws)]

def _chunks(words, max_words=3, max_chars=18):
    """Group words into short punchy captions (1-3 words), breaking at punctuation."""
    out, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= max_words or len(" ".join(x["w"] for x in cur)) >= max_chars or w["w"][-1:] in ".,!?;:":
            out.append(cur); cur = []
    return out + [cur] if cur else out

def _ass_color(hexstr, default):
    """'#RRGGBB' -> ASS '&H00BBGGRR&'."""
    h = (hexstr or "").lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h): h = default
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}&".upper()

CAP_SIZES = {"s": 0.75, "m": 1.0, "l": 1.22, "xl": 1.45}

def write_ass(cs, p, size, caps=True, text_color="#FFFFFF", hi_color="#FFE500", cap_size="m"):
    """Viral Shorts style: big condensed ALL-CAPS (Anton), white with thick black outline + drop shadow,
    1-3 words on screen, the word being spoken highlighted in yellow with a quick pop."""
    w,h = size; vert = h > w
    k = CAP_SIZES.get(cap_size, 1.0); fs = int((w*0.15 if vert else h*0.11)*k); mv = int(h*0.30 if vert else h*0.12)
    ol = max(4, fs//14); sh = max(2, fs//25)
    def ts(t): return f"{int(t//3600)}:{int(t%3600//60):02}:{t%60:05.2f}"
    def clean(x): return x.replace("{","(").replace("}",")").replace("\\","/")
    ev = []
    for seg in cs:
        for ch in _chunks(_words(seg), max_words=2 if k > 1.3 else 3, max_chars=int(18/k)):
            texts = [clean(x["w"].upper() if caps else x["w"]) for x in ch]
            for i, wd in enumerate(ch):
                st = wd["start"]; en = ch[i+1]["start"] if i+1 < len(ch) else max(wd["end"], st+0.25)
                if en <= st: continue
                hc = _ass_color(hi_color, "FFE500"); parts = [(r"{\c" + hc + r"\t(0,90,\fscx112\fscy112)\t(90,160,\fscx100\fscy100)}" + t + r"{\r}") if j == i else t for j, t in enumerate(texts)]
                ev.append(f"Dialogue: 0,{ts(st)},{ts(en)},Viral,,0,0,0,,{' '.join(parts)}\n")
    Path(p).write_text(f"[Script Info]\nScriptType: v4.00+\nPlayResX: {w}\nPlayResY: {h}\nWrapStyle: 0\n\n[V4+ Styles]\n"
      "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
      f"Style: Viral,{CAPTION_FONT},{fs},{_ass_color(text_color, 'FFFFFF')[:-1]},&H00FFFFFF,&H00000000,&HA0000000,0,0,0,0,100,100,1,0,1,{ol},{sh},2,70,70,{mv},1\n\n"
      "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n" + "".join(ev))

# ---------- smart reframe ----------
def crop_track(video, a, b, out_ar, src_w, src_h):
    """Face-tracked horizontal center per second, smoothed. Falls back to frame center."""
    if not hasattr(cv2, "CascadeClassifier"): return None   # OpenCV build without face detection -> center crop
    cap = cv2.VideoCapture(str(video)); det = cv2.CascadeClassifier(cv2.data.haarcascades+"haarcascade_frontalface_default.xml")
    xs, t = [], a
    while t < b:
        cap.set(cv2.CAP_PROP_POS_MSEC, t*1000); ok, f = cap.read()
        cx = None
        if ok:
            sc = 640/f.shape[1]; g = cv2.cvtColor(cv2.resize(f,None,fx=sc,fy=sc), cv2.COLOR_BGR2GRAY)
            fs = det.detectMultiScale(g, 1.1, 5, minSize=(30,30))
            if len(fs): x,y,w,h = max(fs, key=lambda r:r[2]*r[3]); cx = (x+w/2)/sc
        xs.append(cx if cx is not None else (xs[-1] if xs else src_w/2)); t += 0.5
    cap.release()
    xs = np.array(xs); k = 5; pad = np.pad(xs,(k//2,k//2),mode="edge"); return np.convolve(pad, np.ones(k)/k, mode="valid")

def render(video, a, b, cs, out_dir, fmt="9:16", info=None, burn=True, cb=None, text_color="#FFFFFF", hi_color="#FFE500", cap_size="m"):
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    info = info or probe(video); W,H = SIZES[fmt]; ar = W/H; sw,sh = info["width"],info["height"]
    cw,ch = (min(sw, int(sh*ar)), sh) if sw/sh > ar else (sw, min(sh, int(sw/ar)))
    cw,ch = cw//2*2, ch//2*2
    track = crop_track(video, a, b, ar, sw, sh) if cw < sw else None
    write_srt(cs, out_dir/"clip.srt"); write_vtt(cs, out_dir/"clip.vtt"); write_ass(cs, out_dir/"clip.ass", (W,H), text_color=text_color, hi_color=hi_color, cap_size=cap_size)
    fps = info["fps"]
    vf = f"ass={out_dir/'clip.ass'}:fontsdir={FONTS_DIR}" if burn and cs else "null"
    final = out_dir/f"clip_{fmt.replace(':','x')}.mp4"
    def encode(vcodec):
        cap = cv2.VideoCapture(str(video)); cap.set(cv2.CAP_PROP_POS_MSEC, a*1000)
        errf = open(out_dir/"ffmpeg.log", "w+b")   # a file, not a pipe: a full stderr pipe makes ffmpeg freeze mid-render
        enc = subprocess.Popen(["ffmpeg","-y","-hide_banner","-loglevel","error","-f","rawvideo","-pix_fmt","bgr24","-s",f"{W}x{H}","-r",str(fps),"-i","-",
            "-ss",str(a),"-t",str(b-a),"-i",str(video),"-map","0:v","-map","1:a?","-vf",vf, *vcodec,
            "-pix_fmt","yuv420p","-c:a","aac","-b:a","192k","-ar","48000","-movflags","+faststart","-shortest",str(final)], stdin=subprocess.PIPE, stderr=errf)
        n = int((b-a)*fps)
        try:
            for i in range(n):
                ok, f = cap.read()
                if not ok: break
                if track is not None:
                    cx = np.interp(i/fps, np.arange(len(track))*0.5, track); x = int(min(max(cx-cw/2,0), sw-cw)); f = f[:ch, x:x+cw]
                else: f = f[(sh-ch)//2:(sh-ch)//2+ch, :cw]
                enc.stdin.write(cv2.resize(f,(W,H),interpolation=cv2.INTER_AREA if f.shape[0] > H else cv2.INTER_LINEAR).tobytes())
                if cb and i % 15 == 0: cb(i/n)
            enc.stdin.close()
        except BrokenPipeError: pass
        finally: cap.release()
        enc.wait(); errf.seek(0); err = errf.read().decode(errors="replace"); errf.close()
        return enc.returncode, err
    x264 = ["-c:v","libx264","-preset",os.getenv("ENCODE_PRESET","veryfast"),"-crf",os.getenv("ENCODE_CRF","18"),"-profile:v","high"]
    hw = ["-c:v","h264_videotoolbox","-b:v","12M","-maxrate","16M","-bufsize","24M","-profile:v","high","-allow_sw","1"]
    use_hw = sys.platform == "darwin" and os.getenv("HW_ENCODE","1") == "1"
    rc, err = encode(hw if use_hw else x264)
    if rc and use_hw:   # hardware encoder missing/failed -> software
        print("videotoolbox failed, using libx264:", err[-300:], flush=True); rc, err = encode(x264)
    if rc: raise RuntimeError("ffmpeg encode failed: "+err[-600:])
    return final

def validate(path, fmt, expect_dur):
    i = probe(path); W,H = SIZES[fmt]; probs = []
    if (i["width"],i["height"]) != (W,H): probs.append(f"resolution {i['width']}x{i['height']}")
    if not i["has_audio"]: probs.append("no audio")
    if abs(i["duration"]-expect_dur) > 1.0: probs.append(f"duration {i['duration']:.1f}s vs {expect_dur:.1f}s")
    run(["ffmpeg","-v","error","-i",str(path),"-f","null","-"])  # full decode check
    return probs, i


# ---------- thumbnail ----------
FONTS = ["/System/Library/Fonts/Supplemental/Arial Black.ttf", "/System/Library/Fonts/Supplemental/Impact.ttf",
         "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
TAMIL_FONTS = ["/System/Library/Fonts/Supplemental/Tamil Sangam MN.ttc", "/System/Library/Fonts/Supplemental/Tamil MN.ttc",
               "/usr/share/fonts/truetype/noto/NotoSansTamil-Bold.ttf"]

def _font(text, size):
    from PIL import ImageFont
    cands = TAMIL_FONTS + FONTS if re.search(r"[\u0B80-\u0BFF]", text) else FONTS
    for f in cands:
        if Path(f).exists():
            try: return ImageFont.truetype(f, size)
            except Exception: pass
    return ImageFont.load_default()

def make_thumbnail(video, out, title, n_samples=8):
    """Pick the sharpest frame (prefers frames with a face) from the rendered clip and add a bold title. Returns path."""
    from PIL import Image, ImageDraw, ImageFilter
    cap = cv2.VideoCapture(str(video)); total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    det = cv2.CascadeClassifier(cv2.data.haarcascades+"haarcascade_frontalface_default.xml") if hasattr(cv2, "CascadeClassifier") else None
    best, best_score = None, -1
    for k in range(n_samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total*(0.1+0.8*k/max(1,n_samples-1)))); ok, f = cap.read()
        if not ok: continue
        g = cv2.cvtColor(cv2.resize(f, (360, int(360*f.shape[0]/f.shape[1]))), cv2.COLOR_BGR2GRAY)
        score = cv2.Laplacian(g, cv2.CV_64F).var()
        if det is not None and len(det.detectMultiScale(g, 1.1, 5, minSize=(30,30))): score *= 3
        if score > best_score: best, best_score = f, score
    cap.release()
    if best is None: return None
    img = Image.fromarray(cv2.cvtColor(best, cv2.COLOR_BGR2RGB))
    img.thumbnail((1080, 1920)); W, H = img.size
    shade = Image.new("L", (1, H)); [shade.putpixel((0, y), int(200*max(0, 1-y/(H*0.45)))) for y in range(H)]
    img.paste(Image.new("RGB", (W, H), (0, 0, 0)), (0, 0), shade.resize((W, H)))
    d = ImageDraw.Draw(img); text = (title or "").replace("#Shorts","").strip().upper()
    fs = int(W*0.095); font = _font(text, fs)
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = f"{cur} {w}".strip()
        if d.textlength(t, font=font) > W*0.86 and cur: lines.append(cur); cur = w
        else: cur = t
    if cur: lines.append(cur)
    lines = lines[:4]; y = int(H*0.07)
    for i, ln in enumerate(lines):
        tw = d.textlength(ln, font=font); x = (W-tw)/2
        d.text((x, y), ln, font=font, fill=(255, 221, 0) if i == 0 else (255, 255, 255), stroke_width=max(3, fs//12), stroke_fill=(0, 0, 0))
        y += int(fs*1.15)
    img.save(out, "JPEG", quality=88)
    while Path(out).stat().st_size > 2_000_000:   # YouTube limit 2 MB
        img = img.resize((int(img.width*0.85), int(img.height*0.85))); img.save(out, "JPEG", quality=82)
    return out
