"""FastAPI application factory for AP-CODEX.

``create_app`` wires the three layers together so tests and production both
inject what they want (``llm``, ``manager``, connector file) without globals.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional, Union

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from connectors import ConnectorManager, register_builtin_connectors
from core.agent import AgentLoop
from core.llm import BaseLLM, LLMOptions, OpenAICompatibleLLM
from core.scenario import ScenarioRunner

from .routes import router
from .auth import GoogleSignInConnector, router as auth_router
from .users import SessionStore, UserStore

logger = logging.getLogger(__name__)

_ORIGINS = os.environ.get(
    "AP_CODEX_CORS_ORIGINS",
    "*",
)

_WEB_ASSETS = ("index.html", "styles.css", "script.js", "chat.js", "auth.js", "favicon.svg")


def create_app(
    llm: Optional[BaseLLM] = None,
    manager: Optional[ConnectorManager] = None,
    connectors_path: Optional[Union[str, os.PathLike]] = None,
    llm_options: Optional[LLMOptions] = None,
    builtin_connectors: bool = True,
    outbox_path: Optional[str] = None,
    scenario_runner: Optional[ScenarioRunner] = None,
    web_root: Optional[Union[str, os.PathLike]] = None,
    users: Optional[UserStore] = None,
    users_path: Optional[Union[str, os.PathLike]] = None,
    signin: Optional[GoogleSignInConnector] = None,
) -> FastAPI:
    """Build a configured AP-CODEX app.

    Args:
        llm: LLM adapter (defaults to :class:`OpenAICompatibleLLM` from env).
        manager: Connector manager (defaults to a fresh instance).
        connectors_path: JSON config file to load connectors from at startup
            (defaults to the ``AP_CODEX_CONNECTORS`` env var, then to a local
            ``connectors.json`` if present).
        builtin_connectors: Register the shopping / rides / email connectors.
        outbox_path: Where the simulated email outbox is persisted.
        scenario_runner: Deterministic planner used before the LLM agent.
        web_root: Directory containing the landing page assets to serve at
            ``/`` (defaults to the repo root when ``index.html`` is present).
    """
    manager = manager or ConnectorManager()
    llm = llm or OpenAICompatibleLLM(llm_options or LLMOptions.from_env())
    agent = AgentLoop(manager=manager, llm=llm)
    if builtin_connectors:
        register_builtin_connectors(manager, outbox_path=outbox_path)
    scenario = scenario_runner or ScenarioRunner(manager, outbox_path=outbox_path)
    path = connectors_path or os.environ.get("AP_CODEX_CONNECTORS")
    if path is None and os.path.exists("connectors.json"):
        path = "connectors.json"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not path or not os.path.exists(path):
            if path:
                logger.warning("connectors file %s not found", path)
        else:
            try:
                await asyncio.to_thread(manager.load_from_config, path)
                logger.info("loaded connectors from %s", path)
            except Exception as exc:  # noqa: BLE001
                logger.exception("failed to load connectors from %s", path)
                raise RuntimeError(f"connector config failed to load: {exc}") from exc
        yield

    app = FastAPI(
        title="AP-CODEX",
        version="0.1.0",
        description="Multi-app AI agent orchestration backend.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in _ORIGINS.split(",")],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _utf8_json(request: Request, call_next):
        response = await call_next(request)
        content_type = response.headers.get("content-type", "")
        if content_type.startswith(("application/json", "text/event-stream")):
            response.headers["content-type"] = (
                content_type.split(";")[0] + "; charset=utf-8"
            )
        return response

    app.state.manager = manager
    app.state.agent = agent
    app.state.llm = llm
    app.state.scenario = scenario

    # ---- user accounts + sessions ----
    storage = users or UserStore(
        os.fspath(users_path) if users_path is not None else os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "users.json",
        )
    )
    app.state.users = storage
    app.state.sessions = SessionStore()
    app.state.signin = signin or GoogleSignInConnector()

    app.include_router(router)
    app.include_router(auth_router)

    root = web_root if web_root is not None else (
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    if os.path.exists(os.path.join(root, "index.html")):
        _serve_web(app, root)

    return app


def _serve_web(app: FastAPI, root: str) -> None:
    """Serve the landing page assets from ``root`` at ``/`` (after API routes)."""

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(os.path.join(root, "index.html"))

    for name in _WEB_ASSETS:
        path = os.path.join(root, name)
        if not os.path.exists(path):
            continue

        @app.get(f"/{name}", include_in_schema=False)
        async def asset(_name: str = name) -> FileResponse:
            return FileResponse(os.path.join(root, _name))


# Module-level instance so `uvicorn src.api.app:app` just works.
try:
    app = create_app()
except Exception:  # pragma: no cover - import time only
    logger.exception("failed to build default AP-CODEX app; call create_app() directly")
    app = FastAPI(title="AP-CODEX (unconfigured)")