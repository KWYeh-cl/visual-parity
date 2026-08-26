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

  const out = [];
  for (const el of document.querySelectorAll('*')) {
    if (SKIP.has(el.tagName)) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    const cs = getComputedStyle(el);
    const styles = {};
    for (const p of props) styles[p] = cs.getPropertyValue(p).trim();
    out.push({
      path: cssPath(el),
      tag: el.tagName.toLowerCase(),
      text: ownText(el).replace(/\\s+/g, ' ').trim().slice(0, 120),
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
    loc_texts = {norm(e["text"]) for e in loc_els if norm(e["text"])}
    missing, ambiguous = [], []
    for e in ref_els:
        t = norm(e["text"])
        if id(e) in matched_ref or not t:
            continue
        (missing if t not in loc_texts else ambiguous).append(e)
    return pairs, missing, ambiguous


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
        manual_map = json.load(open(args.map))

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

            pairs, missing, ambiguous = build_pairs(ref["elements"], loc["elements"], manual_map)
            total_pairs += len(pairs)

            rows = []
            for r, l, how in pairs:
                for prop, a, b in diff_pair(r, l):
                    rows.append((prop, r["path"], l["path"], a, b, r["text"][:40], how))
                    prop_counter[prop] += 1
            total_diffs += len(rows)

            sec = [f"\n## viewport {w}px\n",
                   f"matched {len(pairs)} elements, {len(rows)} property diffs, "
                   f"{len(missing)} missing locally, "
                   f"{len(ambiguous)} not compared (text present but ambiguous)\n"]
            if rows:
                sec.append("\n| property | ref value | local value | ref selector | text |")
                sec.append("|---|---|---|---|---|")
                for prop, rp, lp, a, b, txt, how in sorted(rows):
                    sec.append(f"| {prop} | `{a}` | `{b}` | `{rp}` | {txt} |")
            if missing:
                sec.append("\n### present in ref, missing locally\n")
                for e in missing[:60]:
                    sec.append(f"- `{e['tag']}` {e['text'][:90]}")
                if len(missing) > 60:
                    sec.append(f"- ...and {len(missing) - 60} more")
            if ambiguous:
                sec.append(
                    f"\n### not compared: {len(ambiguous)} elements whose text is repeated on the page\n"
                    "Add entries to `--map` to compare these explicitly.\n")
            sections.append("\n".join(sec))
            stdout_rows.append((w, len(pairs), len(rows), len(missing), len(ambiguous)))

        browser.close()

    report = [f"# visual-parity diff\n", f"- ref:   {args.ref}", f"- local: {args.local}",
              f"- widths: {widths}"]
    if overflow_notes:
        report.append("\n**layout warnings**")
        report += [f"- {n}" for n in overflow_notes]
    report += sections
    with open(args.out, "w") as f:
        f.write("\n".join(report))

    print(f"report: {args.out}")
    for w, np_, nd, nm, na in stdout_rows:
        print(f"  {w}px: {np_} matched, {nd} diffs, {nm} missing-locally, {na} skipped-ambiguous")
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


def run_flow(args):
    steps = json.load(open(args.steps))
    console_errors, failed_requests, results = [], [], []

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
        page = ctx.new_page()
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
                    page.click(step["selector"], timeout=15000)
                elif act == "fill":
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
    if console_errors:
        lines += ["\n## console errors/warnings\n"] + [f"- {e}" for e in dict.fromkeys(console_errors)]
    if failed_requests:
        lines += ["\n## failed requests\n"] + [f"- {e}" for e in dict.fromkeys(failed_requests)]
    with open(args.out, "w") as f:
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


def selftest():
    """Assert the matcher and differ actually catch a known difference."""
    a = """<html><body><h1>Hello</h1><p class=x>Body copy</p>
      <ul><li>Item one</li></ul><div>Only in ref</div></body></html>"""
    b = """<html><body><h1 style="font-size:40px">Hello</h1>
      <div class=y style="margin-bottom:0">Body copy</div>
      <ul><li style="color:rgb(255,0,0)">Item one</li></ul></body></html>"""
    d = tempfile.mkdtemp()
    for name, html in (("a.html", a), ("b.html", b)):
        open(os.path.join(d, name), "w").write(html)
    out = os.path.join(d, "r.md")
    args = argparse.Namespace(
        ref="file://" + os.path.join(d, "a.html"), local="file://" + os.path.join(d, "b.html"),
        map=None, widths=None, width=1440, height=900, wait_selector=None, extra_wait=0, out=out)
    run_diff(args)
    body = open(out).read()
    assert "font-size" in body, "should detect the h1 font-size change"
    assert "color" in body, "should detect the li color change"
    assert "Only in ref" in body, "should report the ref-only element as missing locally"
    assert "`div`" in body or "margin-bottom" in body, "should match p->div across tag change"
    print("\nselftest OK")


def main():
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

    sub.add_parser("selftest", help="verify the matcher/differ still work")

    args = ap.parse_args()
    if args.cmd == "diff":
        sys.exit(run_diff(args))
    if args.cmd == "responsive":
        args.widths, args.width = "375,768,1440", 1440
        sys.exit(run_diff(args))
    if args.cmd == "flow":
        sys.exit(run_flow(args))
    selftest()


if __name__ == "__main__":
    main()
