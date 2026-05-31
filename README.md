# Fusion 360 MCP Server

This repository provides a Fusion 360 add-in that exposes the active design through the Model Context Protocol (MCP). It is designed for local coding agents such as Codex that need to inspect, plan, modify, and export Fusion 360 geometry from an automated workflow.

## Repository contents

- `MCPserve/`: Fusion 360 add-in source code.
- `MCPserve/MCPserve.py`: add-in entrypoint.
- `MCPserve/commands/MCPServerCommand.py`: main MCP server implementation.
- `MCPserve/MCPserve.manifest`: Fusion 360 manifest file.
- `client.py`: local test and smoke-test client for SSE and file-based communication.
- `install_mcp_for_fusion.py`: helper that installs `mcp[cli]` and `uvicorn` into Fusion 360's bundled Python runtime.
- `deploy_addin_to_fusion.py`: helper that copies the add-in into Fusion 360's user Add-Ins folder.
- `AGENTS.md`: development and validation guidance for coding agents.

## What this server does

The server exposes Fusion 360 data and modeling actions through MCP so an agent can:

- Read the active document, parameters, sketches, bodies, and overall design structure.
- Create parameters and construction geometry for parametric workflows.
- Create and inspect sketches.
- Add sketch geometry such as points, lines, polylines, circles, rectangles, arcs, and splines.
- Apply sketch constraints and driving dimensions.
- Create 3D features such as extrudes and revolves.
- Create offset planes and reusable modeling references.
- Create components when the active Fusion document supports them.
- Delete bodies created during iterative modeling.
- Export sketches and designs to CAD exchange formats.
- Use prompt helpers for sketch planning, parameter planning, and modeling strategy generation.

## Communication model

The add-in exposes two communication paths:

1. HTTP SSE at `http://127.0.0.1:3000/sse`
2. File-based fallback through `mcp_comm/`

The SSE endpoint is the primary path. The file-based path is intentionally preserved for local testing, recovery scenarios, and clients that cannot attach directly to the HTTP endpoint.

## Feature overview

### Resources

- `fusion://active-document-info`
- `fusion://design-structure`
- `fusion://parameters`
- `fusion://components`
- `fusion://sketches`
- `fusion://bodies`

### Modeling tools

- `message_box`
- `create_new_sketch`
- `create_parameter`
- `create_component`
- `create_offset_plane`
- `list_sketch_entities`
- `list_sketch_profiles`
- `create_sketch_point`
- `create_sketch_line`
- `create_sketch_lines`
- `create_sketch_circle`
- `create_sketch_rectangle`
- `create_sketch_center_rectangle`
- `create_sketch_arc`
- `create_sketch_spline`
- `add_sketch_constraint`
- `add_sketch_dimension`
- `create_extrude`
- `create_revolve`
- `delete_body`

### Export tools

- `export_sketch_dxf`
- `export_design_file`
- `export_active_drawing_pdf`

### Prompt helpers

- `create_sketch_prompt`
- `parameter_setup_prompt`
- `feature_strategy_prompt`

## Advanced workflow coverage

This project is not limited to a minimal sketch demo. The current implementation supports a broader automation workflow:

- Parametric setup with named user parameters.
- Construction-plane creation for stacked or offset modeling operations.
- Sketch authoring with token-based entity tracking.
- Constraint-driven sketch refinement.
- Dimension-driven profile control.
- 3D solid generation through extrude and revolve features.
- Iterative cleanup by removing obsolete bodies.
- Export to `STEP`, `IGES`, `SAT`, `STL`, `3MF`, `OBJ`, `DXF`, and drawing `PDF`.
- File-based fallback commands for smoke testing even when direct MCP transport is unavailable.

## Requirements

- Autodesk Fusion 360
- Windows or macOS
- Python 3.7+ for `client.py`, `deploy_addin_to_fusion.py`, and `install_mcp_for_fusion.py`
- `mcp[cli]` and `uvicorn` installed inside Fusion 360's bundled Python

## Installation

### 1. Install Python packages into Fusion 360's Python

Fusion 360 ships with its own Python runtime. MCP must be installed there, not only in your system Python.

Recommended:

```powershell
python install_mcp_for_fusion.py
```

Manual fallback:

```powershell
"[Fusion Python Path]\\python.exe" -m pip install "mcp[cli]" uvicorn
```

### 2. Install the add-in into Fusion 360

Option A, manual import:

1. Open `Tools -> Scripts and Add-Ins`.
2. In `My Add-Ins`, click the green `+`.
3. Select the `MCPserve` folder from this repository.
4. Run the add-in named `MCP Server`.

Option B, copy into Fusion's Add-Ins folder:

```powershell
python deploy_addin_to_fusion.py
```

Then open Fusion 360 and run `MCPserve` from `Tools -> Scripts and Add-Ins`.

## Codex and agent workflow

This repository is prepared for agent-driven development and testing.

- Read `AGENTS.md` first for execution boundaries.
- Edit the repository normally.
- Run syntax checks locally.
- Run functional validation inside Fusion 360 because the `adsk` API only exists there.

Recommended checks:

```powershell
python -m py_compile client.py install_mcp_for_fusion.py deploy_addin_to_fusion.py MCPserve\MCPserve.py MCPserve\commands\MCPServerCommand.py MCPserve\lib\fusionAddInUtils.py
python client.py --wait-ready --test-connection
python client.py --list-resources --list-tools --list-prompts
```

## Client usage examples

Basic connection test:

```powershell
python client.py --wait-ready --test-connection
```

List MCP objects:

```powershell
python client.py --list-resources
python client.py --list-tools
python client.py --list-prompts
```

Read a specific resource:

```powershell
python client.py --test-resource fusion://bodies
```

Run simple tool tests:

```powershell
python client.py --test-message-box --message "Hello from Fusion MCP"
python client.py --test-sketch --plane XY
python client.py --test-parameter --param-name Width --param-expression 25 --param-unit mm
```

Force a specific file-based communication directory:

```powershell
python client.py --comm-dir .\MCPserve\mcp_comm --list-tools
```

## Troubleshooting

- If Fusion cannot import the add-in, confirm that `MCPserve/MCPserve.manifest` exists and matches the folder name.
- If the add-in starts but the client cannot connect, inspect `mcp_comm/server_status.json` and `mcp_comm/mcp_server_error.txt`.
- If the SSE endpoint is unavailable, use the file-based path from `client.py`.
- If package imports fail inside Fusion, rerun `install_mcp_for_fusion.py` against Fusion's bundled Python.
- If the active document is a single-part design, component creation may be limited by Fusion 360 document rules.

## References

- [Fusion 360 API: Creating a Script or Add-In](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/WritingDebugging_UM.htm)
- [OpenAI Codex Help Center](https://help.openai.com/en/collections/14937394-codex)
