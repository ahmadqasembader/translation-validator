#!/usr/bin/env python3
"""
check_translations.py
---------------------
Standalone i18n key checker for BeneHR (or any react-i18next project).
Scans source files for t("key") calls, diffs against a dictionary CSV,
and optionally generates an SQL file to insert missing keys.

Run from anywhere — point it at your project and CSV via flags or config.

Requirements:
    pip install deep-translator

Usage:
    python3 check_translations.py --project ~/Desktop/benehr-web-app --csv ~/exports/dictionary.csv
    python3 check_translations.py --format html --out ~/Desktop/report.html
    python3 check_translations.py --sql-out ~/Desktop/missing.sql
    python3 check_translations.py --sql-out ~/Desktop/missing.sql --translate

Exit codes:
    0  — no missing keys
    1  — missing keys found  (useful for CI)
"""

import re
import csv
import os
import sys
import argparse
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_SRC_SUBDIR  = "src"
DEFAULT_EXTENSIONS  = {".ts", ".tsx", ".js", ".jsx"}
DEFAULT_FORMAT      = "txt"
CONFIG_FILE_NAME    = ".check_translations.toml"
EXCLUDED_DIRS       = {"node_modules", "dist", ".git", ".next", "build", "coverage"}

# Language IDs as they exist in your DB
LANG_EN = 1   # en_US  English
LANG_DE = 2   # de_DE  German

STATIC_KEY_PATTERN             = re.compile(r"""\bt\s*\(\s*["'`]([^"'`\n]+)["'`]""")
DYNAMIC_KEY_PATTERN            = re.compile(r"""\bt\s*\(\s*(?!["'`])([^\s,)]+)""")
TEMPLATE_INTERPOLATION_PATTERN = re.compile(r"""\bt\s*\(\s*[`]([^`]*\$\{[^`]*)[`]""")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class KeyOccurrence:
    key:  str
    file: str
    line: int

@dataclass
class DynamicOccurrence:
    raw_expr: str
    file:     str
    line:     int

@dataclass
class ScanResult:
    static_keys:   dict = field(default_factory=dict)
    dynamic_hits:  list = field(default_factory=list)
    files_scanned: int  = 0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def find_config_file() -> Path | None:
    """Walk up from cwd looking for the config file."""
    current = Path.cwd()
    for parent in [current, *current.parents]:
        candidate = parent / CONFIG_FILE_NAME
        if candidate.exists():
            return candidate
    return None


def load_config() -> dict:
    config_path = find_config_file()
    if not config_path:
        return {}
    if tomllib is None:
        print(f"[WARN] Found {config_path} but no TOML parser available.")
        print("       Use Python 3.11+ or: pip install tomli")
        return {}
    with open(config_path, "rb") as f:
        data = tomllib.load(f)
    print(f"[config] Loaded: {config_path}")
    return data


def resolve_config(args, config: dict) -> dict:
    """Merge: CLI args > config file > defaults."""
    cfg = config.get("check_translations", {})

    # --project sets the root; --src overrides the src subdir inside it
    project  = args.project or cfg.get("project", None)
    src_arg  = args.src     or cfg.get("src", None)

    if src_arg:
        # Explicit src path given — use as-is
        src = src_arg
    elif project:
        # Derive src from project root
        src = str(Path(project) / DEFAULT_SRC_SUBDIR)
    else:
        # Default: ./src relative to cwd
        src = f"./{DEFAULT_SRC_SUBDIR}"

    return {
        "project":          project,
        "src":              src,
        "csv":              args.csv       or cfg.get("csv",       None),
        "extensions":       set(args.ext  or cfg.get("extensions", list(DEFAULT_EXTENSIONS))),
        "format":           args.format    or cfg.get("format",    DEFAULT_FORMAT),
        "out":              args.out       or cfg.get("out",       None),
        "sql_out":          args.sql_out   or cfg.get("sql_out",   None),
        "translate":        args.translate  or cfg.get("translate", False),
        "show_occurrences": args.show_occurrences or cfg.get("show_occurrences", False),
    }


# ---------------------------------------------------------------------------
# Dictionary loader
# ---------------------------------------------------------------------------

