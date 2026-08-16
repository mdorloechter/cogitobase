"""End-to-end Streamable HTTP check: boot the real server via uvicorn and drive a
full protocol handshake with the official MCP client (initialize + tools/list +
one tool round-trip) over the /mcp transport. Complements e2e_server.py (which
covers the auth/ingress middleware via TestClient). Run directly; exits 0 on pass.

    python tests/e2e_streamable.py
"""
import os
import sys
import asyncio
import contextlib
from pathlib import Path

os.environ["AUTH_TOKEN"] = "e2e-streamable-token-7a4e0b3f"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import git_sync
git_sync.init_git_repo = lambda: None  # no real clone/push

import uvicorn
import server
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession


async def main():
    config = uvicorn.Config(server.starlette_app, host="127.0.0.1", port=8123, log_level="warning")
    uv = uvicorn.Server(config)
    serve_task = asyncio.create_task(uv.serve())
    # wait for startup
    for _ in range(50):
        if uv.started:
            break
        await asyncio.sleep(0.1)

    headers = {"Authorization": f"Bearer {os.environ['AUTH_TOKEN']}"}
    try:
        async with streamablehttp_client("http://127.0.0.1:8123/mcp", headers=headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                init_result = await session.initialize()
                # server.py's lifespan() must set `instructions` on the MCP Server
                # (module-level `app`), not on whatever object Starlette passes
                # into the lifespan callback.
                assert init_result.instructions, "initialize() returned no instructions"
                assert "get_core_context" in init_result.instructions, init_result.instructions[:200]
                print(f"INSTRUCTIONS OK — {len(init_result.instructions)} chars")
                tools = await session.list_tools()
                names = sorted(t.name for t in tools.tools)
                print(f"HANDSHAKE OK — {len(names)} tools")
                assert "write_note" in names and "resume_sync" in names, names
                # exercise one real tool round-trip
                res = await session.call_tool("sync_status", {})
                txt = res.content[0].text
                print("sync_status ->", txt[:60])
                assert "sync" in txt.lower()
        print("==== STREAMABLE HTTP E2E: PASS ====")
        rc = 0
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"==== STREAMABLE HTTP E2E: FAIL ({e}) ====")
        rc = 1
    finally:
        uv.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(serve_task, timeout=10)
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
