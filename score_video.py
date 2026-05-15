#!/usr/bin/env python3
"""
score_video.py — Generate a high-quality score video from a PDF and WAV file.

Usage:
    python score_video.py <score.pdf> <audio.wav> [output.mp4] [--dpi 200]

A browser window will open so you can set page-turn timestamps interactively.
No extra dependencies beyond pdf2image and Pillow (plus poppler and ffmpeg).
"""

import argparse
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
from urllib.parse import urlparse


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def fmt_time(seconds: float) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{int(h):d}:{int(m):02d}:{s:06.3f}"
    return f"{int(m):02d}:{s:06.3f}"


def get_audio_duration(wav_path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", wav_path],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        sys.exit(f"❌  Could not read audio duration from '{wav_path}'.")


def render_pdf_pages(pdf_path: str, dpi: int = 200) -> tuple[list[str], str]:
    try:
        from pdf2image import convert_from_path
    except ImportError:
        sys.exit("❌  pdf2image not installed. Run: pip install pdf2image")

    print(f"📄  Rendering PDF pages at {dpi} DPI …")
    tmpdir = tempfile.mkdtemp(prefix="score_video_pages_")
    images = convert_from_path(pdf_path, dpi=dpi, fmt="png", thread_count=4)
    paths = []
    for i, img in enumerate(images):
        p = os.path.join(tmpdir, f"page_{i+1:04d}.png")
        img.save(p, "PNG")
        paths.append(p)
        print(f"   Page {i+1}/{len(images)} rendered", end="\r", flush=True)
    print(f"   ✅  {len(images)} pages rendered.              ")
    return paths, tmpdir


def build_video(
    page_images: list[str],
    timestamps: list[float],
    wav_path: str,
    output_path: str,
    duration: float,
) -> None:
    tmpdir = tempfile.mkdtemp(prefix="score_concat_")
    concat_file = os.path.join(tmpdir, "concat.txt")

    with open(concat_file, "w") as f:
        for i, img_path in enumerate(page_images):
            seg_end = timestamps[i + 1] if i + 1 < len(timestamps) else duration
            seg_dur = max(seg_end - timestamps[i], 0.001)
            safe_path = img_path.replace("\\", "/")
            f.write(f"file '{safe_path}'\n")
            f.write(f"duration {seg_dur:.6f}\n")
        # Repeat last file without duration — required by concat demuxer syntax,
        # but the actual last-frame hold is handled by -vf tpad below.
        f.write(f"file '{page_images[-1].replace(chr(92), '/')}'\n")

    from PIL import Image
    w, h = Image.open(page_images[0]).size
    if w % 2: w += 1
    if h % 2: h += 1

    print(f"\n🎬  Encoding video ({w}×{h}) …")
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", concat_file,
        "-i", wav_path,
        "-vf", f"scale={w}:{h}:flags=lanczos,format=yuv420p",
        "-c:v", "libx264",
        "-preset", "slow",
        "-crf", "15",
        "-c:a", "aac",
        "-b:a", "320k",
        "-map", "0:v:0",
        "-map", "1:a:0",
        # Hard-trim to exact audio duration. This is more reliable than
        # -shortest, which can over-encode when the video stream is padded.
        "-t", f"{duration:.6f}",
        "-movflags", "+faststart",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    shutil.rmtree(tmpdir, ignore_errors=True)

    if result.returncode != 0:
        print("FFmpeg stderr:\n", result.stderr[-3000:])
        raise RuntimeError("FFmpeg encoding failed.")

    size_mb = os.path.getsize(output_path) / 1_048_576
    print(f"✅  Done!  →  {os.path.abspath(output_path)}  ({size_mb:.1f} MB)")


# ──────────────────────────────────────────────────────────────────────────────
# Browser UI
# ──────────────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Score Video — Page Turn Editor</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: 'Segoe UI', system-ui, sans-serif;
  background: #0f0f13; color: #e0e0e0;
  height: 100vh; overflow: hidden;
  display: grid;
  grid-template-rows: auto 1fr auto;
  grid-template-columns: 1fr 340px;
  grid-template-areas:
    "header  header"
    "preview controls"
    "footer  footer";
}

