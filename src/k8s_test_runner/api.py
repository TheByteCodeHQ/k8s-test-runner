"""The control-plane API and dashboard."""

from __future__ import annotations

import secrets
from collections import OrderedDict
from pathlib import Path
from typing import Literal
from xml.etree.ElementTree import ParseError

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import Settings
from .executors import Executor, RunFinished, RunNotFound, TooManyRuns, UnknownSuite, make_executor
from .models import Results, Run, RunRequest, SuiteInfo
from .results import extract_junit, parse_junit, strip_junit
from .selector import SelectorError

STATIC = Path(__file__).parent / "static"
RESULT_CACHE_SIZE = 200
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def create_app(settings: Settings, executor: Executor | None = None, *, api_token: str | None = None,
               allow_no_auth: bool = False) -> FastAPI:
    if not api_token and not allow_no_auth:
        raise RuntimeError("Set KTR_API_TOKEN to protect the API (or KTR_ALLOW_NO_AUTH=1 for local development only).")
    executor = executor or make_executor(settings)
    app = FastAPI(title="k8s-test-runner", version=__version__)
    bearer = HTTPBearer(auto_error=False)
    cache: OrderedDict[str, Results] = OrderedDict()

    def auth(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
        if not api_token:
            return
        supplied = credentials.credentials if credentials else ""
        if not secrets.compare_digest(supplied.encode(), api_token.encode()):
            raise HTTPException(401, "Missing or invalid token", headers={"WWW-Authenticate": "Bearer"})

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

    def lookup(run_id: str) -> Run:
        try:
            return executor.get(run_id)
        except RunNotFound:
            raise HTTPException(404, f"Unknown run '{run_id}'") from None

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"status": "ok", "executor": executor.name}

    @app.get("/api/suites", response_model=list[SuiteInfo], dependencies=[Depends(auth)])
    def suites() -> list[SuiteInfo]:
        return [SuiteInfo(name=s.name, description=s.description, image=s.image,
                          accepts_selector=s.allow_selector,
                          allowed_options=list(s.allowed_flags) if s.allow_selector else [],
                          timeout_seconds=s.timeout_seconds) for s in settings.suites.values()]

    @app.post("/api/runs", response_model=Run, status_code=202, dependencies=[Depends(auth)])
    def create_run(body: RunRequest, response: Response) -> Run:
        try:
            run = executor.submit(body.suite, body.selector)
        except UnknownSuite:
            raise HTTPException(404, f"Unknown suite '{body.suite}'") from None
        except SelectorError as exc:
            raise HTTPException(422, str(exc)) from None
        except TooManyRuns as exc:
            raise HTTPException(429, str(exc), headers={"Retry-After": "30"}) from None
        response.headers["Location"] = f"/api/runs/{run.id}"
        return run

    @app.get("/api/runs", response_model=list[Run], dependencies=[Depends(auth)])
    def list_runs(limit: int = Query(20, ge=1, le=100)) -> list[Run]:
        return executor.list(limit)

    @app.get("/api/runs/{run_id}", response_model=Run, dependencies=[Depends(auth)])
    def get_run(run_id: str) -> Run:
        return lookup(run_id)

    @app.get("/api/runs/{run_id}/logs", response_class=PlainTextResponse, dependencies=[Depends(auth)])
    def get_logs(run_id: str, tail: int = Query(500, ge=1, le=5000)) -> str:
        lookup(run_id)
        return strip_junit(executor.logs(run_id, tail_lines=tail))

    @app.get("/api/runs/{run_id}/results", response_model=Results, dependencies=[Depends(auth)])
    def get_results(run_id: str, outcome: Literal["failed", "passed", "skipped", "all"] = "all") -> Results:
        run = lookup(run_id)
        if not run.status.finished:
            raise HTTPException(409, "The run has not finished yet")
        results = cache.get(run_id)
        if results is None:
            xml = extract_junit(executor.logs(run_id, tail_lines=20))
            if xml is None:
                raise HTTPException(404, "This run produced no results file. See the logs.")
            try:
                results = parse_junit(xml, run_id)
            except ParseError:
                raise HTTPException(422, "The results file is not valid JUnit XML") from None
            cache[run_id] = results
            while len(cache) > RESULT_CACHE_SIZE:
                cache.popitem(last=False)
        if outcome == "all":
            return results
        keep = ("failed", "error") if outcome == "failed" else (outcome,)
        return results.model_copy(update={"results": [r for r in results.results if r.outcome in keep]})

    @app.post("/api/runs/{run_id}/cancel", response_model=Run, dependencies=[Depends(auth)])
    def cancel(run_id: str) -> Run:
        try:
            return executor.cancel(run_id)
        except RunNotFound:
            raise HTTPException(404, f"Unknown run '{run_id}'") from None
        except RunFinished as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
