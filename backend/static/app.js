const $ = s => document.querySelector(s);
const opts = { fmt: "9:16", length: "auto", count: 5, captions: true, autopost: false, privacy: "private", capcolor: "#FFFFFF", hicolor: "#FFE500", capsize: "m" };
let yt = { configured: false, connected: false }, curJob = null;
let mode = null, file = null, es = null, timer = null, t0 = 0, busy = false;
const URL_RE = /^https?:\/\/\S+\.\S+/i;

/* ---------- options ---------- */
document.querySelectorAll(".seg").forEach(seg => seg.addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  seg.querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
  const k = seg.dataset.k, v = b.dataset.v;
  opts[k] = k === "count" ? +v : (k === "captions" || k === "autopost") ? v === "true" : v;
  if (k === "capsize") capPreview();
  if (k === "capcolor" || k === "hicolor") { const p = seg.querySelector("input[type=color]"); if (p) p.value = v; capPreview(); }
  if (k === "captions") document.querySelectorAll(".cap-opt").forEach(o => o.classList.toggle("dim", !opts.captions));
  if (k === "autopost" && opts.autopost && !yt.connected) { needYouTube(); setSeg("autopost", "false"); opts.autopost = false; }
}));

/* ---------- caption colours ---------- */
document.querySelectorAll('.swatches input[type=color]').forEach(inp => {
  inp.addEventListener("click", e => e.stopPropagation());
  inp.addEventListener("input", () => {
    const k = inp.dataset.k; opts[k] = inp.value.toUpperCase();
    inp.closest(".seg").querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.v.toUpperCase() === opts[k]));
    capPreview();
  });
});
function capPreview() {
  const p = $("#capPreview"); if (!p) return;
  p.style.setProperty("--tc", opts.capcolor); p.style.fontSize = ({ s: 15, m: 20, l: 25, xl: 30 })[opts.capsize] + "px"; p.style.setProperty("--hc", opts.hicolor);
}
capPreview();

/* ---------- one input only: link OR file ---------- */
function setMode(m) {
  mode = m;
  $("#urlRow").hidden = m === "file";
  $("#fileRow").hidden = m !== "file";
  $("#orRow").classList.toggle("off", m !== null);     // hide the other option once one is chosen
  $("#clearUrl").hidden = m !== "url";
  $("#go").disabled = !(m === "file" || (m === "url" && URL_RE.test($("#url").value.trim()))) || busy;
  showErr("");
}
const urlIn = $("#url");
urlIn.addEventListener("input", () => setMode(urlIn.value.trim() ? "url" : null));
urlIn.addEventListener("keydown", e => { if (e.key === "Enter" && !$("#go").disabled) start(); });
urlIn.addEventListener("paste", () => setTimeout(() => {           // paste a link -> starts automatically
  setMode(urlIn.value.trim() ? "url" : null);
  if (URL_RE.test(urlIn.value.trim()) && !busy) start();
}, 50));
$("#clearUrl").onclick = () => { urlIn.value = ""; setMode(null); urlIn.focus(); };
$("#file").addEventListener("change", e => pickFile(e.target.files[0]));
$("#clearFile").onclick = () => { file = null; $("#file").value = ""; setMode(null); };
function pickFile(f) {
  if (!f) return;
  if (!/\.(mp4|mov|avi|mkv|webm)$/i.test(f.name)) return showErr("Unsupported file type. Use MP4, MOV, AVI, MKV or WEBM.");
  file = f; urlIn.value = "";
  $("#fname").textContent = f.name; $("#fsize").textContent = (f.size / 1048576).toFixed(1) + " MB";
  setMode("file");
}
const box = $("#box");
["dragenter","dragover"].forEach(ev => box.addEventListener(ev, e => { e.preventDefault(); box.classList.add("drag"); }));
["dragleave","drop"].forEach(ev => box.addEventListener(ev, e => { e.preventDefault(); box.classList.remove("drag"); }));
box.addEventListener("drop", e => pickFile(e.dataTransfer.files[0]));
$("#go").onclick = start;
function showErr(m) { $("#err").hidden = !m; $("#err").textContent = m || ""; }