/* ── Header ── */
#header {
  grid-area: header;
  display: flex; align-items: baseline; gap: 1.2rem;
  padding: .6rem 1.2rem;
  background: #13131a; border-bottom: 1px solid #1e1e2e;
}
#header h1 { font-size: 1.2rem; color: #7dd3fc; white-space: nowrap; }
#header .sub { font-size: .82rem; color: #555; }

/* ── Preview (left, dominant) ── */
#preview {
  grid-area: preview;
  display: flex; flex-direction: column;
  background: #080810;
  overflow: hidden;
  position: relative;
}
#preview-label {
  position: absolute; top: .6rem; left: 50%; transform: translateX(-50%);
  background: rgba(0,0,0,.65); backdrop-filter: blur(4px);
  color: #7dd3fc; font-size: .78rem; font-weight: 600;
  padding: .25rem .7rem; border-radius: 20px;
  pointer-events: none; white-space: nowrap;
  border: 1px solid #2a2a4a;
}
#preview-img {
  width: 100%; height: 100%;
  object-fit: contain;
  display: block;
}
#preview-placeholder {
  width: 100%; height: 100%;
  display: flex; align-items: center; justify-content: center;
  color: #2a2a3a; font-size: 1rem;
}

/* ── Controls (right column) ── */
#controls {
  grid-area: controls;
  display: flex; flex-direction: column; gap: 0;
  border-left: 1px solid #1e1e2e;
  overflow: hidden;
}

