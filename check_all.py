#!/usr/bin/env python3
"""開発用チェック: YAML妥当性・Python構文・文字化け・CRT日本文字混入を検証する。

使い方:
    python check_all.py
"""
import ast
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).parent


def check_workflows():
    print("=== YAML workflows ===")
    ok = True
    for f in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        try:
            d = yaml.safe_load(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [FAIL] {f.name}: YAML parse error: {e}")
            ok = False
            continue
        on = d.get(True, d.get("on"))
        schedule = on.get("schedule") if isinstance(on, dict) else None
        jobs = list(d.get("jobs", {}).keys())
        print(f"  [OK] {f.name}: jobs={jobs} schedule={schedule}")
    return ok


def check_python():
    print("=== Python syntax ===")
    ok = True
    for f in sorted(ROOT.glob("*.py")):
        if f.name == "check_all.py":
            continue
        try:
            ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            print(f"  [OK] {f.name}")
        except SyntaxError as e:
            print(f"  [FAIL] {f.name}: {e}")
            ok = False
    return ok


def check_encoding():
    print("=== Encoding / mojibake scan ===")
    hangeul = re.compile(r"[\uac00-\ud7af]")
    ok = True
    targets = [".gitignore", ".gitattributes", "requirements.txt"]
    files = [p for p in sorted(ROOT.rglob("*"))
             if p.is_file() and ".git/" not in str(p).replace("\\", "/")
             and "__pycache__" not in str(p)]
    for p in files:
        if p.suffix not in {".py", ".yml", ".sh", ".md", ".txt"} and p.name not in targets:
            continue
        try:
            t = p.read_text(encoding="utf-8")
        except Exception as e:
            print(f"  [FAIL] {p.name}: not valid UTF-8: {e}")
            ok = False
            continue
        if hangeul.search(t):
            print(f"  [FAIL] {p.name}: contains Hangul (mojibake)")
            ok = False
        if "\ufffd" in t:
            print(f"  [FAIL] {p.name}: contains U+FFFD replacement char")
            ok = False
    if ok:
        print("  [OK] no mojibake detected")
    return ok


def main():
    results = {
        "workflows": check_workflows(),
        "python": check_python(),
        "encoding": check_encoding(),
    }
    print("\n=== SUMMARY ===")
    for k, v in results.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
