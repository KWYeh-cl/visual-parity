#!/usr/bin/env python3
"""visual-parity: rendered-page comparison and E2E flow runner.

Black-box script. Run with --help. Do not read this source into an agent
context window; every mode writes its full result to a report file and prints
only a compressed summary to stdout.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("playwright not installed. Run: pip install playwright && playwright install chromium")

PROPS = [
    "font-family", "font-size", "font-weight", "font-style", "line-height",
    "letter-spacing", "text-align", "text-transform", "text-decoration-line",
    "color", "background-color", "opacity", "visibility",
    "margin-top", "margin-right", "margin-bottom", "margin-left",
    "padding-top", "padding-right", "padding-bottom", "padding-left",
    "border-top-width", "border-right-width", "border-bottom-width", "border-left-width",
    "border-top-color", "border-radius",
    "display", "flex-direction", "justify-content", "align-items", "gap",
    "list-style-type", "white-space", "overflow-x", "box-shadow",
]

# Box dimensions are compared with a tolerance; exact equality is noise.
BOX_TOLERANCE_PX = 2

COLLECT_JS = """
(props) => {
  const SKIP = new Set(['SCRIPT','STYLE','META','LINK','HEAD','NOSCRIPT','TITLE','BR']);
  const cssPath = (el) => {
    const parts = [];
    while (el && el.nodeType === 1 && parts.length < 8) {
      if (el === document.body) { parts.unshift('body'); break; }
      let p = el.tagName.toLowerCase();
      if (el.id && /^[A-Za-z][\\w-]*$/.test(el.id)) { parts.unshift('#' + el.id); break; }
      const par = el.parentElement;
      if (par) {
        const sibs = Array.from(par.children).filter(c => c.tagName === el.tagName);
        if (sibs.length > 1) p += ':nth-of-type(' + (sibs.indexOf(el) + 1) + ')';
      }
      parts.unshift(p);
      el = el.parentElement;
    }
    return parts.join(' > ');
  };
  const ownText = (el) => (el.children.length === 0
    ? el.textContent
    : Array.from(el.childNodes).filter(n => n.nodeType === 3).map(n => n.textContent).join(' '));

  // Same asset on two hosts or two builds: drop origin, query, and content
  // hashes so /_next/static/media/logo.3f2a9c1d.png matches /img/logo.png.
  const asset = (u) => {
    if (!u) return '';
    try {
      const url = new URL(u, location.href);
      const inner = url.searchParams.get('url');
      if (inner) return asset(inner);
      u = url.pathname;
    } catch (e) {}
    const base = decodeURIComponent(u.split('/').pop() || '');
    return base.replace(/[._-][0-9a-f]{8,}(?=\\.[a-z0-9]+$)/i, '').toLowerCase();
  };
  const bgAsset = (cs) => {
    const m = /url\\(["']?([^"')]+)/.exec(cs.backgroundImage || '');
    return m ? asset(m[1]) : '';
  };
  const visualKey = (el, cs) => {
    const t = el.tagName.toLowerCase();
    const a = (n) => (el.getAttribute(n) || '').trim().toLowerCase();
    if (t === 'img') return 'img:' + (asset(el.currentSrc || el.src) || a('alt'));
    if (t === 'video') return 'video:' + asset(el.currentSrc || el.getAttribute('src') || el.poster);
    if (t === 'iframe') return 'iframe:' + asset(el.src);
    if (t === 'svg') {
      const use = el.querySelector('use');
      const href = use && (use.getAttribute('href') || use.getAttribute('xlink:href'));
      if (href) return 'svg:' + href.split('#').pop();
      if (a('aria-label')) return 'svg:' + a('aria-label');
      const title = el.querySelector('title');
      if (title && title.textContent.trim()) return 'svg:' + title.textContent.trim().toLowerCase();
      const d = el.querySelector('path');
      return d ? 'svg-path:' + (d.getAttribute('d') || '').replace(/\\s+/g, '').slice(0, 80) : '';
    }
    if (t === 'input' || t === 'textarea' || t === 'select')
      return t + ':' + [a('type'), a('name'), a('placeholder'), a('aria-label')].join('|');
    if (a('aria-label')) return t + ':' + a('aria-label');
    if (a('title')) return t + ':' + a('title');
    const bg = bgAsset(cs);
    return bg ? 'bg:' + bg : '';
  };

  const out = [];
  for (const el of document.querySelectorAll('*')) {
    if (SKIP.has(el.tagName)) continue;
    if (el.closest('svg') && el.tagName.toLowerCase() !== 'svg') continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    const cs = getComputedStyle(el);
    const styles = {};
    for (const p of props) styles[p] = cs.getPropertyValue(p).trim();
    const text = ownText(el).replace(/\\s+/g, ' ').trim().slice(0, 120);
    out.push({
      path: cssPath(el),
      tag: el.tagName.toLowerCase(),
      text,
      key: text ? '' : visualKey(el, cs),
      leaf: el.children.length === 0 || el.tagName === 'svg',
      w: Math.round(r.width), h: Math.round(r.height),
      styles,
    });
    if (out.length >= 2500) break;
  }
  return {
    elements: out,
    docScrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  };
}
"""


def norm(t):
    return re.sub(r"\s+", " ", t or "").strip().lower()


def collect(page, url, wait_selector, extra_wait):
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass  # some pages poll forever; the DOM is usable regardless
    if wait_selector:
        page.wait_for_selector(wait_selector, timeout=30000)
    if extra_wait:
        page.wait_for_timeout(int(extra_wait * 1000))
    return page.evaluate(COLLECT_JS, PROPS)


def build_pairs(ref_els, loc_els, manual_map):
    """Match ref elements to local elements. Returns (pairs, unmatched_ref)."""
    pairs, used_loc = [], set()

    ref_by_path = {e["path"]: e for e in ref_els}
    loc_by_path = {e["path"]: e for e in loc_els}
    for ref_sel, loc_sel in (manual_map or {}).items():
        r = ref_by_path.get(ref_sel) or next((e for e in ref_els if e["path"].endswith(ref_sel)), None)
        l = loc_by_path.get(loc_sel) or next((e for e in loc_els if e["path"].endswith(loc_sel)), None)
        if r and l:
            pairs.append((r, l, "manual"))
            used_loc.add(l["path"])

    def index(els, keyfn):
        idx = defaultdict(list)
        for e in els:
            k = keyfn(e)
            if k:
                idx[k].append(e)
        return {k: v[0] for k, v in idx.items() if len(v) == 1}

    matched_ref = {id(r) for r, _, _ in pairs}
    for keyfn, label in (
        (lambda e: f"{e['tag']}|{norm(e['text'])}" if norm(e["text"]) else None, "tag+text"),
        (lambda e: norm(e["text"]) if norm(e["text"]) else None, "text"),
        (lambda e: e.get("key") or None, "visual"),
    ):
        ridx, lidx = index(ref_els, keyfn), index(loc_els, keyfn)
        for k, r in ridx.items():
            if id(r) in matched_ref:
                continue
            l = lidx.get(k)
            if l and l["path"] not in used_loc:
                pairs.append((r, l, label))
                matched_ref.add(id(r))
                used_loc.add(l["path"])

    # An unmatched ref element is only "missing" if its text appears nowhere
    # locally. Text that exists but is ambiguous (repeated nav labels, table
    # cells) simply cannot be paired 1:1 — reporting it as missing sends the
    # reader chasing content that is actually on the page.
    loc_ids = {norm(e["text"]) or e.get("key") for e in loc_els} - {"", None}
    missing, ambiguous, unkeyed = [], [], []
    for e in ref_els:
        if id(e) in matched_ref:
            continue
        ident = norm(e["text"]) or e.get("key")
        if not ident:
            # Text-less leaves with nothing to pair on are reported so the
            # coverage gap is visible; text-less containers are just layout.
            if e.get("leaf"):
                unkeyed.append(e)
            continue
        (missing if ident not in loc_ids else ambiguous).append(e)
    return pairs, missing, ambiguous, unkeyed


def diff_pair(r, l):
    out = []
    for p in PROPS:
        a, b = r["styles"].get(p, ""), l["styles"].get(p, "")
        if a != b:
            out.append((p, a, b))
    for dim in ("w", "h"):
        if abs(r[dim] - l[dim]) > BOX_TOLERANCE_PX:
            out.append((f"box-{dim}", f"{r[dim]}px", f"{l[dim]}px"))
    return out


def run_diff(args):
    manual_map = {}
    if args.map:
        manual_map = json.load(open(args.map, encoding="utf-8"))

    widths = [int(w) for w in args.widths.split(",")] if args.widths else [args.width]
    sections, stdout_rows = [], []
    prop_counter = Counter()
    total_pairs = total_diffs = 0
    overflow_notes = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(ignore_https_errors=True)
        page = ctx.new_page()
        for w in widths:
            page.set_viewport_size({"width": w, "height": args.height})
            ref = collect(page, args.ref, args.wait_selector, args.extra_wait)
            loc = collect(page, args.local, args.wait_selector, args.extra_wait)

            if loc["docScrollWidth"] > loc["clientWidth"] + 1:
                overflow_notes.append(
                    f"{w}px: local has horizontal overflow "
                    f"(scrollWidth {loc['docScrollWidth']} > clientWidth {loc['clientWidth']})"
                )

            pairs, missing, ambiguous, unkeyed = build_pairs(ref["elements"], loc["elements"], manual_map)
            total_pairs += len(pairs)

            label = lambda e: e["text"] or e.get("key") or ""
            rows = []
            for r, l, how in pairs:
                for prop, a, b in diff_pair(r, l):
                    rows.append((prop, r["path"], l["path"], a, b, label(r)[:40], how))
                    prop_counter[prop] += 1
            total_diffs += len(rows) + len(missing)

            sec = [f"\n## viewport {w}px\n",
                   f"matched {len(pairs)} elements, {len(rows)} property diffs, "
                   f"{len(missing)} missing locally, "
                   f"{len(ambiguous)} not compared (text or asset present but ambiguous), "
                   f"{len(unkeyed)} not compared (text-less leaves with no src/alt/aria-label/title)\n"]
            if rows:
                sec.append("\n| property | ref value | local value | ref selector | text |")
                sec.append("|---|---|---|---|---|")
                for prop, rp, lp, a, b, txt, how in sorted(rows):
                    sec.append(f"| {prop} | `{a}` | `{b}` | `{rp}` | {txt} |")
            if missing:
                sec.append("\n### present in ref, missing locally\n")
                for e in missing[:60]:
                    sec.append(f"- `{e['tag']}` {label(e)[:90]}")
                if len(missing) > 60:
                    sec.append(f"- ...and {len(missing) - 60} more")
            if ambiguous:
                sec.append(
                    f"\n### not compared: {len(ambiguous)} elements whose text or asset is repeated on the page\n"
                    "Add entries to `--map` to compare these explicitly.\n")
            if unkeyed:
                sec.append(
                    f"\n### not compared: {len(unkeyed)} text-less leaves with nothing to pair on\n"
                    "Decorative boxes, CSS icons, dividers. Use `--map` for any that matter, "
                    "or cover them with `snapshot`.\n")
                for e in unkeyed[:20]:
                    sec.append(f"- `{e['path']}` ({e['w']}x{e['h']})")
                if len(unkeyed) > 20:
                    sec.append(f"- ...and {len(unkeyed) - 20} more")
            sections.append("\n".join(sec))
            stdout_rows.append((w, len(pairs), len(rows), len(missing), len(ambiguous), len(unkeyed)))

        browser.close()

    report = [f"# visual-parity diff\n", f"- ref:   {args.ref}", f"- local: {args.local}",
              f"- widths: {widths}"]
    if overflow_notes:
        report.append("\n**layout warnings**")
        report += [f"- {n}" for n in overflow_notes]
    report += sections
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(report))

    print(f"report: {args.out}")
    for w, np_, nd, nm, na, nu in stdout_rows:
        print(f"  {w}px: {np_} matched, {nd} diffs, {nm} missing-locally, "
              f"{na} skipped-ambiguous, {nu} skipped-textless")
    for n in overflow_notes:
        print(f"  WARN {n}")
    if prop_counter:
        print("\ntop differing properties (fix these systemically, not element by element):")
        for prop, c in prop_counter.most_common(15):
            print(f"  {c:4d}  {prop}")
    return 1 if total_diffs else 0


def save_video(video, out_path):
    """Persist a Playwright video; convert to MP4 when the caller asked for one.

    Returns the path actually written, which falls back to the .webm original
    if ffmpeg is missing or fails.
    """
    root, ext = os.path.splitext(out_path)
    ext = ext.lower()
    webm = out_path if ext == ".webm" else root + ".webm"
    video.save_as(webm)
    if ext != ".mp4":
        return webm
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print(f"  ffmpeg not found - kept {webm} (install with: brew install ffmpeg)")
        return webm
    try:
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", webm,
             "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path],
            check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        print(f"  ffmpeg failed - kept {webm}: {e.stderr.decode()[:200]}")
        return webm
    os.remove(webm)
    return out_path


CURSOR_JS = """
(() => {
  if (window.__parityCursorInstalled) return;
  window.__parityCursorInstalled = true;
  const css = (el, s) => { for (const k in s) el.style.setProperty(k, s[k], 'important'); };
  const root = () => document.body || document.documentElement;
  let dot = null;
  function ensure() {
    if (dot && dot.isConnected) return dot;
    const r = root();
    if (!r) return null;
    dot = document.createElement('div');
    css(dot, {
      position: 'fixed', left: '0', top: '0', width: '18px', height: '18px',
      'margin-left': '-9px', 'margin-top': '-9px', 'border-radius': '50%',
      background: 'rgba(255,255,255,.35)', border: '2px solid rgba(18,18,18,.92)',
      'box-shadow': '0 0 0 1px rgba(255,255,255,.9), 0 1px 3px rgba(0,0,0,.45)',
      'pointer-events': 'none', 'z-index': '2147483647', opacity: '0',
      transition: 'opacity .12s linear'
    });
    r.appendChild(dot);
    return dot;
  }
  addEventListener('mousemove', (e) => {
    const d = ensure();
    if (!d) return;
    css(d, { opacity: '1', transform: 'translate3d(' + e.clientX + 'px,' + e.clientY + 'px,0)' });
  }, true);
  addEventListener('mousedown', (e) => {
    const r = root();
    if (!r) return;
    const ring = document.createElement('div');
    css(ring, {
      position: 'fixed', left: e.clientX + 'px', top: e.clientY + 'px',
      width: '14px', height: '14px', 'margin-left': '-7px', 'margin-top': '-7px',
      'border-radius': '50%', border: '2px solid rgba(0,120,255,.95)',
      background: 'rgba(0,120,255,.22)', 'pointer-events': 'none',
      'z-index': '2147483646',
      transition: 'transform .45s ease-out, opacity .45s ease-out'
    });
    r.appendChild(ring);
    requestAnimationFrame(() => css(ring, { transform: 'scale(3.2)', opacity: '0' }));
    setTimeout(() => ring.remove(), 600);
  }, true);
})();
"""

GLIDE_STEPS = 24
GLIDE_MS = 15
GLIDE_SETTLE_MS = 120


def glide(page, pos, selector):
    """Walk the pointer to the element before acting on it.

    Playwright dispatches synthetic events with no pointer travel, so a video
    would show the click landing with no indication of what was clicked. Moving
    in timed increments gives CURSOR_JS real mousemove events to render.
    """
    loc = page.locator(selector).first
    try:
        loc.scroll_into_view_if_needed(timeout=15000)
        box = loc.bounding_box()
    except Exception:
        box = None
    if not box:
        return
    tx, ty = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    sx, sy = pos
    for i in range(1, GLIDE_STEPS + 1):
        t = i / GLIDE_STEPS
        t = t * t * (3 - 2 * t)
        page.mouse.move(sx + (tx - sx) * t, sy + (ty - sy) * t)
        page.wait_for_timeout(GLIDE_MS)
    pos[0], pos[1] = tx, ty
    page.wait_for_timeout(GLIDE_SETTLE_MS)


STYLE_PROBE_JS = """
([sel, pseudo, props]) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  const cs = getComputedStyle(el, pseudo || null);
  const out = {};
  for (const p of props) out[p] = cs.getPropertyValue(p).trim();
  return out;
}
"""

# Expected values are written by people ("#fff", "300ms", "1rem"), computed
# values come back canonical ("rgb(255, 255, 255)", "0.3s", "16px"). Resolving
# the expected value through the same element's parent makes both sides
# canonical without hand-maintaining a unit table.
CANONICAL_JS = """
([sel, prop, value]) => {
  const el = document.querySelector(sel);
  const host = (el && el.parentElement) || document.body;
  const probe = document.createElement(el ? el.tagName : 'div');
  probe.style.setProperty('position', 'absolute');
  probe.style.setProperty('visibility', 'hidden');
  probe.style.setProperty(prop, value);
  if (!probe.style.getPropertyValue(prop)) return null;
  host.appendChild(probe);
  const got = getComputedStyle(probe).getPropertyValue(prop).trim();
  probe.remove();
  return got;
}
"""

BOX_JS = """
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return {x: r.x + scrollX, y: r.y + scrollY, width: r.width, height: r.height};
}
"""

WAIT_ANIMATIONS_JS = """
async (sel) => {
  const root = sel ? document.querySelector(sel) : document;
  if (!root) throw new Error('no element for ' + sel);
  const anims = root.getAnimations({subtree: true}).filter(a => a.effect && a.effect.getTiming().iterations !== Infinity);
  await Promise.all(anims.map(a => a.finished.catch(() => null)));
  return anims.length;
}
"""

_NUM_UNIT = re.compile(r"^(-?\d*\.?\d+)(px|ms|s)?$")


def _as_number(v):
    """'0.3s' -> 300.0, '12px' -> 12.0, anything compound -> None."""
    m = _NUM_UNIT.match(v.strip())
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2)
    return n * 1000 if unit == "s" else n


def check_styles(page, step):
    sel, pseudo = step["selector"], step.get("pseudo")
    expected = step["styles"]
    tol = step.get("tolerance", 0.5)
    got = page.evaluate(STYLE_PROBE_JS, [sel, pseudo, list(expected)])
    if got is None:
        raise AssertionError(f"no element for {sel}")
    bad = []
    for prop, want in expected.items():
        have = got[prop]
        if have == want:
            continue
        canon = page.evaluate(CANONICAL_JS, [sel, prop, want]) or want
        if have == canon:
            continue
        a, b = _as_number(have), _as_number(canon)
        if a is not None and b is not None and abs(a - b) <= tol:
            continue
        bad.append(f"{prop}: expected {want!r}, got {have!r}")
    assert not bad, "; ".join(bad)


def check_box(page, step, saved):
    box = page.evaluate(BOX_JS, step["selector"])
    if box is None:
        raise AssertionError(f"no element for {step['selector']}")
    if "same_as" in step:
        want = saved.get(step["same_as"])
        if want is None:
            raise AssertionError(f"no box saved as {step['same_as']!r}")
    else:
        want = {k: step[k] for k in ("x", "y", "width", "height") if k in step}
    tol = step.get("tolerance", 2)
    bad = [f"{k}: expected {want[k]:.1f}, got {box[k]:.1f}"
           for k in want if abs(box[k] - want[k]) > tol]
    assert not bad, "; ".join(bad)


AXE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "axe.min.js")
AXE_IMPACT = ["minor", "moderate", "serious", "critical"]
AXE_DEFAULT_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]


def run_axe(page, step):
    """Returns (markdown section, failure message or '')."""
    if not page.evaluate("() => typeof window.axe !== 'undefined'"):
        page.add_script_tag(path=AXE_PATH)
    context = {"include": [[step["selector"]]]} if step.get("selector") else {}
    if step.get("exclude"):
        context["exclude"] = [[s] for s in step["exclude"]]
    options = {"runOnly": {"type": "tag", "values": step.get("tags", AXE_DEFAULT_TAGS)}}
    res = page.evaluate(
        "([ctx, opts]) => axe.run(Object.keys(ctx).length ? ctx : document, opts)"
        ".then(r => r.violations.map(v => ({id: v.id, impact: v.impact, help: v.help,"
        " nodes: v.nodes.map(n => n.target.join(' '))})))",
        [context, options])
    fail_at = AXE_IMPACT.index(step.get("fail_on", "serious"))
    res.sort(key=lambda v: -AXE_IMPACT.index(v["impact"] or "minor"))
    if not res:
        return "no violations\n", ""
    lines = ["| impact | rule | nodes | example targets |", "|---|---|---|---|"]
    for v in res:
        ex = ", ".join(f"`{t}`" for t in v["nodes"][:3])
        lines.append(f"| {v['impact']} | {v['id']}: {v['help']} | {len(v['nodes'])} | {ex} |")
    blocking = [v for v in res if AXE_IMPACT.index(v["impact"] or "minor") >= fail_at]
    msg = ""
    if blocking:
        msg = f"{len(blocking)} axe violations at or above {AXE_IMPACT[fail_at]}: " + \
              ", ".join(f"{v['id']}({v['impact']})" for v in blocking)
    return "\n".join(lines) + "\n", msg


def run_flow(args):
    steps = json.load(open(args.steps, encoding="utf-8"))
    console_errors, failed_requests, results = [], [], []
    saved_boxes, axe_sections = {}, []

    video_path, tmpdir = None, None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        ctx_opts = {"ignore_https_errors": True}
        if args.video:
            w, h = (int(v) for v in args.video_size.lower().split("x"))
            tmpdir = tempfile.mkdtemp(prefix="parity-video-")
            ctx_opts.update(record_video_dir=tmpdir,
                            record_video_size={"width": w, "height": h},
                            viewport={"width": w, "height": h})
        ctx = browser.new_context(**ctx_opts)
        if args.cursor:
            ctx.add_init_script(CURSOR_JS)
        page = ctx.new_page()
        pos = [0.0, 0.0]
        page.on("console", lambda m: console_errors.append(f"[{m.type}] {m.text}"[:300])
                if m.type in ("error", "warning") else None)
        page.on("requestfailed", lambda r: failed_requests.append(f"{r.method} {r.url} — {r.failure}"[:300]))

        for i, step in enumerate(steps):
            act = step.get("action")
            desc = step.get("desc", f"{act} {step.get('selector') or step.get('url') or ''}")
            try:
                if act == "goto":
                    page.goto(step["url"], wait_until="domcontentloaded", timeout=60000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=20000)
                    except Exception:
                        pass
                elif act == "click":
                    if args.cursor:
                        glide(page, pos, step["selector"])
                    page.click(step["selector"], timeout=15000)
                elif act == "fill":
                    if args.cursor:
                        glide(page, pos, step["selector"])
                    page.fill(step["selector"], step["value"], timeout=15000)
                elif act == "press":
                    page.press(step["selector"], step["key"], timeout=15000)
                elif act == "wait":
                    if "ms" in step:
                        page.wait_for_timeout(step["ms"])
                    else:
                        page.wait_for_selector(step["selector"], timeout=step.get("timeout", 15000))
                elif act == "expect_text":
                    got = page.inner_text(step["selector"], timeout=15000)
                    assert step["value"] in got, f"expected {step['value']!r} in {got[:120]!r}"
                elif act == "expect_visible":
                    assert page.is_visible(step["selector"]), "not visible"
                elif act == "expect_focused":
                    got = page.evaluate(
                        "s => document.activeElement === document.querySelector(s)", step["selector"])
                    assert got, "element is not document.activeElement"
                elif act == "hover":
                    if args.cursor:
                        glide(page, pos, step["selector"])
                    page.hover(step["selector"], timeout=15000)
                elif act == "emulate":
                    media = {k: step[k] for k in ("reduced_motion", "color_scheme", "media") if k in step}
                    page.emulate_media(**media)
                elif act == "wait_animations":
                    page.evaluate(WAIT_ANIMATIONS_JS, step.get("selector"))
                elif act == "expect_style":
                    check_styles(page, step)
                elif act == "save_box":
                    box = page.evaluate(BOX_JS, step["selector"])
                    if box is None:
                        raise AssertionError(f"no element for {step['selector']}")
                    saved_boxes[step["as"]] = box
                elif act == "expect_box":
                    check_box(page, step, saved_boxes)
                elif act == "axe":
                    section, blocking = run_axe(page, step)
                    axe_sections.append(f"\n## axe — step {i}: {desc}\n\n" + section)
                    assert not blocking, blocking
                elif act == "screenshot":
                    page.screenshot(path=step["path"], full_page=step.get("full_page", True))
                else:
                    raise ValueError(f"unknown action {act!r}")
                results.append((i, desc, "PASS", ""))
            except Exception as e:
                results.append((i, desc, "FAIL", str(e)[:300]))
                if not args.keep_going:
                    break

        if args.video and args.video_tail:
            page.wait_for_timeout(int(args.video_tail * 1000))
        video = page.video if args.video else None
        ctx.close()  # the video file is only finalized on context close
        if video:
            video_path = save_video(video, args.video)
        browser.close()
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)

    lines = ["# visual-parity flow\n", f"steps file: {args.steps}\n"]
    if video_path:
        lines.append(f"video: {video_path}\n")
    lines += ["| # | step | result | detail |", "|---|---|---|---|"]
    lines += [f"| {i} | {d} | {r} | {m} |" for i, d, r, m in results]
    lines += axe_sections
    if console_errors:
        lines += ["\n## console errors/warnings\n"] + [f"- {e}" for e in dict.fromkeys(console_errors)]
    if failed_requests:
        lines += ["\n## failed requests\n"] + [f"- {e}" for e in dict.fromkeys(failed_requests)]
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    failed = [r for r in results if r[2] == "FAIL"]
    print(f"report: {args.out}")
    if video_path:
        print(f"video: {video_path}")
    print(f"  {len(results) - len(failed)}/{len(results)} steps passed")
    for i, d, r, m in failed:
        print(f"  FAIL step {i}: {d} — {m}")
    if console_errors:
        print(f"  {len(set(console_errors))} unique console errors/warnings")
    if failed_requests:
        print(f"  {len(set(failed_requests))} failed requests")
    return 1 if failed else 0


FREEZE_CSS = """*,*::before,*::after{animation-duration:0s!important;animation-delay:0s!important;
transition:none!important;caret-color:transparent!important}"""

ELEMENT_AT_JS = """
([x, y]) => {
  window.scrollTo(0, Math.max(0, y - innerHeight / 2));
  let el = document.elementFromPoint(x - scrollX, y - scrollY);
  if (!el) return null;
  const parts = [];
  for (let n = el; n && n.nodeType === 1 && parts.length < 4 && n !== document.body; n = n.parentElement) {
    let p = n.tagName.toLowerCase();
    if (n.id) { parts.unshift(p + '#' + n.id); break; }
    const cls = [...n.classList].filter(c => !/^(css|sc|jsx)-|^[a-z]+_[a-zA-Z0-9]{5}$/.test(c)).slice(0, 2);
    if (cls.length) p += '.' + cls.join('.');
    parts.unshift(p);
  }
  const t = (el.innerText || el.getAttribute('alt') || el.getAttribute('aria-label') || '').trim();
  return {path: parts.join(' > ') || el.tagName.toLowerCase(), text: t.replace(/\\s+/g, ' ').slice(0, 50)};
}
"""


def _flatten(img):
    from PIL import Image
    img = img.convert("RGBA")
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    return Image.alpha_composite(bg, img).convert("RGB")


def _regions(mask, cell_px, min_ratio):
    """Group mismatched pixels into rectangles on a coarse grid (BFS, 8-way)."""
    from PIL import Image
    gw, gh = max(1, round(mask.width / cell_px)), max(1, round(mask.height / cell_px))
    grid = mask.resize((gw, gh), Image.BOX).load()
    hot = {(x, y) for y in range(gh) for x in range(gw) if grid[x, y] >= min_ratio * 255}
    sx, sy = mask.width / gw, mask.height / gh
    out = []
    while hot:
        stack = [hot.pop()]
        cells = []
        while stack:
            cx, cy = stack.pop()
            cells.append((cx, cy))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    n = (cx + dx, cy + dy)
                    if n in hot:
                        hot.remove(n)
                        stack.append(n)
        xs, ys = [c[0] for c in cells], [c[1] for c in cells]
        weight = sum(grid[c] for c in cells) / 255
        out.append({"box": (min(xs) * sx, min(ys) * sy, (max(xs) + 1) * sx, (max(ys) + 1) * sy),
                    "weight": weight, "density": weight / len(cells)})
    return sorted(out, key=lambda r: -r["weight"])


def run_snapshot(args):
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

    ref = _flatten(Image.open(args.ref_image))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(ignore_https_errors=True, reduced_motion="reduce",
                                  viewport={"width": args.width, "height": args.height},
                                  device_scale_factor=args.scale)
        page = ctx.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        if args.wait_selector:
            page.wait_for_selector(args.wait_selector, timeout=30000)
        if args.freeze:
            page.add_style_tag(content=FREEZE_CSS)
        page.evaluate("() => document.fonts.ready.then(() => true)")
        if args.extra_wait:
            page.wait_for_timeout(int(args.extra_wait * 1000))

        shot = os.path.join(tempfile.mkdtemp(prefix="parity-snap-"), "local.png")
        if args.selector:
            loc = page.locator(args.selector).first
            loc.scroll_into_view_if_needed(timeout=15000)
            origin = page.evaluate(BOX_JS, args.selector)
            loc.screenshot(path=shot, animations="disabled")
        else:
            origin = {"x": 0, "y": 0}
            page.screenshot(path=shot, full_page=True, animations="disabled")
        local_raw = _flatten(Image.open(shot))

        css_w = local_raw.width / args.scale
        k = ref.width / css_w  # ref-image pixels per CSS pixel
        local = local_raw.resize((ref.width, max(1, round(local_raw.height * ref.width / local_raw.width))),
                                 Image.LANCZOS)
        ref_css_h, loc_css_h = ref.height / k, local.height / k
        h = min(ref.height, local.height)
        a, b = ref.crop((0, 0, ref.width, h)), local.crop((0, 0, ref.width, h))

        if not args.strict:
            # Figma and Chrome rasterise text and curves differently; blur away
            # sub-pixel edge noise and drop isolated pixels so only real shape,
            # position and colour changes survive.
            r = max(1.0, k * 0.75)
            a, b = a.filter(ImageFilter.GaussianBlur(r)), b.filter(ImageFilter.GaussianBlur(r))
        delta = ImageChops.difference(a, b).convert("L")
        mask = delta.point(lambda v: 255 if v > args.threshold * 255 else 0)
        if not args.strict:
            size = 3 if k < 2 else 5
            mask = mask.filter(ImageFilter.MinFilter(size)).filter(ImageFilter.MaxFilter(size))
        bad_px = mask.histogram()[255]
        mismatch = 100.0 * bad_px / (mask.width * mask.height)

        regions = _regions(mask, args.cell * k, 0.15)[:15]
        for reg in regions:
            x0, y0, x1, y1 = reg["box"]
            reg["css"] = tuple(round(v / k) for v in reg["box"])
            cx = origin["x"] + (x0 + x1) / 2 / k
            cy = origin["y"] + (y0 + y1) / 2 / k
            reg["element"] = page.evaluate(ELEMENT_AT_JS, [cx, cy])
        browser.close()

    overlay = ImageOps.grayscale(local.crop((0, 0, ref.width, h))).convert("RGB")
    overlay = Image.blend(overlay, Image.new("RGB", overlay.size, "white"), 0.5)
    overlay.paste(Image.new("RGB", overlay.size, (230, 0, 0)), mask=mask)
    draw = ImageDraw.Draw(overlay)
    for n, reg in enumerate(regions, 1):
        draw.rectangle(reg["box"], outline=(0, 90, 255), width=max(2, round(k)))
        draw.text((reg["box"][0] + 4, reg["box"][1] + 2), str(n), fill=(0, 90, 255))
    panel_h = max(ref.height, local.height)
    sheet = Image.new("RGB", (ref.width * 3, panel_h), (240, 240, 240))
    sheet.paste(ref, (0, 0))
    sheet.paste(local, (ref.width, 0))
    sheet.paste(overlay, (ref.width * 2, 0))
    sheet.save(args.diff_image)

    height_gap = loc_css_h - ref_css_h
    lines = ["# visual-parity snapshot\n",
             f"- design: {args.ref_image} ({ref.width}x{ref.height}px, {k:.2f}px per CSS px)",
             f"- local:  {args.url}" + (f" `{args.selector}`" if args.selector else " (full page)"),
             f"- viewport: {args.width}px, mode: {'strict' if args.strict else 'anti-alias tolerant'}",
             f"- diff image (design | local | mismatch): {args.diff_image}\n",
             f"**mismatch {mismatch:.2f}%** of the compared area (limit {args.max_mismatch}%)\n",
             f"height: design {ref_css_h:.0f}px, local {loc_css_h:.0f}px (CSS) — "
             f"{'compared the top ' + str(round(h / k)) + 'px only' if abs(height_gap) > 1 else 'same'}\n"]
    if regions:
        lines += ["| # | CSS box (x,y,w,h) | density | local element at centre |", "|---|---|---|---|"]
        for n, reg in enumerate(regions, 1):
            x0, y0, x1, y1 = reg["css"]
            el = reg["element"] or {}
            lines.append(f"| {n} | {x0},{y0},{x1 - x0},{y1 - y0} | {reg['density']:.0%} | "
                         f"`{el.get('path', '?')}` {el.get('text', '')} |")
    with open(args.out, "w") as f:
        f.write("\n".join(lines) + "\n")

    failed = mismatch > args.max_mismatch or abs(height_gap) > args.max_height_diff
    print(f"report: {args.out}")
    print(f"diff image: {args.diff_image}")
    print(f"  mismatch {mismatch:.2f}% (limit {args.max_mismatch}%), "
          f"height design {ref_css_h:.0f}px vs local {loc_css_h:.0f}px")
    for n, reg in enumerate(regions[:5], 1):
        x0, y0, x1, y1 = reg["css"]
        el = reg["element"] or {}
        print(f"  region {n}: {x0},{y0} {x1 - x0}x{y1 - y0} ({reg['density']:.0%}) -> {el.get('path', '?')}")
    return 1 if failed else 0


def selftest():
    """Assert the matcher and differ actually catch a known difference."""
    a = """<html><body><h1>Hello</h1><p class=x>Body copy</p>
      <ul><li>Item one</li></ul><div>Only in ref 🎉</div></body></html>"""
    b = """<html><body><h1 style="font-size:40px">Hello</h1>
      <div class=y style="margin-bottom:0">Body copy</div>
      <ul><li style="color:rgb(255,0,0)">Item one</li></ul></body></html>"""
    d = tempfile.mkdtemp()
    for name, html in (("a.html", a), ("b.html", b)):
        open(os.path.join(d, name), "w", encoding="utf-8").write(html)
    out = os.path.join(d, "r.md")
    args = argparse.Namespace(
        ref="file://" + os.path.join(d, "a.html"), local="file://" + os.path.join(d, "b.html"),
        map=None, widths=None, width=1440, height=900, wait_selector=None, extra_wait=0, out=out)
    run_diff(args)
    body = open(out, encoding="utf-8").read()
    assert "font-size" in body, "should detect the h1 font-size change"
    assert "color" in body, "should detect the li color change"
    assert "Only in ref" in body, "should report the ref-only element as missing locally"
    assert "`div`" in body or "margin-bottom" in body, "should match p->div across tag change"

    selftest_visual(d)
    selftest_flow(d)
    selftest_snapshot(d)
    print("\nselftest OK")


def _write(d, name, html):
    path = os.path.join(d, name)
    open(path, "w").write(html)
    return "file://" + path


def selftest_visual(d):
    a = _write(d, "va.html", """<html><body>
      <img src="/img/logo.1a2b3c4d5e.png" alt="Logo" width=40 height=40>
      <img src="/uploads/banner_95de82a84f.webp" width=60 height=30>
      <img src="hero.png" width=100 height=50>
      <svg aria-label="close" width="20" height="20"><path d="M0 0L10 10"/></svg>
      <span style="display:inline-block;width:10px;height:10px;background:red"></span></body></html>""")
    b = _write(d, "vb.html", """<html><body>
      <img src="https://cdn.example.com/logo.png?v=2" alt="Logo" width=40 height=40>
      <img src="/_next/image?url=%2Fuploads%2Fbanner_0a1b2c3d4e.webp&w=128" width=60 height=30>
      <svg aria-label="close" width="30" height="20"><path d="M0 0L10 10"/></svg>
      <span style="display:inline-block;width:10px;height:10px;background:red"></span></body></html>""")
    out = os.path.join(d, "v.md")
    rc = run_diff(argparse.Namespace(ref=a, local=b, map=None, widths=None, width=800, height=600,
                                     wait_selector=None, extra_wait=0, out=out))
    body = open(out).read()
    assert rc == 1, "missing content must fail the run"
    assert "img:hero.png" in body.split("missing locally")[-1], "should report the dropped image as missing"
    assert "img:logo.png" not in body.split("missing locally")[-1], "hashed and CDN logo URLs should pair"
    assert "banner" not in body.split("missing locally")[-1], "Strapi-hashed and next/image URLs should pair"
    assert "box-w" in body and "svg:close" in body, "should pair the svg by aria-label and diff its width"
    assert "text-less leaves" in body, "should count the unpairable decorative span"


def selftest_flow(d):
    url = _write(d, "flow.html", """<html><head><style>
      #btn{color:#000;transition:color 300ms}
      #btn:hover{color:#f00}
      @media (prefers-reduced-motion: reduce){#btn{transition:none}}
      #box{width:100px;height:40px;margin-left:16px}
      #anim{width:10px;height:10px;animation:grow 200ms forwards}
      @keyframes grow{to{width:200px}}
      </style></head><body lang="en"><main><h1>t</h1>
      <button id=btn>Go</button><div id=box></div><div id=anim></div><img src="x.png"></main></body></html>""")
    steps = [
        {"action": "goto", "url": url},
        {"action": "expect_style", "selector": "#btn", "styles": {"transition-duration": "300ms", "color": "#000"}},
        {"action": "hover", "selector": "#btn"},
        {"action": "wait", "ms": 450},
        {"action": "expect_style", "selector": "#btn", "styles": {"color": "red"}},
        {"action": "save_box", "selector": "#box", "as": "box"},
        {"action": "expect_box", "selector": "#box", "same_as": "box"},
        {"action": "expect_box", "selector": "#box", "width": 100, "height": 40},
        {"action": "wait_animations", "selector": "#anim"},
        {"action": "expect_style", "selector": "#anim", "styles": {"width": "200px"}},
        {"action": "emulate", "reduced_motion": "reduce"},
        {"action": "expect_style", "selector": "#btn", "styles": {"transition-duration": "0s"}},
        {"action": "expect_style", "selector": "#btn", "styles": {"color": "blue"}, "desc": "must fail"},
        {"action": "expect_box", "selector": "#box", "width": 120, "desc": "must fail"},
        {"action": "axe", "desc": "must fail"},
    ]
    sp = os.path.join(d, "steps.json")
    json.dump(steps, open(sp, "w"))
    out = os.path.join(d, "flow.md")
    run_flow(argparse.Namespace(steps=sp, out=out, headless=True, keep_going=True, video=None,
                                video_size="1280x720", video_tail=0, cursor=False))
    rows = [l.split(" | ") for l in open(out).read().splitlines() if re.match(r"^\| \d+ \|", l)]
    verdict = {int(r[0].strip("| ")): (r[1], r[2]) for r in rows}
    for i, (desc, res) in verdict.items():
        want = "FAIL" if "must fail" in desc else "PASS"
        assert res == want, f"flow step {i} ({desc}) was {res}, expected {want}"
    assert "image-alt" in open(out).read(), "axe should flag the img without alt"


def selftest_snapshot(d):
    page = """<html><body style="margin:0;background:#fff;font:16px sans-serif">
      <div id=block style="position:absolute;left:{x}px;top:20px;width:100px;height:60px;background:#1565c0"></div>
      <p style="position:absolute;left:20px;top:120px;margin:0">Design copy</p></body></html>"""
    a = _write(d, "sa.html", page.format(x=20))
    b = _write(d, "sb.html", page.format(x=80))
    ref = os.path.join(d, "design.png")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page(viewport={"width": 400, "height": 200})
        pg.goto(a)
        pg.screenshot(path=ref)
        browser.close()

    def snap(url):
        out = os.path.join(d, "snap.md")
        rc = run_snapshot(argparse.Namespace(
            ref_image=ref, url=url, selector=None, width=400, height=200, scale=2, threshold=0.1,
            max_mismatch=1.0, max_height_diff=8, cell=16, strict=False, freeze=True,
            wait_selector=None, extra_wait=0, out=out, diff_image=os.path.join(d, "snap.png")))
        return rc, open(out).read()

    rc, body = snap(a)
    assert rc == 0, "a page should match its own design export at a different scale:\n" + body
    rc, body = snap(b)
    assert rc == 1 and "div#block" in body, "a moved block should fail and be traced to its element:\n" + body


def main():
    # Windows consoles default to a legacy codepage (e.g. cp950); page text
    # echoed to stdout must not crash the run.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("diff", help="compare computed styles of two rendered URLs")
    d.add_argument("--ref", required=True, help="reference URL (old page / staging)")
    d.add_argument("--local", required=True, help="URL under development")
    d.add_argument("--map", help="JSON file: {\"<ref selector>\": \"<local selector>\"} manual overrides")
    d.add_argument("--width", type=int, default=1440)
    d.add_argument("--widths", help="comma list, e.g. 375,768,1440 (overrides --width)")
    d.add_argument("--height", type=int, default=900)
    d.add_argument("--wait-selector", help="wait for this selector before collecting")
    d.add_argument("--extra-wait", type=float, default=0, help="extra seconds after networkidle")
    d.add_argument("--out", default="parity-diff.md")

    r = sub.add_parser("responsive", help="diff at 375/768/1440 plus horizontal-overflow check")
    for a in ("--ref", "--local"):
        r.add_argument(a, required=True)
    r.add_argument("--map")
    r.add_argument("--height", type=int, default=900)
    r.add_argument("--wait-selector")
    r.add_argument("--extra-wait", type=float, default=0)
    r.add_argument("--out", default="parity-responsive.md")

    f = sub.add_parser("flow", help="run an interaction flow, collect console + network errors")
    f.add_argument("--steps", required=True, help="JSON array of step objects")
    f.add_argument("--out", default="parity-flow.md")
    f.add_argument("--headed", dest="headless", action="store_false", default=True)
    f.add_argument("--keep-going", action="store_true", help="continue after a failing step")
    f.add_argument("--video", help="record the run to this path; .mp4 converts via ffmpeg, .webm keeps the native output")
    f.add_argument("--video-size", default="1280x720", help="WxH of both the recording and the viewport (default 1280x720)")
    f.add_argument("--video-tail", type=float, default=1.0, help="seconds to keep recording after the last step so trailing animations finish (default 1)")
    f.add_argument("--no-cursor", dest="cursor", action="store_false", default=True, help="disable the synthetic pointer (on by default: it travels to each click/fill target and ripples on press)")

    s = sub.add_parser("snapshot", help="pixel-compare a rendered page or element against a design PNG (e.g. a Figma export)")
    s.add_argument("--ref-image", required=True, help="design PNG, e.g. downloaded from Figma get_screenshot")
    s.add_argument("--url", required=True, help="URL under development")
    s.add_argument("--selector", help="capture only this element (match the Figma frame); default full page")
    s.add_argument("--width", type=int, default=1440, help="viewport CSS width; use the Figma frame's original_width")
    s.add_argument("--height", type=int, default=900)
    s.add_argument("--scale", type=float, default=2, help="device scale factor for the local capture (default 2)")
    s.add_argument("--threshold", type=float, default=0.1, help="per-pixel luma difference 0-1 that counts as a mismatch")
    s.add_argument("--max-mismatch", type=float, default=1.0, help="percent of mismatched pixels allowed before exit 1")
    s.add_argument("--max-height-diff", type=float, default=8, help="CSS px of height difference allowed before exit 1")
    s.add_argument("--cell", type=int, default=16, help="grid cell size in CSS px for grouping mismatch regions")
    s.add_argument("--strict", action="store_true", help="no anti-alias tolerance (exact pixel comparison)")
    s.add_argument("--no-freeze", dest="freeze", action="store_false", default=True,
                   help="keep animations, transitions and the caret running")
    s.add_argument("--wait-selector")
    s.add_argument("--extra-wait", type=float, default=0)
    s.add_argument("--out", default="parity-snapshot.md")
    s.add_argument("--diff-image", default="parity-snapshot.png")

    sub.add_parser("selftest", help="verify the matcher/differ still work")

    args = ap.parse_args()
    if args.cmd == "diff":
        sys.exit(run_diff(args))
    if args.cmd == "responsive":
        args.widths, args.width = "375,768,1440", 1440
        sys.exit(run_diff(args))
    if args.cmd == "flow":
        sys.exit(run_flow(args))
    if args.cmd == "snapshot":
        sys.exit(run_snapshot(args))
    selftest()


if __name__ == "__main__":
    main()
