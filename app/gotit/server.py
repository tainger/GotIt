from contextlib import asynccontextmanager
from pathlib import Path
import secrets
import threading
import http.client
import subprocess
import sys

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .jobs import Store
from .network import SafeHTTP, UserError
from .parser import DouyinParser
from .browser import BrowserResolver

ROOT = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).parent / 'static'


class ResolveBody(BaseModel):
    text: str = Field(min_length=1, max_length=5000)


class DownloadBody(BaseModel):
    result_id: str = Field(pattern=r'^[a-f0-9]{32}$')


def create_app(root=None, parser=None, network=None):
    network = network or SafeHTTP()
    parser = parser or DouyinParser(network, BrowserResolver())
    token = secrets.token_urlsafe(32)
    resolving = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        app.state.store = Store(root or ROOT / 'downloads', parser, network)
        yield
        app.state.store.close()

    app = FastAPI(title='GotIt', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])

    @app.middleware('http')
    async def local_only(request: Request, call_next):
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'detail': '请从本地页面访问。'}, status_code=403)
        if request.url.path.startswith('/api/') and request.method == 'POST':
            if request.headers.get('x-gotit-token') != token:
                return JSONResponse({'detail': '页面会话已过期，请刷新。'}, status_code=403)
            # Read bounded bodies before Pydantic parses them.
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 32768:
                    return JSONResponse({'detail': '输入内容过长。'}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.exception_handler(UserError)
    async def user_error(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.get('/')
    def index():
        return FileResponse(STATIC / 'index.html')

    @app.get('/api/config')
    def config():
        return {'token': token, 'download_dir': str(root or ROOT / 'downloads'), 'max_mb': 200}

    @app.post('/api/resolve')
    def resolve(body: ResolveBody):
        if not resolving.acquire(blocking=False):
            raise HTTPException(429, '正在解析另一条链接，请稍后再试。')
        try:
            result = parser.resolve(body.text)
            return app.state.store.add_result(result)
        except (OSError, TimeoutError, http.client.HTTPException):
            raise UserError('无法连接视频来源，请检查网络后重试。') from None
        finally:
            resolving.release()

    @app.post('/api/download')
    def download(body: DownloadBody):
        return app.state.store.start(body.result_id)

    @app.get('/api/tasks')
    def tasks():
        return app.state.store.list_jobs()

    @app.get('/api/files/{jid}')
    def file(jid: str, preview: bool = False):
        path = app.state.store.file(jid)
        return FileResponse(path, media_type='video/mp4', filename=path.name,
                            content_disposition_type='inline' if preview else 'attachment')

    @app.post('/api/files/{jid}/reveal')
    def reveal(jid: str):
        path = app.state.store.file(jid).resolve()
        if sys.platform != 'darwin':
            raise UserError('打开文件位置目前仅支持 macOS。')
        try:
            subprocess.run(['/usr/bin/open', '-R', str(path)], check=True,
                           timeout=10, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            raise UserError('无法打开 Finder，请确认文件仍在下载目录中。') from None
        return {'ok': True}

    app.mount('/static', StaticFiles(directory=STATIC), name='static')
    return app


app = create_app()
