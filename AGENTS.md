# AGENTS.md

## Purpose

This repository contains a Fusion 360 add-in that exposes a local MCP server for coding agents such as Codex.

## Runtime boundaries

- `client.py` and `install_mcp_for_fusion.py` run with regular Python on Windows.
- `MCPserve/MCPserve.py` and `MCPserve/commands/MCPServerCommand.py` run inside Fusion 360 and import `adsk`.
- Do not try to functionally execute the add-in outside Fusion 360. Syntax checks are fine; real validation must happen inside Fusion.

## Important files

- `MCPserve/MCPserve.manifest`: Fusion add-in manifest. The folder name, manifest name, and main Python file name must stay aligned as `MCPserve`.
- `MCPserve/commands/MCPServerCommand.py`: main MCP server implementation, SSE endpoint, and file-based fallback.
- `MCPserve/lib/fusionAddInUtils.py`: minimal helper shim required by the add-in entrypoint.
- `client.py`: local test client for SSE and file-based communication.
- `install_mcp_for_fusion.py`: installs `mcp[cli]` and `uvicorn` into Fusion 360's bundled Python.

## Communication model

- Primary transport: HTTP SSE at `http://127.0.0.1:3000/sse`.
- Fallback transport: files in `mcp_comm/`.
- Keep both transports working when editing the server; the client depends on the file-based fallback for smoke tests.

## Common commands

```powershell
python -m py_compile client.py install_mcp_for_fusion.py MCPserve\MCPserve.py MCPserve\commands\MCPServerCommand.py MCPserve\lib\fusionAddInUtils.py
python client.py --wait-ready --test-connection
python client.py --list-resources --list-tools --list-prompts
python client.py --test-message-box --message "Hello from Codex"
```

## Manual verification checklist

1. In Fusion 360, open `Tools -> Scripts and Add-Ins`.
2. Import the `MCPserve` folder if it is not already listed.
3. Run the `MCP Server` add-in command.
4. Confirm that `mcp_server_ready.txt` or `mcp_comm/server_status.json` appears.
5. Run `python client.py --wait-ready --test-connection`.
6. If SSE fails, test the file fallback with `python client.py --list-tools`.

## Editing guidance

- Prefer repo-relative paths and `Path` objects over hard-coded user directories.
- Treat `mcp_comm/` output as generated runtime data.
- If you add new tools, resources, or prompts, update both the live MCP registration and the file-based fallback metadata.
