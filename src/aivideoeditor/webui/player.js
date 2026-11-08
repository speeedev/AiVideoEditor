"use strict";
// Live preview: the timeline is composited in the browser from the source media (editing proxies
// for the picture, the original files for sound), so the edit plays immediately - no render needed.
// Titles use the same images the renderer overlays (/api/textimg); captions use the renderer's timing.

const Live = {
  playing: false,
  t0: 0,          // timeline time at wall0
  wall0: 0,
  raf: 0,
  pools: new Map(),   // track id + kind -> [element, element] (current + preloaded next clip)
  images: new Map(),  // image asset id -> HTMLImageElement
  titles: new Map(),  // text key -> HTMLImageElement
  frameEls: new Map(),
  tails: new Map(),   // track id -> {clip, el}: previous clip continuing under a crossfade
};

function liveCanvas() { return document.getElementById("live"); }
function clipEnd(c) { return c.start + (c.source_out - c.source_in) / c.speed; }
function srcTime(c, t) { return c.source_in + (t - c.start) * c.speed; }
function activeClip(track, t) { return track.clips.find((c) => c.start <= t && t < clipEnd(c)) || null; }
function assetOf(id) { return S.assets.find((a) => a.id === id); }

function crossfadeLen(c) {
  return c.transition === "crossfade" ? Math.min(c.transition_duration, (clipEnd(c) - c.start) / 2) : 0;
}
function prevClip(track, c) {
  return track.clips.find((p) => p !== c && Math.abs(clipEnd(p) - c.start) < 0.05) || null;
}
function clipFade(c, t) {
  let a = 1;
  const fi = Math.max(c.fade_in, crossfadeLen(c));
  if (fi > 0) a = Math.min(a, (t - c.start) / fi);
  if (c.fade_out > 0) a = Math.min(a, (clipEnd(c) - t) / c.fade_out);
  return Math.max(0, Math.min(1, a));
}
function smooth(x) { x = Math.max(0, Math.min(1, x)); return x * x * (3 - 2 * x); }
function zoomAmount(c, t) {
  let a = 1;
  if (c.zoom_in > 0) a = Math.min(a, smooth((t - c.start) / c.zoom_in));
  if (c.zoom_out > 0) a = Math.min(a, smooth((clipEnd(c) - t) / c.zoom_out));
  return a;
}
function colorFilter(c) {
  const g = c.color || {};
  const parts = [];
  if (g.brightness) parts.push(`brightness(${1 + g.brightness})`);
  if (g.contrast && g.contrast !== 1) parts.push(`contrast(${g.contrast})`);
  if (g.saturation !== undefined && g.saturation !== 1) parts.push(`saturate(${g.saturation})`);
  if (g.warmth) parts.push(g.warmth > 0 ? `sepia(${Math.min(0.5, g.warmth * 0.5)})` : `hue-rotate(${g.warmth * 15}deg)`);
  return parts.join(" ") || "none";
}