/* ---------- start a job ---------- */
async function start() {
  if (busy) return;
  busy = true; $("#go").disabled = true; $("#go").textContent = "Working…";
  resetWork(); $("#work").hidden = false; $("#work").scrollIntoView({ behavior: "smooth" });
  try {
    let id;
    if (mode === "file") {
      $("#srcTitle").textContent = file.name; setStage("Uploading video…", 0);
      id = await uploadFile(file);
    } else {
      const url = urlIn.value.trim();
      $("#srcTitle").textContent = url; setStage("Sending link…", 0);
      const r = await fetch("/api/video/from-url", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url, ...opts }) });
      const j = await r.json(); if (!r.ok) throw new Error(j.detail || "Request failed");
      id = j.job_id;
    }
    try { localStorage.setItem("cf_job", id); } catch {}
    listen(id);
  } catch (e) { fail(e.message); }
}
function uploadFile(f) {
  return new Promise((ok, no) => {
    const fd = new FormData(); fd.append("file", f);
    Object.entries(opts).forEach(([k, v]) => fd.append(k, v));
    const x = new XMLHttpRequest(); x.open("POST", "/api/video/upload");
    x.upload.onprogress = e => e.lengthComputable && setStage(`Uploading · ${Math.round(e.loaded / e.total * 100)}% of ${(e.total / 1048576).toFixed(0)} MB`, e.loaded / e.total * 15);
    x.onload = () => { let j = {}; try { j = JSON.parse(x.responseText); } catch {} x.status < 300 ? ok(j.job_id) : no(new Error(j.detail || "Upload failed")); };
    x.onerror = () => no(new Error("Upload failed — is the server running?"));
    x.send(fd);
  });
}

/* ---------- live updates (Server-Sent Events) ---------- */
function listen(id) {
  if (es) es.close();
  es = new EventSource(`/api/job/${id}/progress`);
  es.onmessage = e => render(JSON.parse(e.data));
  es.onerror = () => { es.close(); poll(id); };
}
let pollT = null;
const uploading = j => (j.clips || []).some(c => c.yt && (c.yt.status === "queued" || c.yt.status === "uploading"));
function keepPolling(id) { if (!pollT) pollT = setTimeout(() => { pollT = null; poll(id); }, 1000); }
async function poll(id) {
  try { const r = await fetch(`/api/job/${id}`); if (!r.ok) throw 0; const j = await r.json(); render(j); if (j.status === "processing" || uploading(j)) keepPolling(id); }
  catch { fail("Lost connection to the server."); }
}