def load_dictionary(csv_path: Path) -> set:
    if not csv_path.exists():
        print(f"[ERROR] Dictionary CSV not found: {csv_path}")
        sys.exit(1)
    keywords = set()
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "keyword" not in (reader.fieldnames or []):
            print(f"[ERROR] CSV must have a 'keyword' column. Found: {reader.fieldnames}")
            sys.exit(1)
        for row in reader:
            kw = row["keyword"].strip()
            if kw:
                keywords.add(kw)
    return keywords


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def _looks_like_i18n_key(key: str) -> bool:
    if len(key) > 120:                                          return False
    if "/" in key or "\\" in key:                               return False
    if key.startswith("http") or key.startswith("data:"):      return False
    if " " in key and len(key.split()) > 4:                    return False
    return True


def _looks_like_dynamic_call(expr: str) -> bool:
    if not expr or len(expr) < 2:                               return False
    if expr in {"(", ")", ",", ";", "{", "}"}:                  return False
    return bool(re.match(r'^[a-zA-Z_$][a-zA-Z0-9_$.]*', expr))


def scan_file(file_path: Path, project_root: Path):
    static_hits, dynamic_hits = [], []
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"  [WARN] Could not read {file_path}: {e}")
        return static_hits, dynamic_hits

    try:
        rel_path = str(file_path.relative_to(project_root))
    except ValueError:
        rel_path = str(file_path)

    for line_no, line in enumerate(content.splitlines(), start=1):
        for m in TEMPLATE_INTERPOLATION_PATTERN.finditer(line):
            dynamic_hits.append(DynamicOccurrence(m.group(0).strip(), rel_path, line_no))
        for m in STATIC_KEY_PATTERN.finditer(line):
            key = m.group(1).strip()
            if _looks_like_i18n_key(key):
                static_hits.append(KeyOccurrence(key, rel_path, line_no))
        for m in DYNAMIC_KEY_PATTERN.finditer(line):
            raw = m.group(1).strip()
            if not STATIC_KEY_PATTERN.search(line[m.start():m.start() + len(raw) + 10]):
                if _looks_like_dynamic_call(raw):
                    dynamic_hits.append(DynamicOccurrence(f"t({raw}...)", rel_path, line_no))
    return static_hits, dynamic_hits


def scan_project(src_dir: Path, extensions: set) -> ScanResult:
    result = ScanResult()
    all_files = [
        p for p in src_dir.rglob("*")
        if p.is_file()
        and p.suffix.lower() in extensions
        and not any(part in EXCLUDED_DIRS for part in p.parts)
    ]
    print(f"Scanning {len(all_files)} files in {src_dir} ...")
    for file_path in sorted(all_files):
        static_hits, dynamic_hits = scan_file(file_path, src_dir.parent)
        result.files_scanned += 1
        for hit in static_hits:
            result.static_keys.setdefault(hit.key, []).append(hit)
        result.dynamic_hits.extend(dynamic_hits)
    return result


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------

def group_by_prefix(keys: list) -> dict:
    groups = {}
    for key in keys:
        prefix = key.split("-")[0] if "-" in key else "other"
        groups.setdefault(prefix, []).append(key)
    return dict(sorted(groups.items()))


def key_to_english(key: str) -> str:
    """Convert kebab-case key to a readable English string."""
    words = key.replace("-", " ").replace("_", " ")
    return words.capitalize()


# ---------------------------------------------------------------------------
# Google Translate via deep-translator (optional)
# ---------------------------------------------------------------------------

def check_deep_translator() -> bool:
    """Return True if deep-translator is installed."""
    try:
        import deep_translator  # noqa: F401
        return True
    except ImportError:
        return False


