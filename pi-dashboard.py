#!/usr/bin/env python3
"""
Lightweight Pi Monitoring Dashboard
Serves a web UI showing system metrics and UPS battery status.

Reads system info from /proc, /sys, and UPS data from /run/ups_status.json
(written by ups-battery-monitor.py).

Requires: pip3 install bottle
Usage:    python3 pi-dashboard.py
Web UI:   http://<pi-ip>:8585
"""

import json
import socket
import subprocess
from datetime import datetime
from functools import lru_cache

from bottle import Bottle, response

# ================== CONFIG ==================
PORT = 8585
HOST = "0.0.0.0"
REFRESH_INTERVAL = 5          # seconds (frontend auto-refresh)
UPS_STATUS_FILE = "/run/ups_status.json"
DISK_PATH = "/media/storage"  # external SSD
TEMP_WARNING = 70             # °C — yellow
TEMP_CRITICAL = 80            # °C — red

# ================== APP ==================
app = Bottle()


# ================== METRIC READERS ==================

@lru_cache(maxsize=1)
def read_cpu_count():
    """Count CPU cores (cached)."""
    n = 0
    with open("/proc/cpuinfo", encoding="utf-8") as f:
        for line in f:
            if line.startswith("processor"):
                n += 1
    return n or 1


def read_load():
    """Read load averages and derive a usage percentage."""
    with open("/proc/loadavg", encoding="utf-8") as f:
        p = f.read().split()
    load1, load5, load15 = float(p[0]), float(p[1]), float(p[2])
    cores = read_cpu_count()
    pct = round(min(load1 / cores, 1.0) * 100, 1)
    return {
        "load_1m": load1,
        "load_5m": load5,
        "load_15m": load15,
        "cores": cores,
        "percent": pct,
    }


def read_memory():
    """Parse /proc/meminfo for RAM usage."""
    info = {}
    with open("/proc/meminfo", encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            info[parts[0].rstrip(":")] = int(parts[1])  # kB
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)
    used = total - available
    return {
        "total_mb": round(total / 1024, 1),
        "used_mb": round(used / 1024, 1),
        "available_mb": round(available / 1024, 1),
        "percent": round(used / total * 100, 1) if total else 0,
    }