// ------------------------------------------------------------- media pool --
function makeEl(kind) {
  const e = document.createElement(kind === "v" ? "video" : "audio");
  e.preload = "auto";
  e.playsInline = true;
  if (kind === "v") e.muted = true;      // sound comes from the original file
  document.getElementById("mediaPool").appendChild(e);
  return e;
}
function pool(trackId, kind) {
  const key = `${trackId}:${kind}`;
  if (!Live.pools.has(key)) Live.pools.set(key, [makeEl(kind), makeEl(kind)]);
  return Live.pools.get(key);
}
function load(el, clip, kind) {
  if (el._clip === clip.id) return;
  el._clip = clip.id;
  const src = `/media/${encodeURIComponent(clip.asset_id)}${kind === "v" ? "?v=1" : ""}`;
  if (el._src !== src) { el._src = src; el.src = src; }
  try { el.currentTime = clip.source_in; } catch (_) { /* metadata not loaded yet; synced next frame */ }
}
function wants(track, clip, kind) {
  const a = assetOf(clip.asset_id);
  if (!a) return false;
  if (kind === "v") return track.kind === "video" && !track.hidden && clip.visible && a.has_video && !a.is_image;
  return !track.muted && a.has_audio && clip.volume * track.volume > 0;
}
function syncTrack(track, t, playing) {
  const active = activeClip(track, t);
  const xd = active ? crossfadeLen(active) : 0;
  const tail = xd && t < active.start + xd ? prevClip(track, active) : null;   // under a crossfade
  const next = tail ? null : (track.clips.find((c) => c.start > t && c.start - t < 1.5) || null);
  Live.tails.delete(track.id);
  for (const kind of track.kind === "video" ? ["v", "a"] : ["a"]) {
    const els = pool(track.id, kind);
    let cur = null;
    if (active && wants(track, active, kind)) {
      cur = els.find((e) => e._clip === active.id) || els.find((e) => !next || e._clip !== next.id) || els[0];
      load(cur, active, kind);
      const want = srcTime(active, t);
      const drift = want - cur.currentTime;                 // > 0: the element is behind the clock
      if (cur.readyState >= 1 && Math.abs(drift) > (playing ? 0.3 : 0.04)) cur.currentTime = want;
      // Small drifts are corrected by playing slightly faster/slower, which is inaudible.
      const nudge = playing && Math.abs(drift) > 0.02 ? Math.max(-0.15, Math.min(0.15, drift * 0.8)) : 0;
      cur.playbackRate = Math.max(0.0625, Math.min(16, active.speed * (1 + nudge)));
      if (kind === "a") cur.volume = Math.max(0, Math.min(1, active.volume * track.volume * clipFade(active, t)));
      if (playing && cur.paused) cur.play().catch(() => {});
      if (!playing && !cur.paused) cur.pause();
    }
    if (tail && wants(track, tail, kind)) {
      // The previous clip keeps playing (past its out point) and fades out under the new one.
      const tel = els.find((e) => e !== cur && e._clip === tail.id) || els.find((e) => e !== cur);
      if (tel) {
        load(tel, tail, kind);
        const want = srcTime(tail, t);
        if (tel.readyState >= 1 && Math.abs(tel.currentTime - want) > 0.3) tel.currentTime = want;
        if (playing && tel.paused) tel.play().catch(() => {});
        if (!playing && !tel.paused) tel.pause();
        if (kind === "a") tel.volume = Math.max(0, Math.min(1, tail.volume * track.volume * (1 - (t - active.start) / xd)));
        else Live.tails.set(track.id, { clip: tail, el: tel });
      }
      if (kind === "v") Live.frameEls.set(track.id, cur);
      continue;
    }
    const spare = next && wants(track, next, kind) ? els.find((e) => e !== cur) : null;
    for (const e of els) if (e !== cur && e !== spare && !e.paused) e.pause();
    if (spare) {
      load(spare, next, kind);
      const want = srcTime(next, t);
      if (playing && next.start - t < 0.35 && want >= 0) {
        // Pre-roll the next clip silently so it is already running in sync at the cut
        // (seeking while silent is inaudible, so it simply follows the clock).
        if (kind === "a") spare.volume = 0;
        if (spare.readyState >= 1 && Math.abs(spare.currentTime - want) > 0.04) spare.currentTime = want;
        if (spare.paused) spare.play().catch(() => {});
        spare.playbackRate = Math.max(0.0625, Math.min(16, next.speed));
      } else if (!spare.paused) {
        spare.pause();
      }
    }
    if (kind === "v") Live.frameEls.set(track.id, cur);
  }
}
function liveSync(t, playing) {
  const live = new Set(S.timeline.tracks.map((tr) => tr.id));
  for (const [key, els] of Live.pools) {          // tracks that no longer exist
    if (!live.has(key.split(":")[0])) els.forEach((e) => e.pause());
  }
  for (const track of S.timeline.tracks) syncTrack(track, t, playing);
}

