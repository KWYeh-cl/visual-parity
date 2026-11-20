#!/usr/bin/env python3
"""Generate a project-level `verify-<app>` skill: launch/doctor/cleanup the dev server, a feature map,
and per-feature flows that run through visual-parity.

Usage:
  python scaffold_verify.py --repo <app repo> [--name <app>] [--port N] [--host H] [--scheme http|https]
                            [--entry /path] [--start "<command>"] [--force]

Values not given are detected from package.json, vite.config.* and next.config.*; the summary says
which ones were detected and which are guesses. Existing files are never overwritten without --force.
"""
import argparse
import glob
import json
import os
import re
import shutil
import sys

TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "verify-app")


def read(path):
    try:
        return open(path, encoding="utf-8").read()
    except OSError:
        return ""


def detect(repo):
    found, guessed = {}, []
    pkg = json.loads(read(os.path.join(repo, "package.json")) or "{}")
    dev = (pkg.get("scripts") or {}).get("dev", "")
    vite = "".join(read(p) for p in glob.glob(os.path.join(repo, "vite.config.*")))
    nxt = "".join(read(p) for p in glob.glob(os.path.join(repo, "next.config.*")))
    name = re.sub(r"^clcom-frontend-", "", os.path.basename(os.path.abspath(repo)))
    found["name"] = name

    if "next" in dev or nxt:
        found["framework"] = "next"
        found["start"] = "npm run dev -- --port $PORT"
        https = "--experimental-https" in dev
        base = re.search(r"basePath:\s*['\"]([^'\"]+)", nxt)
    elif "vite" in dev or vite:
        found["framework"] = "vite"
        found["start"] = "npm run dev -- --port $PORT --strictPort"
        https = bool(re.search(r"https\s*:|basicSsl|mkcert", vite))
        base = re.search(r"\bbase:\s*['\"]([^'\"]+)", vite)
    else:
        found["framework"] = "unknown"
        found["start"] = "npm run dev"
        https, base = False, None
        guessed.append("start (no vite or next config found)")
    found["scheme"] = "https" if https else "http"
    found["entry"] = (base.group(1).rstrip("/") if base else "") + "/"
    if not base:
        guessed.append("entry (no base path found; using /)")
    found["port"] = 8443 if https else 3100
    guessed.append(f"port {found['port']} (chosen to avoid privileged and common ports)")
    found["host"] = "localhost"
    return found, guessed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--name")
    ap.add_argument("--port", type=int)
    ap.add_argument("--host")
    ap.add_argument("--scheme", choices=["http", "https"])
    ap.add_argument("--entry", help="path checked for readiness and opened first, e.g. /auth/login")
    ap.add_argument("--start", help="dev server command; $PORT is replaced by the port")
    ap.add_argument("--timeout", type=int, default=90, help="seconds to wait for the entry page")
    ap.add_argument("--not-covered", default="- Real sign-in, payments and anything that needs a real account.",
                    help="markdown bullet list for the 'Not covered' section")
    ap.add_argument("--force", action="store_true", help="overwrite an existing skill directory")
    args = ap.parse_args()

    repo = os.path.abspath(args.repo)
    found, guessed = detect(repo)
    for key in ("name", "port", "host", "scheme", "entry", "start"):
        given = getattr(args, key)
        if given is not None:
            found[key] = given
            guessed = [g for g in guessed if not g.startswith(key)]
    app = found["name"]
    dest = os.path.join(repo, ".claude", "skills", f"verify-{app}")
    if os.path.exists(dest) and not args.force:
        sys.exit(f"{dest} exists; pass --force to overwrite")

    start = found["start"].replace("$PORT", str(found["port"]))
    entry_url = f"{found['scheme']}://{found['host']}:{found['port']}{found['entry']}"
    values = {
        "__APP__": app, "__PORT__": str(found["port"]), "__HOST__": found["host"],
        "__SCHEME__": found["scheme"], "__ENTRY_URL__": entry_url, "__ENTRY__": found["entry"],
        "__START__": start.replace('"', '\\"'), "__TIMEOUT__": str(args.timeout),
        "__NOT_COVERED__": args.not_covered,
    }
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(TEMPLATE, dest)
    for root, _, files in os.walk(dest):
        for f in files:
            p = os.path.join(root, f)
            text = read(p)
            for k, v in values.items():
                text = text.replace(k, v)
            # START_CMD lives in a double-quoted shell string; the SKILL.md copy is shown as code.
            if f == "SKILL.md":
                text = text.replace(values["__START__"], start)
            open(p, "w", encoding="utf-8").write(text)
            if p.endswith(".sh"):
                os.chmod(p, 0o755)

    print(f"created {os.path.relpath(dest, repo)}")
    print(f"  framework {found['framework']}, entry {entry_url}")
    print(f"  start: {start}")
    for g in guessed:
        print(f"  GUESSED {g} — check scripts/lib.sh")
    print("next: run scripts/launch.sh and scripts/doctor.sh, then replace features/home.md and add one "
          "feature file + flow per screen users talk about; scripts/check-map.sh keeps the map honest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