function render(j) {
  curJob = j;
  setStage(j.detail || j.step, j.progress);
  const s = j.source || {};
  if (s.title) $("#srcTitle").textContent = s.title;
  if (s.thumbnail) $("#thumb").style.backgroundImage = `url("${s.thumbnail}")`;
  $("#srcMeta").textContent = [s.duration ? "Length " + mmss(s.duration) : "", j.transcript_words ? j.transcript_words.toLocaleString() + " words transcribed" : ""].filter(Boolean).join(" · ");
  $("#steps").innerHTML = (j.steps || []).map((n, i) => `<li class="${j.status === "completed" || i < j.step_index ? "ok" : i === j.step_index && j.status === "processing" ? "act" : ""}">${n}</li>`).join("");
  renderClips(j);
  if (j.status === "completed") { done(j); if (uploading(j)) keepPolling(j.id); }
  if (j.status === "failed") fail(j.error);
}
function renderClips(j) {
  const clips = j.clips || []; if (!clips.length) return;
  $("#resHead").hidden = false;
  const ready = clips.filter(c => c.status === "ready").length;
  $("#clipCount").textContent = `${ready} of ${clips.length} ready`;
  $("#zip").disabled = !ready; $("#zip").style.opacity = ready ? 1 : .4;
  const grid = $("#grid"); const ratio = (j.opts && j.opts.fmt) || opts.fmt;
  clips.forEach(c => {
    let card = grid.querySelector(`[data-i="${c.index}"]`);
    if (!card) {
      card = $("#clipTpl").content.firstElementChild.cloneNode(true); card.dataset.i = c.index;
      card.querySelector(".vid").classList.add(ratio === "1:1" ? "r11" : ratio === "16:9" ? "r169" : "r916");
      grid.appendChild(card);
    }
    const sc = card.querySelector(".score"); sc.textContent = `🔥 ${c.score}`; sc.classList.toggle("hot", c.score >= 70);
    card.querySelector(".dur").textContent = mmss(c.end - c.start);
    card.querySelector(".range").textContent = `From ${mmss(c.start)} to ${mmss(c.end)}`;
    card.querySelector(".hook").textContent = "“" + (c.hook || "") + "”";
    card.querySelector(".ctitle").textContent = c.title || `Clip ${c.index + 1}`;
    card.classList.toggle("wait", c.status !== "ready");
    if (c.status === "ready") {
      card.querySelector(".ctitle").textContent = c.title || `Clip ${c.index + 1}`;
      card.querySelector(".tags").innerHTML = (c.hashtags || []).slice(0, 6).map(h => `<span>${esc(h.startsWith("#") ? h : "#" + h)}</span>`).join("");
      ytState(card, c.yt || {});
    }
    if (c.status !== "ready") {
      card.querySelector(".phTxt").textContent = c.status === "rendering" ? `Rendering… ${c.render || 0}%` : "Queued";
      card.querySelector(".minibar i").style.width = (c.render || 0) + "%";
    } else if (!card.querySelector("video")) {
      const v = document.createElement("video");
      Object.assign(v, { src: c.url, controls: true, playsInline: true, preload: "metadata" }); if (c.thumb) v.poster = c.thumb;
      card.querySelector(".ph").replaceWith(v);
      card.querySelector(".tags").innerHTML = (c.hashtags || []).slice(0, 6).map(h => `<span>${esc(h.startsWith("#") ? h : "#" + h)}</span>`).join("");
      card.querySelector(".dl").onclick = async e => {
        const b = e.target, q = card.querySelector(".q").value; b.disabled = true; b.textContent = "Saving…";
        try {
          const r = await fetch(`/api/clip/${j.id}/${c.index}/save?q=${q}`, { method: "POST" }); const d = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(d.detail || "Save failed");
          toast(d.existed ? `Already saved: Desktop/Short YouTube/${d.name}` : `✓ Saved to Desktop/Short YouTube/${d.name}`); b.textContent = "✓ Saved";
        } catch (err) { toast(err.message); b.textContent = "⬇ Save MP4"; }
        setTimeout(() => { b.disabled = false; b.textContent = "⬇ Save MP4"; }, 2500);
      };
      card.querySelector(".srt").href = `${c.url}/clip.srt`;
      card.querySelector(".th").href = c.thumb || "#"; card.querySelector(".th").hidden = !c.thumb;
      card.querySelector(".e-title").value = c.title || ""; card.querySelector(".e-desc").value = c.description || "";
      card.querySelector(".e-tags").value = (c.hashtags || []).join(" ");
      card.querySelector(".e-save").onclick = async e => {
        const b = e.target; b.textContent = "Saving…";
        const tags = card.querySelector(".e-tags").value.split(/[\s,]+/).filter(Boolean).map(t => t.startsWith("#") ? t : "#" + t);
        const r = await fetch(`/api/clip/${j.id}/${c.index}/meta`, { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: card.querySelector(".e-title").value.trim(), description: card.querySelector(".e-desc").value, hashtags: tags }) });
        b.textContent = r.ok ? "Saved ✓" : "Save failed"; setTimeout(() => b.textContent = "Save changes", 1500);
        if (r.ok) { const jj = await (await fetch(`/api/job/${j.id}`)).json(); const cc = jj.clips[c.index]; if (cc.thumb) card.querySelector("video").poster = cc.thumb; card.querySelector(".th").href = cc.thumb; render(jj); }
      };
      card.querySelector(".pub").onclick = () => publish(j.id, c.index, card);
      card.querySelector(".copy").onclick = e => {
        navigator.clipboard.writeText(`${c.title}\n\n${c.description || ""}\n\n${(c.hashtags || []).join(" ")}`);
        e.target.textContent = "Copied ✓"; setTimeout(() => e.target.textContent = "Copy caption", 1500);
      };
    }
  });
}