def translate_keys_via_google(keys: list) -> dict:
    """
    Translate a list of i18n keys to German using Google Translate.
    Keys are converted from kebab-case to readable English first.
    Returns dict: {key: german_translation}
    Falls back to TODO placeholders on any error.
    """
    if not keys:
        return {}

    try:
        from deep_translator import GoogleTranslator
    except ImportError:
        print("\n[WARN] deep-translator not installed. Run: pip install deep-translator")
        print("       Falling back to TODO placeholders.\n")
        return {k: f"TODO: {key_to_english(k)}" for k in keys}

    print(f"\n[Google Translate] Translating {len(keys)} keys to German ...")

    # Translate in batches — Google Translate has a ~5000 char limit per request
    CHUNK_SIZE = 50
    results = {}
    translator = GoogleTranslator(source="en", target="de")

    for i in range(0, len(keys), CHUNK_SIZE):
        chunk     = keys[i:i + CHUNK_SIZE]
        chunk_num = i // CHUNK_SIZE + 1
        total     = (len(keys) + CHUNK_SIZE - 1) // CHUNK_SIZE
        print(f"  Chunk {chunk_num}/{total} ({len(chunk)} keys) ...", end=" ", flush=True)

        # Convert keys to readable English before translating
        readable = [key_to_english(k) for k in chunk]

        try:
            translated = translator.translate_batch(readable)
            for key, de_val in zip(chunk, translated):
                results[key] = de_val or key_to_english(key)
            print(f"✓")
        except Exception as e:
            print(f"✗ ({e})")
            for key in chunk:
                results[key] = f"TODO: {key_to_english(key)}"

    success = sum(1 for v in results.values() if not v.startswith("TODO:"))
    print(f"\n  Done — {success}/{len(keys)} translated successfully.")
    return results


# ---------------------------------------------------------------------------
# SQL generator
# ---------------------------------------------------------------------------

def generate_sql(missing_keys: list, translate: bool, out_path: Path):
    """Generate INSERT SQL for missing dictionary keys and their translations."""

    if not missing_keys:
        print("\n[SQL] No missing keys — skipping SQL generation.")
        return

    # Get German translations
    if translate:
        if not check_deep_translator():
            print("\n[ERROR] --translate requires deep-translator.")
            print("        Install it with: pip install deep-translator\n")
            sys.exit(1)
        de_translations = translate_keys_via_google(missing_keys)
    else:
        de_translations = {}

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []

    lines.append("-- =============================================================")
    lines.append("-- BeneHR — Missing Translation Keys")
    lines.append(f"-- Generated : {ts}")
    lines.append(f"-- Keys      : {len(missing_keys)}")
    if not translate:
        lines.append("-- Note      : German translations are placeholders.")
        lines.append("--             Run with --translate to auto-translate via Google Translate.")
    lines.append("-- =============================================================")
    lines.append("")

    lines.append("-- Step 1: Insert missing keywords into dictionary")
    lines.append("-- (ON CONFLICT DO NOTHING is safe to re-run)")
    lines.append("")

    for key in sorted(missing_keys):
        safe_key = key.replace("'", "''")
        lines.append(f"INSERT INTO dictionary (keyword) VALUES ('{safe_key}') ON CONFLICT DO NOTHING;")

    lines.append("")
    lines.append("")
    lines.append("-- Step 2: Insert English translations (en_US, language_id = 1)")
    lines.append("")

    for key in sorted(missing_keys):
        safe_key = key.replace("'", "''")
        en_value = key_to_english(key).replace("'", "''")
        lines.append(
            f"INSERT INTO translation (dictionary_id, language_id, value)\n"
            f"  SELECT d.id, {LANG_EN}, '{en_value}'\n"
            f"  FROM dictionary d\n"
            f"  WHERE d.keyword = '{safe_key}'\n"
            f"  AND NOT EXISTS (\n"
            f"    SELECT 1 FROM translation t\n"
            f"    WHERE t.dictionary_id = d.id AND t.language_id = {LANG_EN}\n"
            f"  );"
        )
        lines.append("")

    lines.append("")
    lines.append("-- Step 3: Insert German translations (de_DE, language_id = 2)")
    lines.append("")

    for key in sorted(missing_keys):
        safe_key = key.replace("'", "''")
        de_value = de_translations.get(key, f"TODO: {key_to_english(key)}").replace("'", "''")
        lines.append(
            f"INSERT INTO translation (dictionary_id, language_id, value)\n"
            f"  SELECT d.id, {LANG_DE}, '{de_value}'\n"
            f"  FROM dictionary d\n"
            f"  WHERE d.keyword = '{safe_key}'\n"
            f"  AND NOT EXISTS (\n"
            f"    SELECT 1 FROM translation t\n"
            f"    WHERE t.dictionary_id = d.id AND t.language_id = {LANG_DE}\n"
            f"  );"
        )
        lines.append("")

    sql = "\n".join(lines)
    out_path.write_text(sql, encoding="utf-8")
    print(f"\n[SQL] Written to: {out_path}")
    print(f"      {len(missing_keys)} keys × 2 languages = {len(missing_keys) * 2} translation inserts")


