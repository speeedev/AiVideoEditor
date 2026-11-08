"use strict";
const $ = (id) => document.getElementById(id);
const LABEL_W = 90;
let S = null;            // server state
let pps = 40;            // timeline pixels per second
let playhead = 0;
let assistantEl = null;
let thinkingEl = null;
let lastToolEls = [];
let stillTimer = null;
let refreshTimer = null;
let userZoomed = false;

const ROLE_TR = { screen: "ekran", camera: "kamera", mic: "mikrofon", music: "müzik", broll: "b-roll", sfx: "efekt", other: "diğer", overlay: "bindirme" };

// ------------------------------------------------------------------ helpers --
async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
function tc(t) {
  t = Math.max(0, t || 0);
  const m = Math.floor(t / 60), s = t - m * 60;
  return `${String(m).padStart(2, "0")}:${s.toFixed(2).padStart(5, "0")}`;
}
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function status(text, isError) {
  const s = $("statusText");
  s.textContent = text;
  s.style.color = isError ? "var(--danger)" : "";
}
function guard(fn) {
  return async (...args) => {
    try { await fn(...args); } catch (e) { status(e.message, true); }
  };
}

// -------------------------------------------------------------------- state --
async function refresh() {
  S = await api("/api/state");
  renderAll();
}
function refreshSoon() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => refresh().catch(() => {}), 150);
}
function renderAll() {
  $("projectName").textContent = S.project.name;
  if (document.activeElement !== $("brief")) $("brief").value = S.project.brief || "";
  $("model").value = S.model;
  $("effort").value = S.effort;
  renderAssets();
  renderTimeline();
  renderRenders();
  setBusy(S.job);
  const empty = !S.timeline.tracks.length;
  $("viewerEmpty").classList.toggle("hidden", !empty);
  $("frame").classList.toggle("hidden", empty || !$("tabFrame").classList.contains("active"));
  if (!empty && $("tabFrame").classList.contains("active")) requestStill();
}
function setBusy(job) {
  const busy = !!job;
  $("jobSpinner").classList.toggle("hidden", !busy);
  ["btnAnalyze", "btnSync", "btnAssemble", "btnUndo", "btnRedo", "btnPreview", "btnFinal", "send"].forEach(
    (id) => { $(id).disabled = busy; });
  if (busy) status(`${job} çalışıyor…`);
}

// ------------------------------------------------------------------- assets --
function renderAssets() {
  const box = $("assets");
  box.innerHTML = "";
  if (!S.assets.length) box.appendChild(el("div", "muted small", "Henüz kayıt yok."));
  for (const a of S.assets) {
    const item = el("div", "asset");
    item.style.borderLeftColor = `var(--${["screen", "camera", "mic", "music", "broll"].includes(a.role) ? a.role : "other"})`;
    item.appendChild(el("div", "name", a.name));
    const meta = [tc(a.duration)];
    if (a.has_video) meta.push(`${a.width}×${a.height}`);
    meta.push(a.has_audio ? "ses var" : "ses yok");
    item.appendChild(el("div", "meta", `${a.id} · ${meta.join(" · ")}`));
    const badges = el("div", "badges");
    const b = (text, cls) => badges.appendChild(el("span", "badge " + (cls || ""), text));
    if (a.analysis.transcript) b(`transkript ${a.analysis.words}`, "on");
    if (a.analysis.speech) b("sessizlik", "on");
    if (a.analysis.visual) b("görsel", "on");
    if (a.sync) {
      if (a.sync.method === "reference") b("senkron referansı", "on");
      else if (a.sync.method === "audio") b(`senkron ${a.sync.offset >= 0 ? "+" : ""}${a.sync.offset.toFixed(3)} sn`, "on");
      else b(`senkron ${a.sync.method} ${a.sync.offset.toFixed(2)} sn`, "warn");
    }
    item.appendChild(badges);
    const row = el("div", "row");
    const sel = el("select");
    for (const r of S.roles) {
      const o = el("option", "", ROLE_TR[r] || r);
      o.value = r;
      sel.appendChild(o);
    }
    sel.value = a.role;
    sel.onchange = guard(() => api(`/api/assets/${encodeURIComponent(a.id)}`, { role: sel.value }));
    const del = el("button", "small", "✕");
    del.title = "Projeden çıkar";
    del.onclick = guard(async () => {
      if (confirm(`${a.name} projeden çıkarılsın mı? (dosya silinmez)`))
        await api(`/api/assets/${encodeURIComponent(a.id)}`, { delete: true });
    });
    row.append(sel, del);
    item.appendChild(row);
    box.appendChild(item);
  }
}