// ---------------------------------------------------------------- drawing --
function imageFor(assetId) {
  if (!Live.images.has(assetId)) {
    const img = new Image();
    img.src = `/media/${encodeURIComponent(assetId)}`;
    Live.images.set(assetId, img);
  }
  return Live.images.get(assetId);
}
function hashKey(str) {
  let h = 5381;
  for (let i = 0; i < str.length; i++) h = ((h << 5) + h + str.charCodeAt(i)) | 0;
  return (h >>> 0).toString(36);
}
function titleFor(x, W, H) {
  const key = `${JSON.stringify(x)}|${W}x${H}`;
  if (!Live.titles.has(key)) {
    const img = new Image();
    img.src = `/api/textimg?id=${encodeURIComponent(x.id)}&w=${W}&h=${H}&k=${hashKey(key)}`;
    Live.titles.set(key, img);
  }
  return Live.titles.get(key);
}
function sizeCanvas() {
  const c = liveCanvas();
  const cv = S.timeline.canvas;
  const box = c.parentElement.getBoundingClientRect();
  const scale = Math.min(1, Math.min(box.width * devicePixelRatio, 1280) / cv.width);
  const w = Math.max(2, Math.round(cv.width * scale)), h = Math.max(2, Math.round(cv.height * scale));
  if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
}
function drawClip(g, track, clip, t, W, H, el, fullAlpha) {
  const a = assetOf(clip.asset_id);
  if (!a || !a.has_video) return;
  const src = a.is_image ? imageFor(a.id) : (el || Live.frameEls.get(track.id));
  if (!src) return;
  const sw = a.is_image ? src.naturalWidth : src.videoWidth;
  const sh = a.is_image ? src.naturalHeight : src.videoHeight;
  if (!sw || !sh || (!a.is_image && src.readyState < 2)) return;
  const tr = clip.transform;
  let cx = 0, cy = 0, cw = 1, ch = 1;
  if (tr.crop) {
    const k = (clip.zoom_in > 0 || clip.zoom_out > 0) ? zoomAmount(clip, t) : 1;
    cx = tr.crop.x * k; cy = tr.crop.y * k; cw = 1 + (tr.crop.w - 1) * k; ch = 1 + (tr.crop.h - 1) * k;
  }
  let sx = cx * sw, sy = cy * sh, sW = cw * sw, sH = ch * sh;
  const bx = tr.x * W, by = tr.y * H, bw = tr.w * W, bh = tr.h * H;
  let dx = bx, dy = by, dw = bw, dh = bh;
  if (tr.fit === "contain") {
    const s = Math.min(bw / sW, bh / sH);
    dw = sW * s; dh = sH * s; dx = bx + (bw - dw) / 2; dy = by + (bh - dh) / 2;
  } else if (tr.fit === "cover") {
    const s = Math.max(bw / sW, bh / sH);
    const vw = bw / s, vh = bh / s;
    sx += (sW - vw) / 2; sy += (sH - vh) / 2; sW = vw; sH = vh;
  }
  g.globalAlpha = tr.opacity * (fullAlpha ? 1 : clipFade(clip, t));
  g.filter = colorFilter(clip);
  g.drawImage(src, sx, sy, sW, sH, dx, dy, dw, dh);
  g.filter = "none";
  g.globalAlpha = 1;
}
function drawTitles(g, t, W, H) {
  for (const x of S.timeline.texts) {
    if (t < x.start || t >= x.end) continue;
    const img = titleFor(x, W, H);
    if (!img.complete || !img.naturalWidth) continue;
    const d = x.animation === "none" ? 0 : Math.min(0.45, (x.end - x.start) / 3);
    let alpha = 1, ox = 0, oy = 0;
    if (d > 0) {
      const k = Math.min(1, (t - x.start) / d, (x.end - t) / d);
      alpha = Math.max(0, k);
      const inK = Math.max(0, 1 - (t - x.start) / d);
      if (x.animation === "slide_up") oy = inK * H * 0.05;
      if (x.animation === "slide_left") ox = inK * W * 0.05;
    }
    g.globalAlpha = alpha;
    g.drawImage(img, ox, oy, W, H);
    g.globalAlpha = 1;
  }
}
function drawCaptions(g, t, W, H) {
  const cap = S.timeline.captions;
  if (!cap.enabled || !S.caption_events) return;
  let ev = null;
  for (let i = S.caption_events.length - 1; i >= 0; i--) {
    const e = S.caption_events[i];
    if (e[0] <= t && t < e[1]) { ev = e; break; }
  }
  if (!ev) return;
  const [, , texts, hi] = ev;
  const size = Math.max(10, Math.round(cap.size * H));
  g.font = `bold ${size}px "DejaVu Sans", "Helvetica Neue", Arial, sans-serif`;
  g.textBaseline = "alphabetic";
  const stroke = Math.max(1, size / 12);
  const space = g.measureText(" ").width;
  const lines = [[]];
  let lw = 0;
  texts.forEach((txt, i) => {
    const w = g.measureText(txt).width + 2 * stroke;
    if (lines[lines.length - 1].length && lw + space + w > W * 0.88) { lines.push([]); lw = 0; }
    lw += (lines[lines.length - 1].length ? space : 0) + w;
    lines[lines.length - 1].push([i, txt, w]);
  });
  const lineH = size * 1.25;
  let y = cap.y * H - lineH * (lines.length - 1) - size * 0.25;
  g.lineJoin = "round";
  for (const line of lines) {
    const total = line.reduce((s, [, , w]) => s + w, 0) + space * (line.length - 1);
    let x = (W - total) / 2;
    for (const [i, txt, w] of line) {
      g.lineWidth = stroke * 2;
      g.strokeStyle = cap.outline_color;
      g.strokeText(txt, x + stroke, y);
      g.fillStyle = i === hi ? cap.highlight_color : cap.color;
      g.fillText(txt, x + stroke, y);
      x += w + space;
    }
    y += lineH;
  }
}
function liveDraw(t) {
  const c = liveCanvas();
  const g = c.getContext("2d");
  const W = c.width, H = c.height;
  g.globalAlpha = 1;
  g.fillStyle = S.timeline.canvas.background || "black";
  g.fillRect(0, 0, W, H);
  for (const track of S.timeline.tracks) {                 // video tracks bottom layer first
    if (track.kind !== "video" || track.hidden) continue;
    const clip = activeClip(track, t);
    const tail = Live.tails.get(track.id);
    if (tail && tail.clip.visible) drawClip(g, track, tail.clip, t, W, H, tail.el, true);
    if (clip && clip.visible) drawClip(g, track, clip, t, W, H);
  }
  drawTitles(g, t, W, H);
  drawCaptions(g, t, W, H);
}

