"""Default entry: Zhiyi demo. Legacy campus routes and tools are not started."""
from contextlib import asynccontextmanager
from pathlib import Path
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

from app.copilot.api import router
from app.copilot.importer import import_workbook, ImportFailure
from app.copilot.store import Store

ROOT = Path(__file__).resolve().parents[1]


def create_app(db_path=None, source_path=None, auto_import=True):
    @asynccontextmanager
    async def lifespan(app):
        app.state.store = Store(Path(db_path or os.getenv("ZHIYI_DB_PATH", ROOT / "data/zhiyi.sqlite3")))
        app.state.source_path = Path(source_path or os.getenv("ZHIYI_SOURCE_PATH", ROOT.parent / "赛事方数据/赛题 1：数据共情者-业务数据.xlsx"))
        app.state.import_error = None
        if auto_import:
            try:
                import_workbook(app.state.store, app.state.source_path)
            except ImportFailure as exc:
                app.state.import_error = exc.errors
            except OSError:
                app.state.import_error = "未找到官方数据文件，请配置 ZHIYI_SOURCE_PATH"
        yield
        app.state.store.engine.dispose()

    app = FastAPI(title="知意客服副驾", version="0.2.0", lifespan=lifespan)

    @app.middleware("http")
    async def browser_boundary(request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "不允许跨站写入"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/actuator/health")
    def health():
        return {"status": "UP", "product": "zhiyi"}

    app.include_router(router)
    app.mount("/", StaticFiles(directory=ROOT / "app/static/copilot", html=True), name="copilot")
    return app


app = create_app()