function guessRole(name) {
  const n = name.toLowerCase();
  const sel = $("uploadRole").value;
  if (sel !== "auto") return sel;
  if (/music|müzik|muzik|song|bgm/.test(n)) return "music";
  if (/screen|ekran|display|capture|obs/.test(n)) return "screen";
  if (/cam|kamera|webcam|face|yuz|yüz/.test(n)) return "camera";
  if (/\.(wav|mp3|m4a|aac|flac|ogg|aiff?)$/.test(n)) return "mic";
  return "other";
}
async function uploadFiles(files) {
  for (const f of files) {
    status(`${f.name} yükleniyor…`);
    const res = await fetch(`/api/upload?name=${encodeURIComponent(f.name)}&role=${guessRole(f.name)}`,
      { method: "POST", body: f });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { status(`${f.name}: ${data.error || res.statusText}`, true); continue; }
    status(`${f.name} eklendi`);
  }
  refresh();
}

// ----------------------------------------------------------------- timeline --
function niceStep() {
  const steps = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
  return steps.find((s) => s * pps >= 70) || 600;
}
function clipTags(c, track) {
  const tags = [];
  if (Math.abs(c.speed - 1) > 1e-6) tags.push(`${+c.speed.toFixed(2)}x`);
  if (track.kind === "video") {
    if (!c.visible) tags.push("gizli");
    else {
      const t = c.transform;
      if (t.crop) tags.push("zoom");
      if (!(t.x === 0 && t.y === 0 && t.w === 1 && t.h === 1)) tags.push(t.w < 0.5 && t.h < 0.5 ? "PiP" : "kutu");
    }
  }
  if (c.volume === 0 && track.kind === "audio") tags.push("sessiz");
  if (c.fade_in || c.fade_out) tags.push("fade");
  return tags;
}
function renderTimeline() {
  const tl = S.timeline;
  const dur = Math.max(S.duration, 1);
  if (!userZoomed) {
    const avail = $("tlScroll").clientWidth - LABEL_W - 30;
    pps = Math.max(2, Math.min(400, avail / dur));
    $("tlZoom").value = pps;
  }
  const width = dur * pps + 120;
  $("tlInner").style.width = `${width + LABEL_W}px`;
  const ruler = $("ruler");
  ruler.innerHTML = "";
  ruler.style.width = `${width}px`;
  const step = niceStep();
  for (let t = 0; t <= dur + step; t += step) {
    const tick = el("div", "tick", tc(t).replace(/\.00$/, ""));
    tick.style.left = `${t * pps}px`;
    ruler.appendChild(tick);
  }
  for (const m of tl.markers) {
    const mk = el("div", "marker");
    mk.style.left = `${m.time * pps}px`;
    mk.title = m.note;
    ruler.appendChild(mk);
  }
  const tracks = $("tracks");
  tracks.innerHTML = "";
  const video = tl.tracks.filter((t) => t.kind === "video").reverse();  // top layer first
  const audio = tl.tracks.filter((t) => t.kind === "audio");
  for (const t of [...video, ...audio]) {
    const row = el("div", "track");
    const label = el("div", "label");
    label.innerHTML = `<b>${t.id}</b> ${ROLE_TR[t.role] || t.role || ""}<br>` +
      [t.muted && "sessiz", t.hidden && "gizli", t.locked && "kilitli", t.duck && "duck",
       t.denoise && "gürültü↓", t.enhance && "ses+"].filter(Boolean).join(" ");
    const lane = el("div", "lane");
    lane.style.width = `${width}px`;
    for (const c of t.clips) {
      const d = (c.source_out - c.source_in) / c.speed;
      const ce = el("div", `clip ${t.role || "other"}`);
      if (t.kind === "video" && !c.visible) ce.classList.add("hidden-clip");
      if ((t.kind === "audio" && c.volume === 0) || t.muted) ce.classList.add("muted-clip");
      ce.style.left = `${c.start * pps}px`;
      ce.style.width = `${Math.max(1, d * pps - 1)}px`;
      const tags = clipTags(c, t);
      ce.innerHTML = tags.map((x) => `<span class="tag">${x}</span>`).join("") + (c.label || c.asset_id);
      ce.title = `${c.id} · ${c.asset_id}\n${tc(c.start)} – ${tc(c.start + d)} (${d.toFixed(2)} sn)\n` +
        `kaynak ${c.source_in.toFixed(2)} – ${c.source_out.toFixed(2)}\nses ${c.volume}`;
      lane.appendChild(ce);
    }
    row.append(label, lane);
    tracks.appendChild(row);
  }
  if (tl.texts.length) {
    const row = el("div", "track small");
    const label = el("div", "label", "Yazılar");
    const lane = el("div", "lane");
    lane.style.width = `${width}px`;
    for (const x of tl.texts) {
      const te = el("div", "textov", x.text);
      te.style.left = `${x.start * pps}px`;
      te.style.width = `${Math.max(2, (x.end - x.start) * pps - 1)}px`;
      te.title = `${x.id}: ${x.text}`;
      lane.appendChild(te);
    }
    row.append(label, lane);
    tracks.appendChild(row);
  }
  if (S.words.length) {
    const row = el("div", "track small");
    const label = el("div", "label", tl.captions.enabled ? "Konuşma CC" : "Konuşma");
    const lane = el("div", "lane");
    lane.style.width = `${width}px`;
    if (pps >= 25) {
      let lastEnd = -1;
      for (const w of S.words) {
        const x = w.s * pps;
        if (x < lastEnd) continue;            // skip words that would overlap the previous one
        const we = el("div", "word", w.t);
        we.style.left = `${x}px`;
        we.title = `${w.t} ${tc(w.s)}–${tc(w.e)}`;
        lane.appendChild(we);
        lastEnd = x + w.t.length * 6.2 + 6;
      }
    } else {
      lane.appendChild(Object.assign(el("div", "word muted", "kelimeleri görmek için yakınlaştır"), { style: "left:6px" }));
    }
    row.append(label, lane);
    tracks.appendChild(row);
  }
  document.querySelectorAll(".lane, .ruler").forEach((lane) => {
    lane.onmousedown = (e) => {
      if (e.button !== 0) return;
      const r = lane.getBoundingClientRect();
      const t0 = (e.clientX - r.left) / pps;
      let moved = false;
      const move = (ev) => {
        const t1 = (ev.clientX - r.left) / pps;
        if (Math.abs(ev.clientX - e.clientX) > 4) {
          moved = true;
          setSelection(Math.min(t0, t1), Math.max(t0, t1));
        }
      };
      const up = (ev) => {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        if (!moved) setPlayhead(t0);
        else setPlayhead((ev.clientX - r.left) / pps);
      };
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
      e.preventDefault();
    };
  });
  drawSelection();
  $("tlInfo").textContent = `${tl.tracks.length} track · ${tl.tracks.reduce((n, t) => n + t.clips.length, 0)} klip · ` +
    `${tc(S.duration)} · ${tl.canvas.width}×${tl.canvas.height} ${tl.canvas.fps} fps`;
  setPlayhead(Math.min(playhead, S.duration), true);
}
let selection = null;  // [start, end] in timeline seconds
function setSelection(a, b) {
  a = Math.max(0, a);
  b = Math.min(S.duration, b);
  selection = b - a > 0.02 ? [a, b] : null;
  drawSelection();
}
function drawSelection() {
  const box = $("selection");
  const tools = $("selBox");
  if (!selection || !S) { box.classList.add("hidden"); tools.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  tools.classList.remove("hidden");
  box.style.left = `${selection[0] * pps}px`;
  box.style.width = `${(selection[1] - selection[0]) * pps}px`;
  $("selText").textContent = `Seçim ${tc(selection[0])}–${tc(selection[1])} (${(selection[1] - selection[0]).toFixed(2)} sn)`;
}
function setPlayhead(t, noStill) {
  playhead = Math.max(0, Math.min(t, Math.max(0, S.duration - 0.001)));
  $("playhead").style.left = `${playhead * pps}px`;
  $("timecode").textContent = `${tc(playhead)} / ${tc(S.duration)}`;
  if (!$("video").classList.contains("hidden")) {
    if (Math.abs($("video").currentTime - playhead) > 0.05) $("video").currentTime = playhead;
  } else if (!noStill) requestStill();
}
function requestStill() {
  if (!S || !S.timeline.tracks.length || S.duration <= 0) return;
  clearTimeout(stillTimer);
  stillTimer = setTimeout(() => {
    const w = Math.min(1280, Math.max(320, Math.round($("frame").parentElement.clientWidth)));
    $("frame").src = `/api/still?t=${playhead.toFixed(3)}&w=${w}&_=${Date.now()}`;
  }, 120);
}

// ------------------------------------------------------------------ viewer --
function renderRenders() {
  const sel = $("renderSelect");
  const cur = sel.value;
  sel.innerHTML = "";
  for (const r of S.renders) {
    const o = el("option", "", r);
    o.value = r;
    sel.appendChild(o);
  }
  if (cur && S.renders.includes(cur)) sel.value = cur;
}
function showTab(which) {
  const video = which === "video";
  $("tabFrame").classList.toggle("active", !video);
  $("tabVideo").classList.toggle("active", video);
  $("frame").classList.toggle("hidden", video);
  $("video").classList.toggle("hidden", !video);
  $("renderSelect").classList.toggle("hidden", !video);
  if (video) {
    if (!S.renders.length) { status("Henüz render yok: önce 'Ön izleme render'."); return; }
    const file = $("renderSelect").value || S.renders[0];
    const src = `/renders/${encodeURIComponent(file)}`;
    if (!$("video").src.endsWith(src)) $("video").src = src;
  } else requestStill();
}

// --------------------------------------------------------------------- chat --
function chatAdd(cls, text) {
  const m = el("div", "msg " + cls, text);
  $("chat").appendChild(m);
  $("chat").scrollTop = $("chat").scrollHeight;
  $("suggestions").classList.add("hidden");
  return m;
}
function shortArgs(args) {
  const s = JSON.stringify(args || {});
  return s.length > 140 ? s.slice(0, 137) + "…" : s;
}
function handleEvent(ev) {
  const chat = $("chat");
  switch (ev.type) {
    case "state": refreshSoon(); break;
    case "log": status(ev.message); break;
    case "progress": {
      $("progress").classList.remove("hidden");
      $("progress").firstElementChild.style.width = `${(100 * ev.i) / Math.max(ev.n, 1)}%`;
      status(`Render ${Math.min(ev.i + 1, ev.n)}/${ev.n}`);
      break;
    }
    case "job": {
      if (ev.status === "running") { setBusy(ev.name); break; }
      setBusy(null);
      $("progress").classList.add("hidden");
      if (ev.status === "error") {
        status(`${ev.name}: ${ev.error}`, true);
        if (ev.name === "agent") chatAdd("error", ev.error);
      } else {
        status(`${ev.name} tamamlandı`);
        if (ev.name === "render" && ev.result) {
          refresh().then(() => { $("renderSelect").value = ev.result.file; showTab("video"); });
        }
      }
      break;
    }
    case "agent_text":
      thinkingEl = null;
      if (!assistantEl) assistantEl = chatAdd("assistant", "");
      assistantEl.textContent += ev.delta;
      chat.scrollTop = chat.scrollHeight;
      break;
    case "agent_thinking":
      if (!thinkingEl) thinkingEl = chatAdd("thinking", "");
      thinkingEl.textContent += ev.delta;
      chat.scrollTop = chat.scrollHeight;
      break;
    case "agent_turn_end": assistantEl = null; thinkingEl = null; break;
    case "tool_call": {
      assistantEl = null; thinkingEl = null;
      const t = el("div", "tool");
      t.innerHTML = `<span class="name"></span> <span class="args"></span>`;
      t.querySelector(".name").textContent = ev.name;
      t.querySelector(".args").textContent = shortArgs(ev.args);
      chat.appendChild(t);
      lastToolEls.push(t);
      chat.scrollTop = chat.scrollHeight;
      break;
    }
    case "tool_result": {
      const idx = lastToolEls.findIndex((x) => x.querySelector(".name").textContent === ev.name && !x.dataset.done);
      const t = idx >= 0 ? lastToolEls[idx] : el("div", "tool");
      t.dataset.done = "1";
      if (ev.is_error) t.classList.add("err");
      const det = el("details");
      const sum = el("summary", "", (ev.is_error ? "✗ " : "✓ ") + (ev.text.split("\n")[0] || "").slice(0, 120));
      det.appendChild(sum);
      if (ev.text.includes("\n")) det.appendChild(el("pre", "", ev.text));
      for (const src of ev.images || []) {
        const img = el("img");
        img.src = src;
        img.onclick = () => { $("lightbox").querySelector("img").src = src; $("lightbox").classList.remove("hidden"); };
        det.appendChild(img);
        det.open = true;
      }
      t.appendChild(det);
      lastToolEls = lastToolEls.filter((x) => !x.dataset.done);
      chat.scrollTop = chat.scrollHeight;
      break;
    }
    case "agent_done":
      assistantEl = null; thinkingEl = null; lastToolEls = [];
      chat.appendChild(el("div", "done", `— ${ev.stop_reason} · ${ev.usage} —`));
      chat.scrollTop = chat.scrollHeight;
      break;
  }
}
async function loadConversation() {
  const conv = await api("/api/conversation");
  for (const m of conv) {
    if (m.role === "tool") {
      const t = el("div", "tool");
      t.innerHTML = `<span class="name"></span>`;
      t.querySelector(".name").textContent = m.text;
      $("chat").appendChild(t);
    } else chatAdd(m.role, m.text);
  }
}
const send = guard(async () => {
  const text = $("prompt").value.trim();
  if (!text) return;
  let instruction = text;
  if (selection) {
    instruction = `[Seçili aralık (timeline): ${selection[0].toFixed(2)}–${selection[1].toFixed(2)} sn]\n${text}`;
  }
  chatAdd("user", instruction);
  $("prompt").value = "";
  await api("/api/agent", { instruction, new: $("newConv").checked });
  $("newConv").checked = false;
});

// ------------------------------------------------------------------- wiring --
function wire() {
  const roleSel = $("uploadRole");
  roleSel.appendChild(Object.assign(el("option", "", "rolü tahmin et"), { value: "auto" }));
  for (const r of ["screen", "camera", "mic", "music", "broll", "sfx", "other"])
    roleSel.appendChild(Object.assign(el("option", "", ROLE_TR[r]), { value: r }));

  $("btnAnalyze").onclick = guard(() => api("/api/analyze", {
    transcribe: $("anTranscribe").checked, model: $("anModel").value, language: $("anLang").value,
  }));
  $("btnSync").onclick = guard(() => api("/api/sync", {}));
  $("btnAssemble").onclick = guard(async () => {
    if (S.timeline.tracks.length && !confirm("Mevcut kurgu silinip kayıtlar baştan dizilecek (geri alınabilir). Devam?")) return;
    await api("/api/assemble", {});
  });
  $("btnUndo").onclick = guard(() => api("/api/undo", {}));
  $("btnRedo").onclick = guard(() => api("/api/redo", {}));
  $("btnPreview").onclick = guard(() => api("/api/render", { preview: true }));
  $("btnFinal").onclick = guard(() => api("/api/render", { preview: false }));
  $("exportFormat").onchange = guard(async () => {
    const fmt = $("exportFormat").value;
    if (!fmt) return;
    $("exportFormat").value = "";
    const r = await api("/api/export", { format: fmt });
    status(`Dışa aktarıldı: ${r.file}`);
    window.open(r.url, "_blank");
  });
  $("brief").onchange = guard(() => api("/api/brief", { brief: $("brief").value }));
  $("model").onchange = $("effort").onchange = guard(() =>
    api("/api/settings", { model: $("model").value, effort: $("effort").value }));
  $("tabFrame").onclick = () => showTab("frame");
  $("tabVideo").onclick = () => showTab("video");
  $("renderSelect").onchange = () => showTab("video");
  $("video").ontimeupdate = () => {
    playhead = $("video").currentTime;
    $("playhead").style.left = `${playhead * pps}px`;
    $("timecode").textContent = `${tc(playhead)} / ${tc(S.duration)}`;
  };
  $("tlZoom").oninput = () => { pps = +$("tlZoom").value; userZoomed = true; renderTimeline(); };
  $("tlFit").onclick = () => { userZoomed = false; renderTimeline(); };
  $("send").onclick = send;
  $("selClear").onclick = () => { selection = null; drawSelection(); };
  $("selCut").onclick = guard(async () => {
    if (!selection) return;
    const r = await api("/api/edit", { tool: "ripple_delete", args: { ranges: [{ start: selection[0], end: selection[1] }] } });
    status(r.result);
    selection = null;
    drawSelection();
  });
  $("selLayout").onchange = guard(async () => {
    const layout = $("selLayout").value;
    $("selLayout").value = "";
    if (!selection || !layout) return;
    const r = await api("/api/edit", { tool: "apply_layout", args: { start: selection[0], end: selection[1], layout } });
    status(r.result);
  });
  $("prompt").onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } };
  for (const b of $("suggestions").querySelectorAll("button"))
    b.onclick = () => { $("prompt").value = b.textContent; $("prompt").focus(); };
  $("lightbox").onclick = () => $("lightbox").classList.add("hidden");

  const drop = $("drop");
  ["dragenter", "dragover"].forEach((n) => drop.addEventListener(n, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((n) => drop.addEventListener(n, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => guard(uploadFiles)([...e.dataTransfer.files]));
  $("fileInput").onchange = () => guard(uploadFiles)([...$("fileInput").files]);
  $("btnAddPath").onclick = guard(async () => {
    const p = $("pathInput").value.trim();
    if (!p) return;
    await api("/api/assets", { path: p, role: guessRole(p) });
    $("pathInput").value = "";
  });

  document.addEventListener("keydown", (e) => {
    if (["TEXTAREA", "INPUT", "SELECT"].includes(document.activeElement.tagName)) return;
    const fps = S ? S.timeline.canvas.fps : 30;
    if ((e.key === "Delete" || e.key === "Backspace") && selection) { $("selCut").click(); }
    else if (e.key === "Escape") { selection = null; drawSelection(); }
    else if (e.key === "ArrowRight") setPlayhead(playhead + (e.shiftKey ? 1 : 1 / fps));
    else if (e.key === "ArrowLeft") setPlayhead(playhead - (e.shiftKey ? 1 : 1 / fps));
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
      e.preventDefault();
      guard(() => api(e.shiftKey ? "/api/redo" : "/api/undo", {}))();
    } else return;
    e.preventDefault();
  });
  window.addEventListener("resize", () => { if (S) renderTimeline(); });

  const es = new EventSource("/api/events");
  es.onmessage = (e) => handleEvent(JSON.parse(e.data));
  es.onerror = () => status("Sunucu bağlantısı koptu, yeniden deneniyor…", true);
  es.onopen = () => status("Hazır");
}

wire();
refresh().then(loadConversation).catch((e) => status(e.message, true));
