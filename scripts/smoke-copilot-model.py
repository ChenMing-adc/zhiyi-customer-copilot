"""Opt-in real provider smoke test. Uses .env, MOCK input and a temporary DB."""
import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.copilot.store import Store
from app.copilot.importer import import_workbook
from app.copilot.context import get_context
from app.copilot.analysis import analyze

root = Path(__file__).resolve().parents[1]
with TemporaryDirectory() as folder:
    store = Store(Path(folder) / "smoke.sqlite3")
    import_workbook(store, Path(os.getenv("ZHIYI_SOURCE_PATH", root.parent / "赛事方数据/赛题 1：数据共情者-业务数据.xlsx")))
    result = asyncio.run(analyze(store, get_context(store, "S00053"), "smoke"))
    report = {k: result[k] for k in ['provider','model','status','intent','emotion','trend','summary','draft','calls','elapsed_ms','error','trace']}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    store.engine.dispose()