# ---------------------------------------------------------------------------
# TXT renderer
# ---------------------------------------------------------------------------

def render_txt(scan, dictionary, src_dir, show_occurrences) -> str:
    lines = []
    SEP = "=" * 70
    keys_in_code      = set(scan.static_keys.keys())
    missing_from_dict = sorted(keys_in_code - dictionary)
    unused_in_code    = sorted(dictionary - keys_in_code)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines += [
        SEP,
        "  BeneHR — Translation Key Report",
        f"  Generated : {ts}",
        f"  Source    : {src_dir}",
        SEP,
        f"  Files scanned              : {scan.files_scanned}",
        f"  Keys found in code         : {len(keys_in_code)}",
        f"  Keys in dictionary         : {len(dictionary)}",
        SEP,
        f"  Missing from dictionary    : {len(missing_from_dict)}",
        f"  Dynamic keys (manual check): {len(scan.dynamic_hits)}",
        f"  Orphaned dictionary keys   : {len(unused_in_code)}",
        SEP,
    ]

    lines.append("\n[MISSING FROM DICTIONARY]")
    lines.append("-" * 70)
    if missing_from_dict:
        for prefix, keys in group_by_prefix(missing_from_dict).items():
            lines.append(f"\n  [{prefix}]")
            for key in keys:
                lines.append(f"    {key}")
                if show_occurrences:
                    for occ in scan.static_keys[key]:
                        lines.append(f"        {occ.file}:{occ.line}")
    else:
        lines.append("  All keys are present in the dictionary.")

    lines.append("\n[DYNAMIC KEYS — MANUAL REVIEW]")
    lines.append("-" * 70)
    if scan.dynamic_hits:
        for hit in scan.dynamic_hits:
            lines.append(f"  {hit.file}:{hit.line}")
            lines.append(f"      {hit.raw_expr}")
    else:
        lines.append("  No dynamic key calls found.")

    lines.append("\n[ORPHANED DICTIONARY KEYS]")
    lines.append("-" * 70)
    if unused_in_code:
        for prefix, keys in group_by_prefix(unused_in_code).items():
            lines.append(f"\n  [{prefix}]")
            for key in keys:
                lines.append(f"    {key}")
    else:
        lines.append("  Every dictionary key is referenced in code.")

    lines.append(f"\n{SEP}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MD renderer
# ---------------------------------------------------------------------------

def render_md(scan, dictionary, src_dir, show_occurrences) -> str:
    keys_in_code      = set(scan.static_keys.keys())
    missing_from_dict = sorted(keys_in_code - dictionary)
    unused_in_code    = sorted(dictionary - keys_in_code)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    out = []
    out.append("# 🌐 BeneHR — Translation Key Report\n")
    out.append(f"_Generated: {ts} | Source: `{src_dir}`_\n")
    out.append("## Summary\n")
    out.append("| Metric | Count |")
    out.append("|--------|-------|")
    out.append(f"| Files scanned | {scan.files_scanned} |")
    out.append(f"| Keys found in code | {len(keys_in_code)} |")
    out.append(f"| Keys in dictionary | {len(dictionary)} |")
    out.append(f"| ❌ Missing from dictionary | {len(missing_from_dict)} |")
    out.append(f"| ⚠️ Dynamic keys (manual) | {len(scan.dynamic_hits)} |")
    out.append(f"| 🗑️ Orphaned dictionary keys | {len(unused_in_code)} |")
    out.append("")
    out.append("---\n")

    out.append(f"## ❌ Missing from Dictionary ({len(missing_from_dict)} keys)\n")
    if missing_from_dict:
        out.append("> Used in code but not yet added to the dictionary.\n")
        for prefix, keys in group_by_prefix(missing_from_dict).items():
            out.append(f"### `{prefix}-*` ({len(keys)})\n")
            if show_occurrences:
                out.append("| Key | File | Line |")
                out.append("|-----|------|------|")
                for key in keys:
                    for occ in scan.static_keys[key]:
                        out.append(f"| `{key}` | `{occ.file}` | {occ.line} |")
            else:
                out.append("| Key |")
                out.append("|-----|")
                for key in keys:
                    out.append(f"| `{key}` |")
            out.append("")
    else:
        out.append("✅ All keys are present in the dictionary.\n")

    out.append("---\n")
    out.append(f"## ⚠️ Dynamic Keys — Manual Review ({len(scan.dynamic_hits)} hits)\n")
    if scan.dynamic_hits:
        out.append("> Cannot be verified automatically — check these manually.\n")
        out.append("| File | Line | Expression |")
        out.append("|------|------|------------|")
        for hit in scan.dynamic_hits:
            out.append(f"| `{hit.file}` | {hit.line} | `{hit.raw_expr}` |")
        out.append("")
    else:
        out.append("✅ No dynamic key calls found.\n")

    out.append("---\n")
    out.append(f"## 🗑️ Orphaned Dictionary Keys ({len(unused_in_code)} keys)\n")
    if unused_in_code:
        out.append("> In the dictionary but not found in any source file.\n")
        for prefix, keys in group_by_prefix(unused_in_code).items():
            out.append(f"### `{prefix}-*` ({len(keys)})\n")
            out.append("| Key |")
            out.append("|-----|")
            for key in keys:
                out.append(f"| `{key}` |")
            out.append("")
    else:
        out.append("✅ Every dictionary key is referenced in code.\n")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# HTML renderer
# ---------------------------------------------------------------------------

def render_html(scan, dictionary, src_dir, show_occurrences) -> str:
    keys_in_code      = set(scan.static_keys.keys())
    missing_from_dict = sorted(keys_in_code - dictionary)
    unused_in_code    = sorted(dictionary - keys_in_code)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    def esc(s):
        return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

    def missing_tables_html():
        if not missing_from_dict:
            return '<p class="ok">✅ All keys are present in the dictionary.</p>'
        html = []
        for prefix, keys in group_by_prefix(missing_from_dict).items():
            html.append(f'<details open><summary><code>{esc(prefix)}-*</code><span class="count">{len(keys)}</span></summary>')
            if show_occurrences:
                html.append('<table><thead><tr><th>Key</th><th>File</th><th>Line</th></tr></thead><tbody>')
                for key in keys:
                    for occ in scan.static_keys[key]:
                        html.append(f'<tr><td><code>{esc(key)}</code></td><td><code>{esc(occ.file)}</code></td><td>{occ.line}</td></tr>')
            else:
                html.append('<table><thead><tr><th>Key</th></tr></thead><tbody>')
                for key in keys:
                    html.append(f'<tr><td><code>{esc(key)}</code></td></tr>')
            html.append('</tbody></table></details>')
        return "\n".join(html)

    def dynamic_table_html():
        if not scan.dynamic_hits:
            return '<p class="ok">✅ No dynamic key calls found.</p>'
        rows = "\n".join(
            f'<tr><td><code>{esc(h.file)}</code></td><td>{h.line}</td><td><code>{esc(h.raw_expr)}</code></td></tr>'
            for h in scan.dynamic_hits
        )
        return f'<table><thead><tr><th>File</th><th>Line</th><th>Expression</th></tr></thead><tbody>{rows}</tbody></table>'

    def orphan_tables_html():
        if not unused_in_code:
            return '<p class="ok">✅ Every dictionary key is referenced in code.</p>'
        html = []
        for prefix, keys in group_by_prefix(unused_in_code).items():
            html.append(f'<details><summary><code>{esc(prefix)}-*</code><span class="count orphan">{len(keys)}</span></summary>')
            html.append('<table><thead><tr><th>Key</th></tr></thead><tbody>')
            for key in keys:
                html.append(f'<tr><td><code>{esc(key)}</code></td></tr>')
            html.append('</tbody></table></details>')
        return "\n".join(html)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Translation Report — BeneHR</title>
<style>
:root{{--red:#dc2626;--yellow:#d97706;--green:#16a34a;--grey:#6b7280;--bg:#f9fafb;--card:#fff;--border:#e5e7eb;--code-bg:#f3f4f6;--text:#111827;--muted:#6b7280}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text);font-size:14px;line-height:1.6;padding:2rem}}
header{{max-width:980px;margin:0 auto 2rem}}
header h1{{font-size:1.5rem;font-weight:700;margin-bottom:.25rem}}
header p{{color:var(--muted);font-size:.85rem}}
.summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1rem;max-width:980px;margin:0 auto 2rem}}
.stat{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:1rem 1.25rem}}
.stat .label{{color:var(--muted);font-size:.75rem;text-transform:uppercase;letter-spacing:.05em}}
.stat .value{{font-size:1.7rem;font-weight:700;margin-top:.2rem}}
.stat.red .value{{color:var(--red)}}.stat.yellow .value{{color:var(--yellow)}}.stat.green .value{{color:var(--green)}}.stat.grey .value{{color:var(--grey)}}
section{{max-width:980px;margin:0 auto 1.5rem;background:var(--card);border:1px solid var(--border);border-radius:12px;overflow:hidden}}
.section-header{{padding:1rem 1.25rem;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:.5rem;font-weight:600;font-size:.95rem}}
.section-body{{padding:1.25rem}}
.section-body p.note{{font-size:.82rem;color:var(--muted);margin-bottom:1rem}}
p.ok{{color:var(--green);font-weight:500}}
details{{border:1px solid var(--border);border-radius:8px;margin-bottom:.75rem;overflow:hidden}}
details summary{{padding:.55rem 1rem;cursor:pointer;font-weight:600;font-size:.85rem;background:var(--bg);list-style:none;display:flex;align-items:center;gap:.5rem;user-select:none}}
details summary::-webkit-details-marker{{display:none}}
details summary::before{{content:"▶";font-size:.6rem;color:var(--muted);transition:transform .15s;margin-right:.25rem}}
details[open] summary::before{{transform:rotate(90deg)}}
.count{{background:var(--red);color:#fff;border-radius:999px;padding:0 .45rem;font-size:.72rem;font-weight:700;margin-left:auto}}
.count.orphan{{background:var(--yellow)}}
table{{width:100%;border-collapse:collapse;font-size:.82rem}}
th{{text-align:left;padding:.5rem .75rem;background:var(--bg);font-weight:600;color:var(--muted);text-transform:uppercase;font-size:.72rem;letter-spacing:.04em;border-bottom:1px solid var(--border)}}
td{{padding:.45rem .75rem;border-bottom:1px solid var(--border);vertical-align:top}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:var(--bg)}}
code{{background:var(--code-bg);border-radius:4px;padding:.1em .35em;font-family:"SF Mono","Fira Code",monospace;font-size:.82em;word-break:break-all}}
.search-bar{{margin-bottom:1rem}}
.search-bar input{{border:1px solid var(--border);border-radius:6px;padding:.4rem .75rem;font-size:.82rem;width:100%;max-width:320px;outline:none}}
.search-bar input:focus{{border-color:#6366f1;box-shadow:0 0 0 2px #6366f120}}
</style>
</head>
<body>
<header>
  <h1>🌐 Translation Key Report</h1>
  <p>BeneHR &mdash; Generated {ts} &mdash; Source: <code>{esc(str(src_dir))}</code></p>
</header>

<div class="summary">
  <div class="stat grey"><div class="label">Files Scanned</div><div class="value">{scan.files_scanned}</div></div>
  <div class="stat grey"><div class="label">Keys in Code</div><div class="value">{len(keys_in_code)}</div></div>
  <div class="stat grey"><div class="label">Keys in Dict</div><div class="value">{len(dictionary)}</div></div>
  <div class="stat {'red' if missing_from_dict else 'green'}"><div class="label">Missing</div><div class="value">{len(missing_from_dict)}</div></div>
  <div class="stat {'yellow' if scan.dynamic_hits else 'green'}"><div class="label">Dynamic</div><div class="value">{len(scan.dynamic_hits)}</div></div>
  <div class="stat {'yellow' if unused_in_code else 'green'}"><div class="label">Orphaned</div><div class="value">{len(unused_in_code)}</div></div>
</div>

<section>
  <div class="section-header">❌ Missing from Dictionary <span class="count">{len(missing_from_dict)}</span></div>
  <div class="section-body">
    <p class="note">Used in code but not yet in the dictionary. Grouped by key prefix.</p>
    {missing_tables_html()}
  </div>
</section>

<section>
  <div class="section-header">⚠️ Dynamic Keys — Manual Review <span class="count" style="background:var(--yellow)">{len(scan.dynamic_hits)}</span></div>
  <div class="section-body">
    <p class="note">Computed or variable key names — cannot be verified automatically.</p>
    {dynamic_table_html()}
  </div>
</section>

<section>
  <div class="section-header">🗑️ Orphaned Dictionary Keys <span class="count orphan">{len(unused_in_code)}</span></div>
  <div class="section-body">
    <p class="note">In the dictionary but not found in any source file. Verify before deleting.</p>
    {orphan_tables_html()}
  </div>
</section>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Renderers map
# ---------------------------------------------------------------------------

RENDERERS = {"txt": render_txt, "md": render_md, "html": render_html}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="i18n key checker — finds missing/orphaned translation keys.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--project", help="Root directory of the frontend project")
    parser.add_argument("--src",     help="Source subdirectory to scan (overrides --project/src)")
    parser.add_argument("--csv",     help="Path to the exported dictionary CSV")
    parser.add_argument("--ext",     nargs="+", help="File extensions to scan (e.g. .ts .tsx .js .jsx)")
    parser.add_argument("--format",  choices=["txt", "md", "html"], help="Output format (default: txt)")
    parser.add_argument("--out",     help="Save report to this file path")
    parser.add_argument("--sql-out", help="Generate SQL INSERT file at this path")
    parser.add_argument("--translate", action="store_true",
                        help="Auto-translate German via Google Translate (requires: pip install deep-translator)")
    parser.add_argument("--show-occurrences", action="store_true",
                        help="Include file:line for each missing key in the report")
    return parser.parse_args()


def main():
    args   = parse_args()
    config = load_config()
    cfg    = resolve_config(args, config)

    src_dir  = Path(cfg["src"]).resolve()
    fmt      = cfg["format"]
    out_path = Path(cfg["out"]).resolve() if cfg["out"] else None
    sql_path = Path(cfg["sql_out"]).resolve() if cfg["sql_out"] else None
    translate = cfg["translate"]

    # CSV is required — no default since it lives outside the project
    csv_raw = cfg.get("csv")
    if not csv_raw:
        print("[ERROR] No dictionary CSV specified.")
        print("        Use --csv /path/to/dictionary.csv  or set csv in .check_translations.toml")
        sys.exit(1)
    csv_path = Path(csv_raw).resolve()

    if not src_dir.exists():
        print(f"[ERROR] Source directory not found: {src_dir}")
        sys.exit(1)

    print(f"Dictionary : {csv_path}")
    dictionary = load_dictionary(csv_path)
    print(f"           → {len(dictionary)} keywords loaded\n")

    scan   = scan_project(src_dir, cfg["extensions"])
    render = RENDERERS[fmt]
    report = render(scan, dictionary, src_dir, cfg["show_occurrences"])

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        print(f"\nReport saved → {out_path}")
    else:
        print(report)

    if sql_path:
        keys_in_code      = set(scan.static_keys.keys())
        missing_from_dict = sorted(keys_in_code - dictionary)
        sql_path.parent.mkdir(parents=True, exist_ok=True)
        generate_sql(missing_from_dict, translate, sql_path)

    missing_count = len(set(scan.static_keys.keys()) - dictionary)
    sys.exit(1 if missing_count > 0 else 0)


if __name__ == "__main__":
    main()