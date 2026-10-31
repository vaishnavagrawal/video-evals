"""dubeval-project: file-based multi-video workspace (no database).

Layout (default root ./projects, override with --root or DUBEVAL_PROJECTS):

  projects/
    index.html                  # all projects, latest score per pair, links to reports
    <name>/
      project.json              # sources, languages, run history
      media/original.<ext>      # downloaded file, or symlink to a local file
      media/dub-01.<ext> ...
      work/                     # per-track caches (audio, LID, ASR, stems, judge)
      report.json  report.html  # latest run
      runs/<UTC timestamp>/report.json   # every run kept for comparison

Sources may be local paths, http(s) URLs or Google Drive share links.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import shutil
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("dubeval")

_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_GDRIVE = re.compile(r"drive\.google\.com|docs\.google\.com")


def _root(arg: str | None) -> Path:
    return Path(arg or os.environ.get("DUBEVAL_PROJECTS") or "projects")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ext(source: str) -> str:
    suffix = Path(urllib.request.urlparse(source).path).suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{2,4}", suffix or "") else ".mp4"


def fetch(source: str, dest_stem: Path) -> Path:
    """Materialize a source next to `dest_stem` (extension added). Idempotent: reuses an existing file."""
    if not re.match(r"^https?://", source, re.I):
        src = Path(source).expanduser().resolve()
        if not src.is_file():
            raise FileNotFoundError(f"no such file: {source}")
        dest = dest_stem.with_suffix(src.suffix.lower() or ".mp4")
        if not dest.exists():
            dest.symlink_to(src)
        return dest
    dest = dest_stem.with_suffix(_ext(source))
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    part = dest.with_suffix(dest.suffix + ".part")
    log.info("downloading %s -> %s", source, dest)
    if _GDRIVE.search(source):
        import gdown

        if gdown.download(source, str(part), quiet=True, fuzzy=True) is None:
            raise RuntimeError(f"Google Drive download failed (is the file shared publicly?): {source}")
    else:
        req = urllib.request.Request(source, headers={"User-Agent": "dubeval/0.1"})
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
            shutil.copyfileobj(r, f, length=1 << 20)
    if part.stat().st_size == 0:
        part.unlink()
        raise RuntimeError(f"empty download: {source}")
    part.rename(dest)
    return dest


def _load(d: Path) -> dict:
    f = d / "project.json"
    if not f.exists():
        raise SystemExit(f"no project at {d} (create it with `dubeval-project add`)")
    return json.loads(f.read_text())


def _save(d: Path, meta: dict) -> None:
    tmp = d / "project.json.tmp"
    tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    tmp.replace(d / "project.json")


def add(root: Path, name: str, original: str | None, dubs: list[str], src_lang: str | None,
        tgt_langs: list[str]) -> Path:
    """Create a project or append dubs to an existing one; downloads happen here, not at run time."""
    from .lang import resolve

    if not _NAME.match(name):
        raise SystemExit(f"invalid project name {name!r}: lowercase letters, digits, '.', '_', '-'")
    for label in ([src_lang] if src_lang else []) + tgt_langs:
        resolve(label)  # fail before downloading hundreds of MB
    d = root / name
    is_new = not (d / "project.json").exists()
    if is_new and not original:
        raise SystemExit("a new project needs --original")
    if is_new and not dubs:
        raise SystemExit("a new project needs at least one --dub")
    media = d / "media"
    media.mkdir(parents=True, exist_ok=True)
    meta = {"name": name, "created": _now(), "original": None, "dubs": [], "src_lang": None, "tgt_langs": [],
            "runs": []} if is_new else json.loads((d / "project.json").read_text())
    try:
        if original:
            if meta["original"] and meta["original"]["source"] != original:
                raise SystemExit(f"project {name} already has an original ({meta['original']['source']}); "
                                 "use a new project name")
            meta["original"] = {"source": original, "file": str(fetch(original, media / "original").relative_to(d))}
        known = {x["source"] for x in meta["dubs"]}
        for src in dubs:
            if src in known:
                continue
            f = fetch(src, media / f"dub-{len(meta['dubs']) + 1:02d}")
            meta["dubs"].append({"source": src, "file": str(f.relative_to(d))})
            known.add(src)
    except BaseException:
        if is_new:  # don't leave a half-created project behind
            shutil.rmtree(d, ignore_errors=True)
        raise
    if src_lang:
        meta["src_lang"] = src_lang
    if tgt_langs:
        meta["tgt_langs"] = tgt_langs
    _save(d, meta)
    return d


def run(root: Path, name: str, cfg_overrides: dict) -> dict:
    from .pipeline import Config, run as run_batch
    from .viz import render

    d = root / name
    meta = _load(d)
    cfg = Config(src_lang=meta.get("src_lang"), tgt_langs=meta.get("tgt_langs") or [], **cfg_overrides)
    batch = run_batch(d / meta["original"]["file"], [d / x["file"] for x in meta["dubs"]], d / "work", cfg)
    stamp = _now()
    run_dir = d / "runs" / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = batch.model_dump_json(indent=2)
    (run_dir / "report.json").write_text(payload)
    (d / "report.json").write_text(payload)
    render(batch.model_dump(mode="json"), d / "report.html")
    meta["runs"].append({
        "at": stamp, "report": str((run_dir / "report.json").relative_to(d)),
        "pairs": [{"pair": r.pair, "dub": r.target["file"], "audio_index": r.target["audio_index"],
                   "score": r.summary.score, "severity_counts": r.summary.severity_counts} for r in batch.reports],
        "skipped": len(batch.skipped_tracks), "failed": batch.failed})
    _save(d, meta)
    write_index(root)
    return meta["runs"][-1]


def projects(root: Path) -> list[dict]:
    out = []
    for f in sorted(root.glob("*/project.json")):
        try:
            out.append(json.loads(f.read_text()))
        except json.JSONDecodeError:
            log.warning("unreadable %s", f)
    return out


def write_index(root: Path) -> Path:
    rows = []
    for p in projects(root):
        last = p["runs"][-1] if p.get("runs") else None
        pairs = "<br>".join(
            f"{html.escape(x['pair'])} a:{x['audio_index']} — <b>{x['score']}</b> "
            f"(C{x['severity_counts']['critical']} / M{x['severity_counts']['major']} / m{x['severity_counts']['minor']})"
            for x in (last or {}).get("pairs", [])) or "not run yet"
        failed = "".join(f"<div class=warn>failed: {html.escape(f['dub'])}: {html.escape(f['error'])}</div>"
                         for f in (last or {}).get("failed", []))
        n = html.escape(p["name"])
        link = f'<a href="{n}/report.html">report</a> · <a href="{n}/report.json">json</a>' if last else ""
        rows.append(f"<tr><td><b>{n}</b><div class=s>{html.escape(p['original']['source'])}</div></td>"
                    f"<td>{len(p['dubs'])}</td><td>{pairs}{failed}</td>"
                    f"<td>{html.escape(last['at']) if last else ''}<div class=s>{len(p.get('runs', []))} run(s)</div></td>"
                    f"<td>{link}</td></tr>")
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>dubeval projects</title>
<style>body{{font:14px/1.4 system-ui,sans-serif;margin:16px;color:#1a1a1a}}table{{border-collapse:collapse;width:100%}}
th,td{{border-bottom:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}}th{{background:#f3f3f3}}
.s{{font-size:12px;color:#555;word-break:break-all}}.warn{{color:#a04000}}</style></head><body>
<h1>dubeval projects</h1><table><thead><tr><th>project</th><th>dubs</th><th>latest scores per pair</th>
<th>last run</th><th>open</th></tr></thead><tbody>{''.join(rows) or '<tr><td colspan=5>no projects</td></tr>'}
</tbody></table></body></html>"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(page, encoding="utf-8")
    return root / "index.html"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dubeval-project", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", help="projects directory (default ./projects or $DUBEVAL_PROJECTS)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="create a project / add dubs (downloads sources)")
    a.add_argument("name")
    a.add_argument("--original", help="path, http(s) URL or Google Drive link")
    a.add_argument("--dub", action="append", default=[], help="repeatable: path, URL or Drive link")
    a.add_argument("--src-lang")
    a.add_argument("--tgt-lang", help="comma-separated, e.g. es-419,pt-BR")
    a.add_argument("--run", action="store_true", help="run the eval right after adding")

    r = sub.add_parser("run", help="evaluate a project (all dubs) and refresh its report")
    r.add_argument("name", nargs="+", help="project name(s), or 'all'")

    for sp in (a, r):
        sp.add_argument("--separate", action="store_true", help="Demucs stems (CPU: ~3 min per 5 min audio)")
        sp.add_argument("--no-visual", action="store_true")
        sp.add_argument("--llm-model", default="gemini-flash,auto/best-fast", help="'none' disables")
        sp.add_argument("--asr-model", default="groq/whisper-large-v3", help="'none' = local only")
        sp.add_argument("--threads", type=int, default=4)

    sub.add_parser("list", help="list projects and latest scores")
    sub.add_parser("index", help="rebuild projects/index.html")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    for noisy in ("httpx", "httpcore", "openai", "huggingface_hub", "faster_whisper", "gdown"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root = _root(args.root)

    def overrides():
        llm = args.llm_model or ""
        return {"separate": args.separate, "visual": not args.no_visual, "threads": args.threads,
                "llm_models": [] if llm.lower() == "none" else [m.strip() for m in llm.split(",") if m.strip()],
                "asr_cloud_model": None if args.asr_model.lower() == "none" else args.asr_model}

    if args.cmd == "add":
        tgt = [x.strip() for x in (args.tgt_lang or "").split(",") if x.strip()]
        try:
            d = add(root, args.name, args.original, args.dub, args.src_lang, tgt)
        except ValueError as e:
            p.error(str(e))
        print(f"project ready: {d}")
        names = [args.name] if args.run else []
    elif args.cmd == "run":
        names = [x["name"] for x in projects(root)] if args.name == ["all"] else args.name
    elif args.cmd == "list":
        for x in projects(root):
            last = x["runs"][-1] if x.get("runs") else None
            scores = ", ".join(f"{y['pair']}={y['score']}" for y in last["pairs"]) if last else "not run"
            print(f"{x['name']:24} dubs={len(x['dubs'])} runs={len(x.get('runs', []))} {scores}")
        return 0
    else:
        print(f"wrote {write_index(root)}")
        return 0

    status = 0
    for n in names:
        res = run(root, n, overrides())
        for y in res["pairs"]:
            print(f"{n}: {y['pair']} a:{y['audio_index']} score {y['score']} {y['severity_counts']}")
        for f in res["failed"]:
            print(f"{n}: FAILED {f['dub']}: {f['error']}", file=sys.stderr)
            status = 1
        print(f"{n}: report {root / n / 'report.html'}")
    if names:
        print(f"index: {root / 'index.html'}")
    return status
