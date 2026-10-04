#!/usr/bin/env python3
"""開発用チェック: YAML妥当性・cron5フィールド・Python構文・実行時必須名・文字化けを検証する。

使い方:
    python check_all.py
"""
import ast
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).parent


def check_cron(expr):
    """GitHub Actions の schedule cron は5フィールド必須。4フィールドは起動不可になる。"""
    parts = expr.strip().split()
    return len(parts) == 5


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
        if isinstance(schedule, list):
            for entry in schedule:
                cron = entry.get("cron", "") if isinstance(entry, dict) else ""
                if cron and not check_cron(cron):
                    print(f"  [FAIL] {f.name}: cron は5フィールド必須: {cron!r}")
                    ok = False
    return ok


def check_runtime_imports():
    """実行時に NameError になる既知リスク名を検出する。

    過去の実績: recorder.py で format_bytes 未定義・datetime が
    `if __name__` 内遅延 import のみ、という脆い状態があった。
    汎用スキャンは関数引数を誤検出するため、トップレベル定義の有無に絞る。
    """
    print("=== Runtime name check ===")
    ok = True
    # ファイルごとに「トップレベルで定義必須」の名前
    required = {
        "recorder.py": {"format_bytes", "datetime"},
        "twitch_recorder.py": set(),
    }
    for f in sorted(ROOT.glob("*.py")):
        if f.name == "check_all.py" or f.name not in required:
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        except SyntaxError as e:
            print(f"  [FAIL] {f.name}: {e}")
            ok = False
            continue
        top_defined = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                top_defined.add(node.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    top_defined.add((a.asname or a.name).split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    top_defined.add(a.asname or a.name)
        want = required[f.name]
        missing = sorted(n for n in want if n not in top_defined)
        if missing:
            print(f"  [FAIL] {f.name}: トップレベル未定義: {missing}")
            ok = False
        else:
            print(f"  [OK] {f.name}: 必須名 OK ({sorted(want)})")
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
        "runtime_names": check_runtime_imports(),
        "encoding": check_encoding(),
    }
    print("\n=== SUMMARY ===")
    for k, v in results.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
