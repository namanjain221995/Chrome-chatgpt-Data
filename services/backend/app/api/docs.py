"""Self-hosted interactive API documentation.

FastAPI's built-in `/docs` loads Swagger UI from a public CDN and initialises
it with an inline `<script>`. Both are blocked by this API's
Content-Security-Policy, and relaxing the policy for them would mean a page
that describes the admin and ingest surface -- and into which an administrator
pastes a bearer token -- executing third-party JavaScript.

Instead the assets are baked into the image (pinned and digest-verified, see
the Dockerfile) and the initialiser is served as a file, so the page needs no
external origin and no `'unsafe-inline'` for scripts.

The routes exist only when `Settings.api_docs_available` is true.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse
from starlette.staticfiles import StaticFiles

from app.core.config import Settings

#: Where the Dockerfile installs the vendored Swagger UI distribution.
SWAGGER_UI_DIR = Path("/opt/swagger-ui")

ASSET_MOUNT = "/static/swagger"
INITIALISER_PATH = "/docs/initialiser.js"
DOCS_PATH = "/docs"
OPENAPI_PATH = "/openapi.json"

#: Relaxed only for the documentation page. The API itself keeps
#: `default-src 'none'`; this adds exactly what Swagger UI needs, all from the
#: origin. `style-src` needs `'unsafe-inline'` because Swagger UI sets element
#: styles at runtime; `script-src` deliberately does not.
DOCS_CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'none'"
)

_INITIALISER = """// Served as a file so the documentation page needs no inline-script
// permission in its Content-Security-Policy.
window.onload = function () {
  window.ui = SwaggerUIBundle({
    url: '%(openapi_url)s',
    dom_id: '#swagger-ui',
    deepLinking: true,
    layout: 'BaseLayout',
    presets: [SwaggerUIBundle.presets.apis],
  });
};
"""

_PAGE = """<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="robots" content="noindex, nofollow">
    <title>%(title)s</title>
    <link rel="stylesheet" href="%(assets)s/swagger-ui.css">
  </head>
  <body>
    <div id="swagger-ui"></div>
    <script src="%(assets)s/swagger-ui-bundle.js"></script>
    <script src="%(initialiser)s"></script>
  </body>
</html>
"""


def assets_present() -> bool:
    """True when the vendored Swagger UI distribution is in the image."""
    return (SWAGGER_UI_DIR / "swagger-ui-bundle.js").is_file() and (
        SWAGGER_UI_DIR / "swagger-ui.css"
    ).is_file()


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.get(DOCS_PATH, response_class=HTMLResponse)
    async def swagger_ui() -> HTMLResponse:
        html = _PAGE % {
            "title": f"{settings.app_name} API",
            "assets": ASSET_MOUNT,
            "initialiser": INITIALISER_PATH,
        }
        return HTMLResponse(html, headers={"Content-Security-Policy": DOCS_CSP})

    # Deliberately outside ASSET_MOUNT: a Starlette mount matches its whole
    # prefix, so a route underneath it would never be reached.
    @router.get(INITIALISER_PATH, response_class=PlainTextResponse)
    async def initialiser() -> PlainTextResponse:
        return PlainTextResponse(
            _INITIALISER % {"openapi_url": OPENAPI_PATH},
            media_type="application/javascript",
        )

    return router


def mount(app: FastAPI, settings: Settings) -> bool:
    """Attach the documentation routes. Returns False when assets are absent."""
    if not assets_present():
        return False
    app.include_router(build_router(settings))
    app.mount(
        ASSET_MOUNT,
        StaticFiles(directory=str(SWAGGER_UI_DIR)),
        name="swagger-ui-assets",
    )
    return True