$("#zip").onclick = async () => {
  if (!curJob) return; const b = $("#zip"), q = $("#qAll").value; b.disabled = true; b.textContent = "Saving…";
  try {
    const r = await fetch(`/api/save-all/${curJob.id}?q=${q}`, { method: "POST" }); const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || "Save failed");
    toast(`✓ ${d.saved} saved${d.already ? `, ${d.already} already there` : ""} → Desktop/Short YouTube`);
  } catch (err) { toast(err.message); }
  b.disabled = false; b.textContent = "⬇ Save all";
};
$("#openDir").onclick = () => fetch("/api/open-folder", { method: "POST" });

/* ---------- UI state ---------- */
function setStage(text, pct = 0) {
  $("#detail").textContent = text || ""; $("#pct").textContent = Math.round(pct) + "%"; $("#barFill").style.width = pct + "%";
}
function resetWork() {
  $("#grid").innerHTML = ""; $("#resHead").hidden = true; $("#steps").innerHTML = ""; $("#thumb").style.backgroundImage = "";
  $("#srcMeta").textContent = ""; showErr("");
  const l = $(".live"); l.className = "live"; $("#liveTxt").textContent = "LIVE";
  t0 = Date.now(); clearInterval(timer); timer = setInterval(() => $("#elapsed").textContent = mmss((Date.now() - t0) / 1000), 1000);
}
function done(j) {
  stop(); const l = $(".live"); l.className = "live done"; $("#liveTxt").textContent = "COMPLETED";
  setStage(`${(j.clips || []).length} clips ready in ${mmss((Date.now() - t0) / 1000)}`, 100);
}
function fail(msg) {
  stop(); const l = $(".live"); l.className = "live fail"; $("#liveTxt").textContent = "FAILED";
  $("#detail").textContent = msg || "Something went wrong"; showErr(msg || "Something went wrong");
}
function stop() { busy = false; clearInterval(timer); if (es) es.close(); $("#go").textContent = "✦ Get clips"; setMode(mode); }
$("#newBtn").onclick = () => { stop(); try { localStorage.removeItem("cf_job"); } catch {} urlIn.value = ""; file = null; $("#file").value = ""; setMode(null); $("#work").hidden = true; window.scrollTo({ top: 0, behavior: "smooth" }); urlIn.focus(); };
const mmss = s => { s = Math.max(0, Math.round(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

/* resume a running job after page refresh */
(async () => {
  let id = null; try { id = localStorage.getItem("cf_job"); } catch {}
  if (!id) return;
  const r = await fetch(`/api/job/${id}`).catch(() => null);
  if (!r || !r.ok) { try { localStorage.removeItem("cf_job"); } catch {} return; }   // finished clips stay visible after a refresh
  busy = true; resetWork(); $("#work").hidden = false; listen(id);
})();
setMode(null);

/* ---------- YouTube ---------- */
function setSeg(k, v) { document.querySelectorAll(`.seg[data-k="${k}"] button`).forEach(b => b.classList.toggle("on", b.dataset.v === v)); }
function toast(m) { const t = document.createElement("div"); t.className = "toast"; t.textContent = m; document.body.appendChild(t); setTimeout(() => t.remove(), 3500); }
function needYouTube() { yt.configured ? (location.href = "/api/youtube/connect") : ($("#setup").hidden = false); }
$("#setupClose").onclick = () => $("#setup").hidden = true;
$("#setup").addEventListener("click", e => { if (e.target.id === "setup") $("#setup").hidden = true; });
async function loadYT() {
  try { yt = await (await fetch("/api/youtube/status")).json(); } catch { yt = { configured: false, connected: false }; }
  const nav = $("#ytNav");
  if (yt.connected) {
    const ch = yt.channel || {};
    nav.innerHTML = `<div class="ytchip">${ch.thumbnail ? `<img src="${esc(ch.thumbnail)}" alt="">` : ""}<span class="dot"></span><span>${esc(ch.title || "YouTube")}</span><button id="ytOff" title="Disconnect">Disconnect</button></div>`;
    $("#ytOff").onclick = async () => { if (!confirm("Disconnect this YouTube channel?")) return; await fetch("/api/youtube/disconnect", { method: "POST" }); loadYT(); };
  } else {
    nav.innerHTML = `<button class="btn btn-yt sm" id="ytConnect">▶ Connect YouTube</button>`;
    $("#ytConnect").onclick = needYouTube;
    if (opts.autopost) { opts.autopost = false; setSeg("autopost", "false"); }
  }
  document.querySelectorAll(".yt-opt").forEach(o => o.classList.toggle("dim", !yt.connected));
  if (yt.error) toast(yt.error);
  if (curJob) render(curJob);
}
function ytState(card, s) {
  const st = card.querySelector(".ytst"), b = card.querySelector(".pub");
  st.className = "ytst"; b.hidden = false; b.disabled = false; b.textContent = "▶ Post to YouTube";
  if (s.status === "queued" || s.status === "uploading") {
    b.disabled = true; b.textContent = "Posting…";
    st.textContent = `${s.stage || "Uploading"}${s.stage === "Uploading video" ? ` · ${s.pct || 0}%` : "…"}`;
  } else if (s.status === "done") {
    b.hidden = true; st.classList.add("ok");
    st.innerHTML = `✓ Posted (${esc(s.privacy || "private")}) · <a href="${esc(s.url)}" target="_blank">View on YouTube</a>` + (s.warnings || []).map(w => `<span class="warn">${esc(w)}</span>`).join("");
  } else if (s.status === "error") {
    st.classList.add("bad"); st.textContent = s.error; b.textContent = "↻ Retry";
  } else st.textContent = yt.connected ? `Will post as ${opts.privacy}` : "Connect YouTube to post";
}
async function publish(jid, n, card) {
  if (!yt.connected) return needYouTube();
  const r = await fetch(`/api/clip/${jid}/${n}/publish`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ privacy: opts.privacy }) });
  if (!r.ok) { const e = await r.json().catch(() => ({})); return toast(e.detail || "Could not start upload"); }
  card.querySelector(".pub").disabled = true; card.querySelector(".ytst").textContent = "Queued…";
  if (!es || es.readyState === 2) keepPolling(jid);   // keep live updates flowing after the job finished
}
(() => {
  const q = new URLSearchParams(location.search).get("yt");
  if (q) { toast(q === "connected" ? "✓ YouTube channel connected" : q === "denied" ? "YouTube access was cancelled" : "YouTube connection failed — try again"); history.replaceState(null, "", "/"); }
  loadYT();
})();

/* ---------- light / dark theme ---------- */
(() => {
  const btn = $("#themeBtn"), root = document.documentElement;
  const paint = () => { const dark = root.dataset.theme === "dark"; btn.textContent = dark ? "☀️" : "🌙"; btn.title = dark ? "Switch to light theme" : "Switch to dark theme"; };
  btn.onclick = () => { root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark"; try { localStorage.setItem("cf_theme", root.dataset.theme); } catch {} paint(); };
  paint();
})();
