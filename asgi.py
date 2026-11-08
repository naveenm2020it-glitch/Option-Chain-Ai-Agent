"""Combined website + authenticated Streamable HTTP MCP ASGI entrypoint."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import app as website_app
from mcp_server import mcp, protected_mcp_app


@asynccontextmanager
async def lifespan(_app: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(
    title="NIFTY Option Chain AI + MCP",
    version="1.1.0",
    lifespan=lifespan,
)
# Mount MCP before the website catch-all so /mcp remains reachable.
app.mount("/mcp", protected_mcp_app(), name="mcp")
app.mount("/", website_app, name="website")
