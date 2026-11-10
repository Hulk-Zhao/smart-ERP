"""smart-ERP API 入口。

本地启动：``python -m apps.main``
健康检查：``GET /healthz``（不检查任何依赖，避免依赖抖动引发重启风暴，C13）
"""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI

from apps.platform.config import Settings, get_settings

SERVICE_NAME = "smart-erp-api"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="smart-ERP API",
        version="0.1.0",
        root_path=settings.api_root_path,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": SERVICE_NAME, "image_tag": settings.image_tag}

    return app


app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "apps.main:app",
        host="0.0.0.0",
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
