"""Self-contained interactive HTML graph viewer."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..io import atomic_write_text


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Graphite — {{title}}</title>
<style>
  :root { color-scheme: light dark; }
  body { margin: 0; font-family: ui-sans-serif, system-ui, sans-serif; overflow: hidden; background: #0f172a; color: #e2e8f0; }
  #toolbar { position: fixed; top: 12px; left: 12px; z-index: 10; display: flex; gap: 8px; }
  #toolbar button, #toolbar label { background: #1e293b; border: 1px solid #334155; color: #e2e8f0; padding: 6px 10px; border-radius: 6px; cursor: pointer; font-size: 13px; }
  #toolbar input { background: #1e293b; border: 1px solid #334155; color: #e2e8f0; padding: 6px 10px; border-radius: 6px; width: 180px; }
  #info { position: fixed; top: 12px; right: 12px; z-index: 10; background: #1e293b; border: 1px solid #334155; padding: 12px; border-radius: 8px; max-width: 320px; font-size: 13px; }
  #info h3 { margin: 0 0 6px; font-size: 14px; }
  #info p { margin: 4px 0; }
  canvas { display: block; cursor: grab; }
  canvas:active { cursor: grabbing; }
  #tooltip { position: fixed; pointer-events: none; background: #020617; border: 1px solid #334155; padding: 6px 8px; border-radius: 4px; font-size: 12px; z-index: 20; display: none; }
</style>
</head>
<body>
<div id="toolbar">
  <input id="search" placeholder="Find node..." />
  <button id="reset">Reset view</button>
  <button id="pause">Pause sim</button>
  <label><input type="checkbox" id="labels" checked /> Labels</label>
</div>
<div id="info">
  <h3>Graphite</h3>
  <p>{{node_count}} nodes · {{edge_count}} edges · {{cluster_count}} clusters</p>
  <p id="selection">Hover or click a node.</p>
</div>
<div id="tooltip"></div>
<canvas id="canvas"></canvas>
<script>
const DATA = {{data}};
const nodes = DATA.nodes.map((n, i) => ({
  id: n.id,
  label: n.name || n.id,
  kind: n.kind || 'unknown',
  cluster: DATA.clusters.findIndex(c => c.members.includes(n.id)),
  x: Math.random() * 800 - 400,
  y: Math.random() * 600 - 300,
  vx: 0, vy: 0,
  ...n
}));
const edges = DATA.edges.map(e => ({ source: e.source, target: e.target, ...e }));
const clusters = DATA.clusters;
const palette = ['#f87171','#fb923c','#facc15','#4ade80','#22d3ee','#818cf8','#c084fc','#f472b6'];
const canvas = document.getElementById('canvas');
const ctx = canvas.getContext('2d');
let width, height, transform = {x:0, y:0, k:1}, dragging = null, hovered = null, paused = false, showLabels = true;

function resize() { width = canvas.width = window.innerWidth; height = canvas.height = window.innerHeight; }
window.addEventListener('resize', resize); resize();

function clusterColor(c) { return palette[c % palette.length] || '#94a3b8'; }

function step() {
  if (!paused) {
    // Repulsion
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        let dx = a.x - b.x, dy = a.y - b.y;
        let d = Math.sqrt(dx*dx + dy*dy) || 1;
        const f = 120 / (d * d);
        const fx = (dx / d) * f, fy = (dy / d) * f;
        a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
      }
    }
    // Attraction along edges
    edges.forEach(e => {
      const a = nodes.find(n => n.id === e.source);
      const b = nodes.find(n => n.id === e.target);
      if (!a || !b) return;
      let dx = b.x - a.x, dy = b.y - a.y;
      let d = Math.sqrt(dx*dx + dy*dy) || 1;
      const f = d * 0.0003;
      const fx = (dx / d) * f, fy = (dy / d) * f;
      a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
    });
    // Center gravity + damp
    nodes.forEach(n => {
      n.vx -= n.x * 0.00005;
      n.vy -= n.y * 0.00005;
      n.vx *= 0.92; n.vy *= 0.92;
      n.x += n.vx; n.y += n.vy;
    });
  }
  render();
  requestAnimationFrame(step);
}

function render() {
  ctx.fillStyle = '#0f172a'; ctx.fillRect(0, 0, width, height);
  ctx.save();
  ctx.translate(transform.x + width/2, transform.y + height/2);
  ctx.scale(transform.k, transform.k);
  // Edges
  ctx.strokeStyle = 'rgba(148,163,184,0.25)'; ctx.lineWidth = 1 / transform.k;
  edges.forEach(e => {
    const a = nodes.find(n => n.id === e.source);
    const b = nodes.find(n => n.id === e.target);
    if (!a || !b) return;
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
  });
  // Nodes
  nodes.forEach(n => {
    const r = n.kind === 'file' ? 6 : 4;
    ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
    ctx.fillStyle = clusterColor(n.cluster);
    ctx.fill();
    if (showLabels && (n.kind === 'file' || hovered === n)) {
      ctx.fillStyle = '#e2e8f0'; ctx.font = `${12/transform.k}px sans-serif`;
      ctx.fillText(n.label, n.x + r + 2, n.y + 3);
    }
  });
  ctx.restore();
}

function worldPos(evt) {
  return {
    x: (evt.clientX - transform.x - width/2) / transform.k,
    y: (evt.clientY - transform.y - height/2) / transform.k
  };
}

canvas.addEventListener('mousedown', e => {
  const p = worldPos(e);
  hovered = nodes.find(n => Math.hypot(n.x - p.x, n.y - p.y) < 10) || null;
  dragging = hovered;
});
canvas.addEventListener('mousemove', e => {
  const p = worldPos(e);
  const near = nodes.find(n => Math.hypot(n.x - p.x, n.y - p.y) < 10) || null;
  hovered = near;
  const tt = document.getElementById('tooltip');
  if (near) {
    tt.style.display = 'block'; tt.style.left = (e.clientX + 12) + 'px'; tt.style.top = (e.clientY + 12) + 'px';
    tt.textContent = `${near.label} (${near.kind})`;
  } else { tt.style.display = 'none'; }
  if (dragging) { dragging.x = p.x; dragging.y = p.y; dragging.vx = 0; dragging.vy = 0; }
  updateSelection();
});
canvas.addEventListener('mouseup', () => dragging = null);
canvas.addEventListener('wheel', e => {
  e.preventDefault();
  const factor = e.deltaY > 0 ? 0.9 : 1.1;
  transform.k *= factor;
  render();
}, {passive: false});

let panning = false, lastPan = {x:0,y:0};
canvas.addEventListener('contextmenu', e => { e.preventDefault(); panning = true; lastPan = {x:e.clientX, y:e.clientY}; });
window.addEventListener('mouseup', () => panning = false);
window.addEventListener('mousemove', e => {
  if (!panning) return;
  transform.x += e.clientX - lastPan.x; transform.y += e.clientY - lastPan.y;
  lastPan = {x:e.clientX, y:e.clientY}; render();
});

document.getElementById('reset').onclick = () => { transform = {x:0,y:0,k:1}; };
document.getElementById('pause').onclick = () => { paused = !paused; };
document.getElementById('labels').onchange = e => { showLabels = e.target.checked; };
document.getElementById('search').addEventListener('input', e => {
  const q = e.target.value.toLowerCase();
  const match = nodes.find(n => n.label.toLowerCase().includes(q));
  if (match) { hovered = match; updateSelection(); }
});

function updateSelection() {
  const el = document.getElementById('selection');
  if (!hovered) { el.textContent = 'Hover or click a node.'; return; }
  const incoming = edges.filter(e => e.target === hovered.id).length;
  const outgoing = edges.filter(e => e.source === hovered.id).length;
  const cluster = clusters[hovered.cluster];
  el.innerHTML = `<b>${hovered.label}</b> (${hovered.kind})<br/>cluster: ${cluster ? cluster.labels.join(', ') : 'none'}<br/>in: ${incoming} / out: ${outgoing}`;
}

step();
</script>
</body>
</html>
"""


def to_html(
    graph_data: dict[str, Any],
    clusters: dict[str, Any],
    analysis: dict[str, Any],
    manifest: dict[str, Any],
    output_path: Path,
) -> None:
    """Write a self-contained interactive HTML viewer."""
    bundle = {
        "nodes": graph_data.get("nodes", []),
        "edges": graph_data.get("edges", []),
        "clusters": clusters.get("clusters", []),
        "analysis": analysis,
    }
    data_json = json.dumps(bundle, ensure_ascii=False)
    html = (
        _HTML_TEMPLATE
        .replace("{{data}}", data_json)
        .replace("{{title}}", manifest.get("root", "codebase"))
        .replace("{{node_count}}", str(graph_data.get("metadata", {}).get("node_count", 0)))
        .replace("{{edge_count}}", str(graph_data.get("metadata", {}).get("edge_count", 0)))
        .replace("{{cluster_count}}", str(clusters.get("count", 0)))
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)