/* Player section */
#player-section {
  padding: .9rem 1rem .7rem;
  border-bottom: 1px solid #1e1e2e;
  flex-shrink: 0;
}
.cur-time {
  font-size: 1.9rem; font-weight: 700; color: #fde68a;
  text-align: center; font-variant-numeric: tabular-nums;
  letter-spacing: .04em; margin-bottom: .5rem;
}
audio { width: 100%; margin-bottom: .6rem; }
.stamp-btn {
  width: 100%; padding: .6rem; background: #7dd3fc; color: #000;
  border: none; border-radius: 8px; font-size: .95rem; font-weight: 700;
  cursor: pointer; transition: background .15s;
}
.stamp-btn:hover { background: #38bdf8; }
.stamp-btn:active { background: #0ea5e9; transform: scale(.98); }
.hint { font-size: .72rem; color: #444; margin-top: .4rem; text-align: center; line-height: 1.4; }

/* Table section — scrollable */
#table-section {
  flex: 1 1 0;
  overflow-y: auto;
  overflow-x: hidden;
}
#table-section::-webkit-scrollbar { width: 6px; }
#table-section::-webkit-scrollbar-track { background: transparent; }
#table-section::-webkit-scrollbar-thumb { background: #2a2a3a; border-radius: 3px; }

table { width: 100%; border-collapse: collapse; font-size: .82rem; }
thead { position: sticky; top: 0; z-index: 2; background: #13131a; }
thead th {
  text-align: left; padding: .4rem .5rem;
  color: #7dd3fc; font-size: .75rem; font-weight: 600; letter-spacing: .04em;
  border-bottom: 1px solid #1e1e2e;
}
tbody tr { cursor: pointer; transition: background .08s; }
tbody tr:hover { background: #181825; }
tbody tr.sel { background: #1a2540; }
td { padding: .3rem .5rem; vertical-align: middle; }
td.pn { color: #444; width: 2.2rem; font-size: .75rem; }
td.tc input {
  background: #111; border: 1px solid #2a2a3a; color: #e0e0e0;
  border-radius: 5px; padding: .22rem .45rem; font-size: .8rem; width: 108px;
  font-family: 'Consolas', 'Courier New', monospace; transition: border-color .15s;
}
td.tc input:focus { outline: none; border-color: #7dd3fc; background: #0d0d18; }
td.tc input.locked { background: transparent; border-color: #1a1a2a; color: #3a3a4a; cursor: default; }
td.tc input.err { border-color: #f87171; }
td.dc { color: #6ee7b7; font-family: 'Consolas', monospace; font-size: .75rem; width: 5.5rem; }
td.sc { width: 2.8rem; }
td.sc button {
  padding: .18rem .4rem; background: #1a1a2e; border: 1px solid #2a2a3a;
  color: #7dd3fc; border-radius: 4px; cursor: pointer; font-size: .7rem;
  transition: background .12s; white-space: nowrap;
}
td.sc button:hover { background: #1e2a40; }
td.sc button.used { background: #0d2b1a; border-color: #166534; color: #6ee7b7; }
td.cc { width: 2rem; }
td.cc .clear-btn {
  padding: .18rem .38rem; background: transparent; border: 1px solid #2a2a3a;
  color: #3a3a4a; border-radius: 4px; cursor: pointer; font-size: .7rem;
  transition: all .12s;
}
td.cc .clear-btn:hover { background: #2d0f0f; border-color: #7f1d1d; color: #f87171; }
td.cc .clear-btn.active { color: #f87171; border-color: #7f1d1d; }

/* ── Footer ── */
#footer {
  grid-area: footer;
  display: flex; align-items: center; gap: 1rem;
  padding: .5rem 1rem;
  background: #13131a; border-top: 1px solid #1e1e2e;
}
#status {
  flex: 1; font-size: .82rem; padding: .4rem .8rem;
  border-radius: 6px; border-left: 3px solid #facc15; background: #1a1700; color: #fde68a;
}
#status.ok  { border-color: #4ade80; background: #0a1f0a; color: #86efac; }
#status.err { border-color: #f87171; background: #1f0a0a; color: #fca5a5; }
.gen-btn {
  flex-shrink: 0; padding: .55rem 1.4rem;
  background: #4ade80; color: #000; border: none; border-radius: 8px;
  font-size: .92rem; font-weight: 700; cursor: pointer;
  transition: background .15s, opacity .15s;
  white-space: nowrap;
}
.gen-btn:disabled { opacity: .3; cursor: not-allowed; }
.gen-btn:not(:disabled):hover { background: #22c55e; }
</style>
</head>
<body>

<div id="header">
  <h1>🎵 Score Video — Page Turn Editor</h1>
  <span class="sub" id="sub"></span>
</div>

<!-- Left: large preview -->
<div id="preview" tabindex="-1">
  <div id="preview-label" style="display:none"></div>
  <div id="preview-placeholder">← select a page to preview</div>
  <img id="preview-img" src="" alt="" style="display:none">
</div>

<!-- Right: controls + table -->
<div id="controls">

  <div id="player-section">
    <div class="cur-time" id="curTime">00:00.000</div>
    <audio id="audio" controls src="/audio"></audio>
    <button class="stamp-btn" onclick="stampSel()">⏱ Stamp selected page &nbsp;[Space]</button>
    <p class="hint">Select a row · play audio · Space to stamp · Delete to clear.<br>Preview shows the <em>previous</em> page (what a performer would see before turning).</p>
  </div>

  <div id="table-section">
    <table>
      <thead><tr>
        <th>Pg</th><th>Turn at</th><th>Dur</th><th></th><th></th>
      </tr></thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>

</div>

<!-- Footer -->
<div id="footer">
  <div id="status">Set timestamps for all pages, then generate.</div>
  <button class="gen-btn" id="genBtn" disabled onclick="generate()">🎬 Generate</button>
</div>

<script>
const N   = __N_PAGES__;
const DUR = __DURATION__;
const audio = document.getElementById('audio');
document.getElementById('sub').textContent = `${N} pages  •  ${fmtTime(DUR)}`;

let sel = 1;
const ts = new Array(N).fill(null);
ts[0] = 0.0;

// ── Helpers ──────────────────────────────────────────────────
function fmtTime(s) {
  const m = Math.floor(s / 60), sec = (s % 60).toFixed(3).padStart(6, '0');
  return `${String(m).padStart(2,'0')}:${sec}`;
}
function parseTime(str) {
  str = str.trim(); if (!str) return null;
  const p = str.split(':');
  try {
    if (p.length === 1) return parseFloat(p[0]);
    if (p.length === 2) return parseInt(p[0]) * 60 + parseFloat(p[1]);
    if (p.length === 3) return parseInt(p[0]) * 3600 + parseInt(p[1]) * 60 + parseFloat(p[2]);
  } catch(e) {}
  return null;
}
function validate(i, v) {
  if (v === null || isNaN(v)) return 'Invalid time';
  if (v < 0 || v > DUR)       return `Must be 0–${fmtTime(DUR)}`;
  const prev = ts[i-1]; if (prev !== null && v <= prev) return `Must be after page ${i} (${fmtTime(prev)})`;
  const next = ts[i+1]; if (next !== null && v >= next) return `Must be before page ${i+2} (${fmtTime(next)})`;
  return null;
}

// ── Preview ───────────────────────────────────────────────────
// When row i is selected, show the PREVIOUS page (i-1), mimicking
// what a performer sees on their stand before they turn the page.
function showPreview(i) {
  const previewIdx = Math.max(0, i - 1);
  const img   = document.getElementById('preview-img');
  const ph    = document.getElementById('preview-placeholder');
  const label = document.getElementById('preview-label');

  img.src = `/thumb/${previewIdx}`;
  img.style.display = 'block';
  ph.style.display  = 'none';
  label.style.display = 'block';

  if (i === 0) {
    label.textContent = `Page 1 (first page)`;
  } else {
    label.textContent = `Page ${previewIdx + 1} — turn to page ${i + 1} here`;
  }
}

// ── Table ─────────────────────────────────────────────────────
function buildTable() {
  const tbody = document.getElementById('tbody');
  for (let i = 0; i < N; i++) {
    const locked = i === 0;
    const tr = document.createElement('tr');
    tr.dataset.i = i;
    tr.onclick = () => selectRow(i);
    tr.innerHTML = `
      <td class="pn">${i+1}</td>
      <td class="tc"><input id="inp${i}" type="text"
        value="${locked ? fmtTime(0) : ''}"
        placeholder="${locked ? '' : 'mm:ss.mmm'}"
        ${locked ? 'readonly class="locked"' : ''}
        oninput="onInp(${i})" onblur="onBlur(${i})" onkeydown="onKey(event,${i})"></td>
      <td class="dc" id="dur${i}">—</td>
      <td class="sc">${locked ? '' : `<button id="sb${i}" onclick="stampRow(${i},event)">⏱</button>`}</td>
      <td class="cc">${locked ? '' : `<button id="cb${i}" class="clear-btn" onclick="clearRow(${i},event)" title="Clear timestamp">✕</button>`}</td>`;
    tbody.appendChild(tr);
  }
  selectRow(1);
  refresh();
}

function selectRow(i) {
  document.querySelectorAll('#tbody tr').forEach(r => r.classList.remove('sel'));
  const tr = document.querySelector(`#tbody tr[data-i="${i}"]`);
  if (tr) {
    tr.classList.add('sel');
    tr.scrollIntoView({ block: 'nearest' });
  }
  sel = i;
  showPreview(i);
}

// ── Input handlers ────────────────────────────────────────────
function onInp(i) {
  const v = parseTime(document.getElementById(`inp${i}`).value);
  ts[i] = (v !== null && !isNaN(v)) ? v : null;
  document.getElementById(`inp${i}`).classList.remove('err');
  refresh();
}
function onBlur(i) {
  const inp = document.getElementById(`inp${i}`);
  const v = parseTime(inp.value);
  if (inp.value && validate(i, v)) {
    inp.classList.add('err');
  } else if (v !== null && !isNaN(v)) {
    inp.value = fmtTime(v);
    inp.classList.remove('err');
    ts[i] = v;
  }
  refresh();
}
function onKey(e, i) {
  if (e.key === 'Enter') {
    onBlur(i);
    const next = Math.min(i + 1, N - 1);
    selectRow(next);
    document.getElementById(`inp${next}`)?.focus();
  }
}

// ── Stamping ──────────────────────────────────────────────────
function stampSel() { stampRow(sel); }
function stampRow(i, e) {
  if (e) e.stopPropagation();
  if (i === 0) return;
  const wasSet = ts[i] !== null;
  ts[i] = audio.currentTime;
  const inp = document.getElementById(`inp${i}`);
  inp.value = fmtTime(ts[i]);
  inp.classList.remove('err');
  document.getElementById(`sb${i}`)?.classList.add('used');
  document.getElementById(`cb${i}`)?.classList.add('active');
  // Only auto-advance to next row if this was a fresh stamp (not an overwrite)
  if (!wasSet && i < N - 1) selectRow(i + 1);
  refresh();
}

// ── Clearing ──────────────────────────────────────────────────
function clearSel() { clearRow(sel); }
function clearRow(i, e) {
  if (e) e.stopPropagation();
  if (i === 0) return;
  ts[i] = null;
  const inp = document.getElementById(`inp${i}`);
  inp.value = '';
  inp.classList.remove('err');
  document.getElementById(`sb${i}`)?.classList.remove('used');
  document.getElementById(`cb${i}`)?.classList.remove('active');
  refresh();
}

// ── Keyboard ──────────────────────────────────────────────────
// Intercept Space on the audio element in the capture phase, before
// the browser can handle it as play/pause.
audio.addEventListener('keydown', e => {
  if (e.code === 'Space') {
    e.preventDefault();
    e.stopImmediatePropagation();
    stampSel();
  }
}, true);

document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  if (e.code === 'Space') {
    e.preventDefault();
    audio.blur();
    document.getElementById('preview').focus();
    stampSel();
  }
  if (e.code === 'Delete' || e.code === 'Backspace') { e.preventDefault(); clearSel(); }
  if (e.code === 'ArrowDown') { e.preventDefault(); selectRow(Math.min(sel + 1, N - 1)); }
  if (e.code === 'ArrowUp')   { e.preventDefault(); selectRow(Math.max(sel - 1, 0)); }
});

// After clicking play/pause, pull focus away from the audio element
// immediately so Space routes to our handler next time.
audio.addEventListener('play',  () => { audio.blur(); document.getElementById('preview').focus(); });
audio.addEventListener('pause', () => { audio.blur(); document.getElementById('preview').focus(); });

audio.addEventListener('timeupdate', () => {
  document.getElementById('curTime').textContent = fmtTime(audio.currentTime);
});

// ── Refresh / validate ────────────────────────────────────────
function refresh() {
  for (let i = 0; i < N; i++) {
    const t0 = ts[i], t1 = (i + 1 < N ? ts[i+1] : DUR);
    document.getElementById(`dur${i}`).textContent =
      (t0 !== null && t1 !== null) ? fmtTime(t1 - t0) : '—';
  }
  const missing = [];
  for (let i = 1; i < N; i++) {
    if (ts[i] === null) { missing.push(i + 1); continue; }
    const err = validate(i, ts[i]);
    document.getElementById(`inp${i}`)?.classList.toggle('err', !!err);
    if (err) missing.push(i + 1);
  }
  const bar = document.getElementById('status');
  const btn = document.getElementById('genBtn');
  if (missing.length === 0) {
    bar.className = 'ok';
    bar.textContent = '✅ All timestamps set — ready to generate!';
    btn.disabled = false;
  } else {
    bar.className = '';
    bar.textContent = `⚠ ${missing.length} page(s) unset: ${missing.slice(0,6).join(', ')}${missing.length > 6 ? '…' : ''}`;
    btn.disabled = true;
  }
}

// ── Generate ──────────────────────────────────────────────────
async function generate() {
  document.getElementById('genBtn').disabled = true;
  const bar = document.getElementById('status');
  bar.className = '';
  bar.textContent = '⏳ Encoding… check your terminal for progress.';
  try {
    const r = await fetch('/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ timestamps: ts })
    });
    const d = await r.json();
    if (d.ok) {
      bar.className = 'ok';
      bar.textContent = `✅ Saved → ${d.output}`;
    } else {
      bar.className = 'err';
      bar.textContent = `❌ ${d.error}`;
      document.getElementById('genBtn').disabled = false;
    }
  } catch(err) {
    bar.className = 'err';
    bar.textContent = `❌ Network error: ${err}`;
    document.getElementById('genBtn').disabled = false;
  }
}

buildTable();
</script>
</body></html>
"""


def launch_browser_ui(
    page_images: list[str],
    wav_path: str,
    output_path: str,
    duration: float,
) -> None:
    result_holder: dict = {}
    server_ref: dict = {}

    html = (HTML
            .replace("__N_PAGES__", str(len(page_images)))
            .replace("__DURATION__", f"{duration:.6f}"))

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                self._send(200, "text/html; charset=utf-8", html.encode())
            elif path == "/audio":
                self._send_file(wav_path, "audio/wav")
            elif path.startswith("/thumb/"):
                try:
                    idx = int(path.rsplit("/", 1)[-1])
                    self._send_file(page_images[idx], "image/png")
                except (ValueError, IndexError):
                    self._send(404, "text/plain", b"not found")
            else:
                self._send(404, "text/plain", b"not found")

        def do_POST(self):
            if self.path == "/generate":
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                timestamps = body["timestamps"]
                try:
                    build_video(page_images, timestamps, wav_path, output_path, duration)
                    result_holder["ok"] = True
                    result_holder["output"] = os.path.abspath(output_path)
                except Exception as exc:
                    result_holder["ok"] = False
                    result_holder["error"] = str(exc)
                self._send(200, "application/json", json.dumps(result_holder).encode())
                threading.Thread(target=lambda: (
                    __import__("time").sleep(0.5),
                    server_ref["srv"].shutdown()
                ), daemon=True).start()
            else:
                self._send(404, "text/plain", b"not found")

        def _send(self, code, ctype, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: str, ctype: str):
            with open(path, "rb") as f:
                data = f.read()
            self._send(200, ctype, data)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_ref["srv"] = server
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"

    print(f"\n🌐  Browser UI →  {url}")
    print("    (If it doesn't open automatically, paste the URL above into your browser.)")
    print("    Set all page-turn timestamps, then click  🎬 Generate Video.\n")
    threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    server.serve_forever()

    if not result_holder.get("ok"):
        sys.exit(f"❌  Encoding failed: {result_holder.get('error', 'unknown')}")


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate a high-quality score video from a PDF + WAV.",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("pdf",    help="Score PDF path")
    parser.add_argument("audio",  help="Audio WAV path")
    parser.add_argument("output", nargs="?", default="score_video.mp4",
                        help="Output MP4 (default: score_video.mp4)")
    parser.add_argument("--dpi",  type=int, default=200,
                        help="PDF render DPI (default: 200; use 300 for sharper output)")
    args = parser.parse_args()

    for p, label in [(args.pdf, "PDF"), (args.audio, "WAV")]:
        if not os.path.isfile(p):
            sys.exit(f"❌  {label} file not found: {p}")

    duration = get_audio_duration(args.audio)
    print(f"🎵  Audio duration: {fmt_time(duration)}")

    page_images, tmpdir = render_pdf_pages(args.pdf, dpi=args.dpi)
    try:
        if len(page_images) == 1:
            print("Single-page score — no timestamps needed.")
            build_video(page_images, [0.0], args.audio, args.output, duration)
        else:
            launch_browser_ui(page_images, args.audio, args.output, duration)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()