def read_temperature():
    """Read CPU temperature from thermal zone."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", encoding="utf-8") as f:
            temp = int(f.read().strip()) / 1000.0
        if temp >= TEMP_CRITICAL:
            status = "critical"
        elif temp >= TEMP_WARNING:
            status = "warning"
        else:
            status = "normal"
        return {"celsius": round(temp, 1), "status": status}
    except (FileNotFoundError, ValueError):
        return {"celsius": None, "status": "unavailable"}


def read_disk():
    """Get disk usage for external SSD via df.

    Returns None when DISK_PATH is not a dedicated mount point (i.e. it
    falls back to the root filesystem), so we don't misreport the SD card
    as the external SSD.
    """
    try:
        r = subprocess.run(
            ["df", "-B1", DISK_PATH],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if r.returncode != 0:
            return None
        parts = r.stdout.strip().split("\n")[1].split()
        device = parts[0]
        mount_point = parts[5]

        if mount_point != DISK_PATH:
            return None

        total = int(parts[1])
        used = int(parts[2])
        avail = int(parts[3])
        return {
            "mount": DISK_PATH,
            "device": device,
            "total_gb": round(total / 1073741824, 1),
            "used_gb": round(used / 1073741824, 1),
            "available_gb": round(avail / 1073741824, 1),
            "percent": round(used / total * 100, 1) if total else 0,
        }
    except Exception:
        return None


def read_uptime():
    """Human-readable uptime from /proc/uptime."""
    try:
        with open("/proc/uptime", encoding="utf-8") as f:
            sec = float(f.read().split()[0])
        d = int(sec // 86400)
        h = int(sec % 86400 // 3600)
        m = int(sec % 3600 // 60)
        parts = []
        if d:
            parts.append(f"{d}d")
        if h:
            parts.append(f"{h}h")
        parts.append(f"{m}m")
        return {"text": " ".join(parts), "seconds": int(sec)}
    except Exception:
        return {"text": "unknown", "seconds": 0}


def read_ups():
    """Read UPS status JSON written by ups-battery-monitor.py."""
    try:
        with open(UPS_STATUS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        data["available"] = True
        return data
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return {"available": False}


_INTERPRETERS = {"python", "python3", "python2", "java", "node", "nodejs",
                 "perl", "ruby", "bash", "sh", "zsh"}


def _friendly_name(args_str):
    """Derive a human-useful label from a full command line.

    For interpreter-based processes (python3, node, java …) the bare binary
    name is useless, so we pick the first argument that looks like a script
    or module name instead.  Everything else just gets the basename of argv[0].
    """
    tokens = args_str.split()
    if not tokens:
        return args_str
    base = tokens[0].rsplit("/", 1)[-1]
    if base in _INTERPRETERS and len(tokens) > 1:
        for tok in tokens[1:]:
            if tok.startswith("-"):
                continue
            # "python3 -m streamlit run app.py" → "streamlit"
            if tok == "-m" or tok == "-u":
                continue
            return tok.rsplit("/", 1)[-1]
    return base


def read_top_processes(sort_key="cpu", count=10):
    """Return top processes sorted by CPU or memory usage.

    Uses ``ps`` with ``args`` instead of ``comm`` so we can extract a
    meaningful name for interpreter-based processes (python3, node, etc.).
    """
    flag = "-%cpu" if sort_key == "cpu" else "-%mem"
    try:
        r = subprocess.run(
            ["ps", "-eo", "pid,%cpu,%mem,args", "--sort", flag, "--no-headers"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if r.returncode != 0:
            return []
        procs = []
        for line in r.stdout.strip().splitlines()[:count]:
            parts = line.split(None, 3)
            if len(parts) < 4:
                continue
            procs.append({
                "pid": int(parts[0]),
                "cpu": float(parts[1]),
                "mem": float(parts[2]),
                "name": _friendly_name(parts[3]),
            })
        return procs
    except Exception:
        return []


# ================== ROUTES ==================

@app.route("/api/stats")
def api_stats():
    """JSON API — all metrics in one call."""
    response.content_type = "application/json"
    return json.dumps({
        "cpu": read_load(),
        "memory": read_memory(),
        "temperature": read_temperature(),
        "disk": read_disk(),
        "uptime": read_uptime(),
        "ups": read_ups(),
        "top_cpu": read_top_processes("cpu"),
        "top_mem": read_top_processes("mem"),
        "hostname": socket.gethostname(),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.route("/")
def index():
    """Serve the single-page dashboard."""
    return DASHBOARD_HTML


# ================== EMBEDDED DASHBOARD HTML ==================

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pi Dashboard</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d1117;--bg-card:#161b22;
  --text:#e6edf3;--text2:#8b949e;--text3:#6e7681;
  --border:#30363d;--bar-bg:#21262d;
  --blue:#58a6ff;--green:#3fb950;--yellow:#d29922;
  --red:#f85149;
}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",
  Helvetica,Arial,sans-serif;
  background:var(--bg);color:var(--text);line-height:1.5;
  min-height:100vh}

/* header */
.hdr{padding:20px 24px 12px;display:flex;align-items:baseline;
  flex-wrap:wrap;gap:8px 24px;border-bottom:1px solid var(--border)}
.hdr h1{font-size:20px;font-weight:600;white-space:nowrap}
.hdr .meta{font-size:13px;color:var(--text2)}
.hdr .dot{width:8px;height:8px;border-radius:50%;
  display:inline-block;margin-right:6px;
  background:var(--green);vertical-align:middle}
.hdr .dot.err{background:var(--red)}
.hdr .spacer{flex:1}

/* grid — 3 columns on wide, 2 on medium, 1 on narrow */
.grid{display:grid;
  grid-template-columns:repeat(3,1fr);
  gap:16px;padding:20px 24px 32px}

/* card */
.card{background:var(--bg-card);border:1px solid var(--border);
  border-radius:12px;padding:20px;
  display:flex;flex-direction:column;gap:12px;
  transition:border-color .2s}
.card:hover{border-color:var(--text3)}
.card-title{font-size:12px;font-weight:600;
  text-transform:uppercase;letter-spacing:.05em;
  color:var(--text2);display:flex;align-items:center;gap:8px}
.card-title .icon{font-size:16px}

/* gauge ring */
.gauge-wrap{display:flex;align-items:center;gap:16px}
.gauge{width:100px;height:100px;flex-shrink:0}
.gauge-bg{fill:none;stroke:var(--bar-bg);stroke-width:10}
.gauge-fill{fill:none;stroke-width:10;stroke-linecap:round;
  transform:rotate(-90deg);transform-origin:center;
  transition:stroke-dashoffset .6s ease,stroke .4s}
.gauge-text{fill:var(--text);font-size:22px;font-weight:700;
  text-anchor:middle;dominant-baseline:central}
.gauge-sub{fill:var(--text2);font-size:11px;text-anchor:middle}
.info{display:flex;flex-direction:column;gap:4px;
  font-size:13px;color:var(--text2)}
.info .v{color:var(--text);font-weight:500}

/* bar */
.bar-wrap{width:100%}
.bar-lbl{display:flex;justify-content:space-between;
  font-size:13px;color:var(--text2);margin-bottom:4px}
.bar-lbl .v{color:var(--text);font-weight:500}
.bar{height:8px;border-radius:4px;background:var(--bar-bg);
  overflow:hidden}
.bar-fill{height:100%;border-radius:4px;
  transition:width .6s ease,background .4s}

/* badge */
.badge{display:inline-flex;align-items:center;gap:4px;
  padding:2px 10px;border-radius:12px;
  font-size:11px;font-weight:600;text-transform:uppercase;
  letter-spacing:.03em}
.badge.green{background:rgba(63,185,80,.15);color:var(--green)}
.badge.yellow{background:rgba(210,153,34,.15);color:var(--yellow)}
.badge.red{background:rgba(248,81,73,.15);color:var(--red)}
.badge.muted{background:rgba(110,118,129,.15);color:var(--text3)}

/* ups power source — prominent */
.ups-src{font-size:15px;font-weight:600;display:flex;
  align-items:center;gap:8px}

/* process table */
.ptable{width:100%;border-collapse:collapse;font-size:12px}
.ptable th{text-align:left;padding:4px 8px;color:var(--text2);
  font-weight:600;border-bottom:1px solid var(--border);
  white-space:nowrap}
.ptable th.r,.ptable td.r{text-align:right}
.ptable td{padding:4px 8px;border-bottom:1px solid var(--border);
  white-space:nowrap}
.ptable tr:last-child td{border-bottom:none}
.ptable .pname{max-width:120px;overflow:hidden;
  text-overflow:ellipsis;color:var(--text)}
.ptable .pbar{width:60px;height:6px;border-radius:3px;
  background:var(--bar-bg);display:inline-block;vertical-align:middle}
.ptable .pbar-fill{height:100%;border-radius:3px}
.card.wide{grid-column:span 1}

/* local services */
.svc-list{display:flex;flex-wrap:wrap;gap:8px}
.svc-link{flex:1 1 140px;display:flex;align-items:center;gap:10px;
  padding:10px 12px;border-radius:8px;
  color:var(--text);text-decoration:none;
  border:1px solid var(--border);background:var(--bar-bg);
  font-size:13px;font-weight:500;
  transition:background .15s,border-color .15s,color .15s}
.svc-link .svc-icon{font-size:18px;line-height:1;flex-shrink:0}
.svc-link:hover{border-color:var(--blue);color:var(--blue)}
.svc-link .arrow{margin-left:auto;font-size:11px;color:var(--text3)}
.svc-link:hover .arrow{color:var(--blue)}
.card.services{grid-column:1/-1}

/* footer */
.footer{text-align:center;padding:0 24px 20px;
  font-size:12px;color:var(--text3)}

@media(min-width:1200px){
  .grid{grid-template-columns:repeat(4,1fr)}
}
@media(max-width:900px){
  .grid{grid-template-columns:repeat(2,1fr)}
}
@media(max-width:560px){
  .grid{grid-template-columns:1fr;padding:12px}
  .hdr{padding:16px 12px 10px}
  .gauge{width:80px;height:80px}
  .gauge-text{font-size:18px}
}
</style>
</head>
<body>

<div class="hdr">
  <h1 id="hostname">Pi Dashboard</h1>
  <div class="spacer"></div>
  <div class="meta">
    <span class="dot" id="dot"></span>
    <span id="status">connecting...</span>
  </div>
  <div class="meta" id="uptime"></div>
</div>

<div class="grid" id="grid"></div>
<div class="footer" id="footer"></div>

<script>
const REFRESH = __REFRESH__ * 1000;
const C = 2 * Math.PI * 45;

/* — helpers — */
function gOff(pct) {
  return C * (1 - Math.min(Math.max(pct,0),100)/100);
}
function battColor(pct) {
  if (pct < 30) return 'var(--red)';
  if (pct < 75) return 'var(--yellow)';
  return 'var(--green)';
}
function usageColor(pct) {
  if (pct >= 90) return 'var(--red)';
  if (pct >= 75) return 'var(--yellow)';
  return 'var(--blue)';
}
function tempColor(s) {
  if (s==='critical') return 'var(--red)';
  if (s==='warning')  return 'var(--yellow)';
  return 'var(--green)';
}
function tempBadge(s) {
  const m={normal:'green',warning:'yellow',
    critical:'red',unavailable:'muted'};
  return '<span class="badge '+(m[s]||'muted')+'">'+s+'</span>';
}
function esc(s) {
  const d=document.createElement('div');
  d.textContent=s; return d.innerHTML;
}
function $(id) { return document.getElementById(id); }

/* — gauge SVG snippet — */
function gaugeSvg(id, pct, color, label) {
  return `<svg class="gauge" viewBox="0 0 100 100">
    <circle class="gauge-bg" cx="50" cy="50" r="45"/>
    <circle class="gauge-fill" id="${id}" cx="50" cy="50" r="45"
      stroke-dasharray="${C}"
      stroke-dashoffset="${gOff(pct)}"
      stroke="${color}"/>
    <text class="gauge-text" x="50" y="44"
      id="${id}-t">${Math.round(pct)}%</text>
    <text class="gauge-sub" x="50" y="66">${label}</text>
  </svg>`;
}

/* — card builders — */
function mkRAM(m) {
  const c = document.createElement('div');
  c.className = 'card';
  c.innerHTML = `
    <div class="card-title">
      <span class="icon">&#9619;</span> Memory
    </div>
    <div class="gauge-wrap">
      ${gaugeSvg('ram',m.percent,usageColor(m.percent),'used')}
      <div class="info">
        <div>Used: <span class="v" id="ram-u">
          ${(m.used_mb/1024).toFixed(1)} GB</span></div>
        <div>Free: <span class="v" id="ram-f">
          ${(m.available_mb/1024).toFixed(1)} GB</span></div>
        <div>Total: <span class="v">
          ${(m.total_mb/1024).toFixed(1)} GB</span></div>
      </div>
    </div>`;
  return c;
}

function mkCPU(cpu) {
  const c = document.createElement('div');
  c.className = 'card';
  c.innerHTML = `
    <div class="card-title">
      <span class="icon">&#9881;</span> CPU Load
    </div>
    <div class="gauge-wrap">
      ${gaugeSvg('cpu',cpu.percent,usageColor(cpu.percent),'load')}
      <div class="info">
        <div>1 min: <span class="v" id="l1">
          ${cpu.load_1m}</span></div>
        <div>5 min: <span class="v" id="l5">
          ${cpu.load_5m}</span></div>
        <div>15 min: <span class="v" id="l15">
          ${cpu.load_15m}</span></div>
        <div>Cores: <span class="v">${cpu.cores}</span></div>
      </div>
    </div>`;
  return c;
}

function mkTemp(t) {
  const val = t.celsius !== null ? t.celsius+'°C' : 'N/A';
  const col = tempColor(t.status);
  const pct = t.celsius !== null ? Math.min(t.celsius,100) : 0;
  const c = document.createElement('div');
  c.className = 'card';
  c.innerHTML = `
    <div class="card-title">
      <span class="icon">&#127777;</span> Temperature
    </div>
    <div class="gauge-wrap">
      <svg class="gauge" viewBox="0 0 100 100">
        <circle class="gauge-bg" cx="50" cy="50" r="45"/>
        <circle class="gauge-fill" id="tmp" cx="50" cy="50" r="45"
          stroke-dasharray="${C}"
          stroke-dashoffset="${gOff(pct)}"
          stroke="${col}"/>
        <text class="gauge-text" x="50" y="44"
          id="tmp-t" fill="${col}">${val}</text>
        <text class="gauge-sub" x="50" y="66">CPU</text>
      </svg>
      <div class="info">
        <div>Status: <span id="tmp-s">${tempBadge(t.status)}</span>
        </div>
        <div style="font-size:11px;color:var(--text3)">
          Warn __TW__° / Crit __TC__°</div>
      </div>
    </div>`;
  return c;
}

function mkDisk(d) {
  if (!d) {
    const c = document.createElement('div');
    c.className = 'card';
    c.innerHTML = `
      <div class="card-title">
        <span class="icon">&#128190;</span> External SSD
      </div>
      <div class="info">
        <span class="badge muted">not mounted</span>
      </div>`;
    return c;
  }
  const c = document.createElement('div');
  c.className = 'card';
  c.innerHTML = `
    <div class="card-title">
      <span class="icon">&#128190;</span>
      External SSD
    </div>
    <div class="bar-wrap">
      <div class="bar-lbl">
        <span id="dk-lbl">${d.used_gb} / ${d.total_gb} GB</span>
        <span class="v" id="dk-pct">${d.percent}%</span>
      </div>
      <div class="bar"><div class="bar-fill" id="dk-bar"
        style="width:${d.percent}%;
          background:${usageColor(d.percent)}">
      </div></div>
    </div>
    <div class="info">
      <div>Free: <span class="v" id="dk-free">
        ${d.available_gb} GB</span></div>
      <div style="font-size:11px;color:var(--text3)">
        ${esc(d.device)} &rarr; ${esc(d.mount)}</div>
    </div>`;
  return c;
}

function mkUPSStatus(ups) {
  const c = document.createElement('div');
  c.className = 'card';
  if (!ups.available) {
    c.innerHTML = `
      <div class="card-title">
        <span class="icon">&#9889;</span> UPS Power
      </div>
      <div class="info">
        <span class="badge muted">unavailable</span>
      </div>`;
    return c;
  }
  const onBatt = ups.on_battery;
  c.innerHTML = `
    <div class="card-title">
      <span class="icon">&#9889;</span> UPS Power
    </div>
    <div class="ups-src" id="ups-src">${srcHtml(onBatt)}</div>
    <div class="info">
      <div>Voltage: <span class="v" id="ups-v">
        ${(ups.voltage||0).toFixed(2)} V</span></div>
      <div>Current: <span class="v" id="ups-ma">
        ${(ups.current_ma||0).toFixed(0)} mA</span></div>
      <div style="font-size:11px;color:var(--text3)"
        id="ups-ts">${ups.timestamp||''}</div>
    </div>`;
  return c;
}
function srcHtml(onBatt) {
  return onBatt
    ? '<span class="badge red">&#9889; On Battery</span>'
    : '<span class="badge green">&#9889; AC Power</span>';
}

function mkUPSBattery(ups) {
  const c = document.createElement('div');
  c.className = 'card';
  if (!ups.available) {
    c.innerHTML = `
      <div class="card-title">
        <span class="icon">&#128267;</span> UPS Battery
      </div>
      <div class="info">
        <span class="badge muted">unavailable</span>
      </div>`;
    return c;
  }
  const pct = ups.percent || ups.battery_percent || 0;
  c.innerHTML = `
    <div class="card-title">
      <span class="icon">&#128267;</span> UPS Battery
    </div>
    <div class="gauge-wrap">
      ${gaugeSvg('batt',pct,battColor(pct),'charge')}
      <div class="info">
        <div>Charge:
          <span class="v" id="batt-b">
            ${battBadge(pct)}</span></div>
        <div style="font-size:11px;color:var(--text3)">
          &gt;75% green / &lt;75% yellow / &lt;30% red</div>
      </div>
    </div>`;
  return c;
}
function battBadge(pct) {
  if (pct >= 75)
    return '<span class="badge green">'+pct+'%</span>';
  if (pct >= 30)
    return '<span class="badge yellow">'+pct+'%</span>';
  return '<span class="badge red">'+pct+'%</span>';
}

function procRows(list, valKey) {
  if (!list || !list.length)
    return '<tr><td colspan="4" style="color:var(--text3)">no data</td></tr>';
  return list.map(function(p,i) {
    var v = p[valKey];
    var col = usageColor(v);
    return '<tr>'
      +'<td class="r" style="color:var(--text3)">'+(i+1)+'</td>'
      +'<td class="pname" title="'+esc(p.name)+'">'+esc(p.name)+'</td>'
      +'<td class="r" style="color:var(--text)">'+v.toFixed(1)+'%</td>'
      +'<td><span class="pbar"><span class="pbar-fill" style="width:'
        +Math.min(v,100)+'%;background:'+col+'"></span></span></td>'
      +'</tr>';
  }).join('');
}

function mkServices() {
  var services = [
    {icon:'🛡️', name:'Pi-hole',      url:'http://pi.lan:8081/admin/'},
    {icon:'📥', name:'Transmission', url:'http://pi.lan:9091/transmission/web/'},
    {icon:'🎬', name:'Plex',         url:'http://pi.lan:32400/web/index.html#!'},
    {icon:'📁', name:'File Browser', url:'http://pi.lan:8083/login?redirect=/files/torrents/'},
    {icon:'🐦', name:'BirdNET Pi',   url:'http://pi.lan/'}
  ];
  var c = document.createElement('div');
  c.className = 'card services';
  c.innerHTML =
    '<div class="card-title">'
    +'<span class="icon">&#128279;</span> Local Services'
    +'</div>'
    +'<div class="svc-list">'
    +services.map(function(s) {
      return '<a class="svc-link" href="'+esc(s.url)+'" target="_blank" rel="noopener">'
        +'<span class="svc-icon">'+s.icon+'</span>'
        +esc(s.name)+'<span class="arrow">&#8599;</span></a>';
    }).join('')
    +'</div>';
  return c;
}

function mkTopCPU(list) {
  var c = document.createElement('div');
  c.className = 'card wide';
  c.innerHTML =
    '<div class="card-title">'
    +'<span class="icon">&#9881;</span> Top 10 CPU'
    +'</div>'
    +'<table class="ptable" id="tcpu-tbl">'
    +'<tr><th class="r">#</th><th>Process</th>'
    +'<th class="r">CPU</th><th></th></tr>'
    +procRows(list,'cpu')
    +'</table>';
  return c;
}

function mkTopMem(list) {
  var c = document.createElement('div');
  c.className = 'card wide';
  c.innerHTML =
    '<div class="card-title">'
    +'<span class="icon">&#9619;</span> Top 10 Memory'
    +'</div>'
    +'<table class="ptable" id="tmem-tbl">'
    +'<tr><th class="r">#</th><th>Process</th>'
    +'<th class="r">Mem</th><th></th></tr>'
    +procRows(list,'mem')
    +'</table>';
  return c;
}

/* — render / update — */
let first = true;

async function refresh() {
  try {
    const r = await fetch('/api/stats');
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    if (first) { first=false; build(d); } else { update(d); }
    $('dot').className = 'dot';
    $('status').textContent = 'connected';
  } catch(e) {
    $('dot').className = 'dot err';
    $('status').textContent = 'connection lost';
  }
}

function build(d) {
  $('hostname').textContent = d.hostname || 'Pi Dashboard';
  document.title = (d.hostname||'Pi') + ' Dashboard';
  $('uptime').textContent = 'Up: '+(d.uptime&&d.uptime.text||'?');
  $('footer').textContent = 'Last updated: '+(d.timestamp||'');
  const g = $('grid'); g.innerHTML = '';
  g.appendChild(mkServices());
  g.appendChild(mkRAM(d.memory));
  g.appendChild(mkCPU(d.cpu));
  g.appendChild(mkTemp(d.temperature));
  g.appendChild(mkDisk(d.disk));
  g.appendChild(mkUPSStatus(d.ups));
  g.appendChild(mkUPSBattery(d.ups));
  g.appendChild(mkTopCPU(d.top_cpu));
  g.appendChild(mkTopMem(d.top_mem));
}

function setR(id,pct,col) {
  const e=$(id); if(!e) return;
  e.setAttribute('stroke-dashoffset', gOff(pct));
  if(col) e.setAttribute('stroke', col);
}
function sT(id,t) { const e=$(id); if(e) e.textContent=t; }
function sH(id,h) { const e=$(id); if(e) e.innerHTML=h; }

function update(d) {
  $('uptime').textContent='Up: '+(d.uptime&&d.uptime.text||'?');
  $('footer').textContent='Last updated: '+(d.timestamp||'');

  // RAM
  const m=d.memory;
  setR('ram',m.percent,usageColor(m.percent));
  sT('ram-t',Math.round(m.percent)+'%');
  sT('ram-u',(m.used_mb/1024).toFixed(1)+' GB');
  sT('ram-f',(m.available_mb/1024).toFixed(1)+' GB');

  // CPU
  const c=d.cpu;
  setR('cpu',c.percent,usageColor(c.percent));
  sT('cpu-t',Math.round(c.percent)+'%');
  sT('l1',c.load_1m); sT('l5',c.load_5m); sT('l15',c.load_15m);

  // Temp
  const t=d.temperature;
  const tv=t.celsius!==null ? t.celsius+'°C' : 'N/A';
  const tc=tempColor(t.status);
  const tp=t.celsius!==null ? Math.min(t.celsius,100) : 0;
  setR('tmp',tp,tc);
  const te=$('tmp-t');
  if(te){te.textContent=tv;te.setAttribute('fill',tc);}
  sH('tmp-s',tempBadge(t.status));

  // Disk
  const dk=d.disk;
  if(dk){
    sT('dk-lbl',dk.used_gb+' / '+dk.total_gb+' GB');
    sT('dk-pct',dk.percent+'%');
    sT('dk-free',dk.available_gb+' GB');
    const bf=$('dk-bar');
    if(bf){bf.style.width=dk.percent+'%';
      bf.style.background=usageColor(dk.percent);}
  }

  // UPS status
  const u=d.ups;
  if(u.available){
    sH('ups-src',srcHtml(u.on_battery));
    sT('ups-v',(u.voltage||0).toFixed(2)+' V');
    sT('ups-ma',(u.current_ma||0).toFixed(0)+' mA');
    sT('ups-ts',u.timestamp||'');
    const bp=u.percent||u.battery_percent||0;
    setR('batt',bp,battColor(bp));
    sT('batt-t',Math.round(bp)+'%');
    sH('batt-b',battBadge(bp));
  }

  // Top CPU
  var tcTbl=$('tcpu-tbl');
  if(tcTbl) tcTbl.innerHTML='<tr><th class="r">#</th><th>Process</th>'
    +'<th class="r">CPU</th><th></th></tr>'
    +procRows(d.top_cpu,'cpu');

  // Top Mem
  var tmTbl=$('tmem-tbl');
  if(tmTbl) tmTbl.innerHTML='<tr><th class="r">#</th><th>Process</th>'
    +'<th class="r">Mem</th><th></th></tr>'
    +procRows(d.top_mem,'mem');
}

refresh();
setInterval(refresh, REFRESH);
</script>
</body>
</html>
""".replace('__REFRESH__', str(REFRESH_INTERVAL)) \
   .replace('__TW__', str(TEMP_WARNING)) \
   .replace('__TC__', str(TEMP_CRITICAL))


# ================== MAIN ==================

if __name__ == "__main__":
    print(f"Pi Dashboard starting on http://{HOST}:{PORT}")
    print(f"UPS status file: {UPS_STATUS_FILE}")
    print(f"Monitoring disk: {DISK_PATH}")
    print(f"Refresh interval: {REFRESH_INTERVAL}s")
    app.run(host=HOST, port=PORT, quiet=True)