// ------------------------------------------------------------- transport --
function liveTime() { return Live.t0 + (performance.now() - Live.wall0) / 1000; }
function showLive(on) {
  liveCanvas().classList.toggle("hidden", !on);
  $("frame").classList.toggle("hidden", on);
  if (on) { $("video").classList.add("hidden"); $("video").pause(); }
}
function liveFrame() {
  if (!Live.playing) return;
  const t = liveTime();
  if (t >= S.duration - 1e-3) { liveStop(); setPlayhead(0); return; }
  liveSync(t, true);
  liveDraw(t);
  setPlayhead(t, true);
  followPlayhead();
  Live.raf = requestAnimationFrame(liveFrame);
}
function followPlayhead() {
  const sc = $("tlScroll");
  const x = 90 + playhead * pps;
  if (x > sc.scrollLeft + sc.clientWidth - 40 || x < sc.scrollLeft + 90) sc.scrollLeft = Math.max(0, x - 140);
}
async function liveStart() {
  if (!S || !S.timeline.tracks.length) { status("Önce timeline'a bir kayıt ekle", true); return; }
  if (playhead >= S.duration - 0.05) playhead = 0;
  $("tabFrame").classList.add("active");
  $("tabVideo").classList.remove("active");
  $("renderSelect").classList.add("hidden");
  $("dlLink").classList.add("hidden");
  $("btnReveal").classList.add("hidden");
  sizeCanvas();
  showLive(true);
  liveSync(playhead, false);
  liveDraw(playhead);
  // Give the media a moment to seek and buffer so playback starts cleanly.
  const t0 = performance.now();
  await new Promise((resolve) => {
    const check = () => {
      const els = [...Live.pools.values()].flat().filter((e) => e._clip && e.readyState < 3 &&
        S.timeline.tracks.some((tr) => { const c = activeClip(tr, playhead); return c && c.id === e._clip; }));
      if (!els.length || performance.now() - t0 > 900) resolve(); else setTimeout(check, 50);
    };
    check();
  });
  Live.t0 = playhead;
  Live.wall0 = performance.now();
  Live.playing = true;
  updatePlayButton();
  Live.raf = requestAnimationFrame(liveFrame);
}
function liveStop() {
  Live.playing = false;
  cancelAnimationFrame(Live.raf);
  for (const els of Live.pools.values()) els.forEach((e) => e.pause());
  showLive(false);
  updatePlayButton();
  requestStill();
}
function liveSeek(t) {
  Live.t0 = t;
  Live.wall0 = performance.now();
  liveSync(t, true);
}
