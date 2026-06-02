# Fusion 360 MCP Server

This repository provides a Fusion 360 add-in that exposes the active Fusion session through the Model Context Protocol (MCP). It is designed for local coding agents such as Codex that need to inspect, plan, modify, export, and automate both Fusion mechanical design data and Fusion Electronics data from an automated workflow.

Once the add-in is installed into Fusion 360's Add-Ins directory, it can run from there without requiring this development repository to remain in place. The repository is mainly for development, packaging, and testing.

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
- Read the active Fusion Electronics context, including open electronics documents, schematic state, board state, and libraries.
- Create parameters and construction geometry for parametric workflows.
- Create and inspect sketches.
- Add sketch geometry such as points, lines, polylines, circles, rectangles, arcs, and splines.
- Apply sketch constraints and driving dimensions.
- Create 3D features such as extrudes and revolves.
- Create offset planes and reusable modeling references.
- Create components when the active Fusion document supports them.
- Delete bodies created during iterative modeling.
- Export sketches and designs to CAD exchange formats.
- Create electronics sheets and manage electronics transaction boundaries safely.
- Upload and activate electronics projects and library files inside Fusion Electronics.
- Export active electronics schematic, board, and library documents.
- Use prompt helpers for sketch planning, parameter planning, and modeling strategy generation.
- Inspect live Fusion API objects dynamically.
- Execute arbitrary Fusion API Python against the live session when a fixed tool is not enough.
- Execute live Fusion Electronics API and text commands for workflows that do not yet have a fixed dedicated MCP tool.

## Communication model

The add-in exposes two communication paths:

1. HTTP SSE at `http://127.0.0.1:3000/sse`
2. File-based fallback through `mcp_comm/`

The SSE endpoint is the primary path. The file-based path is intentionally preserved for local testing, recovery scenarios, and clients that cannot attach directly to the HTTP endpoint.

Recent stability improvements:

- File-based commands preserve the full `command_<id>.json` identifier, including IDs that contain underscores.
- Fusion API work requested through MCP is marshaled onto Fusion's main thread instead of being executed directly from background monitor threads.

## Feature overview

### Resources

- `fusion://active-document-info`
- `fusion://design-structure`
- `fusion://parameters`
- `fusion://components`
- `fusion://sketches`
- `fusion://bodies`
- `fusion://mcp-capabilities`

### Electronics resources

- `fusion://electronics-context`
- `fusion://electronics-schematic`
- `fusion://electronics-board`
- `fusion://electronics-library`
- `fusion://electronics-libraries`
- `fusion://electronics-documents`
- `fusion://electronics-errors`

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

### Generic bridge tools

- `inspect_fusion_object`
- `execute_fusion_api`

### Electronics tools

- `create_electronics_sheet`
- `begin_electronics_change`
- `end_electronics_change`
- `cancel_electronics_change`
- `list_electronics_documents`
- `upload_electronics_project`
- `open_electronics_document`
- `activate_electronics_document`
- `export_electronics_file`
- `execute_text_command`
- `execute_electronics_api`

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
- Main-thread dispatch for Fusion API calls to reduce instability during heavier modeling or export operations.
- Dynamic live inspection of Fusion API objects, collections, and selected entities.
- A general-purpose execution bridge for unsupported or newly needed Fusion API features without waiting for a dedicated MCP tool.
- Fusion Electronics context inspection for active schematic, board, library, and project documents.
- Electronics document upload and activation workflows for `SCH`, `BRD`, `LBR`, and their Fusion-managed `FSCH`, `FBRD`, `FLBR`, and `FPRJ` counterparts.
- Electronics sheet creation and explicit transaction control for safer scripted edits.
- Round-trip export of live Fusion Electronics documents back to electronics file formats.
- A live electronics execution bridge for schematic and board automation when a dedicated tool is not yet present.

## Runtime mode

The add-in supports two practical modes:

1. Development mode: run from this repository while editing code and using helper scripts.
2. Installed mode: run directly from Fusion 360's user Add-Ins folder with Codex or another MCP client, without depending on this repository path.

In installed mode, the add-in still creates runtime artifacts such as `mcp_comm/` and export output near the installed add-in when needed.

## Requirements

- Autodesk Fusion 360
- Windows or macOS
- Python 3.7+ for `client.py`, `deploy_addin_to_fusion.py`, and `install_mcp_for_fusion.py`
- `mcp[cli]` and `uvicorn` installed inside Fusion 360's bundled Python

On recent builds, the add-in also attempts to install missing runtime packages automatically on first launch by invoking Fusion 360's bundled `python.exe`. Manual installation is still available as a fallback.

## Installation

### 1. Install Python packages into Fusion 360's Python

Fusion 360 ships with its own Python runtime. MCP must be installed there, not only in your system Python.

Recommended:

```powershell
python install_mcp_for_fusion.py
```

Non-interactive setup:

```powershell
python install_mcp_for_fusion.py --yes
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

For direct MCP use from Codex, the most important requirement is that Fusion 360 is open and the `MCPserve` add-in is running. After that, a client can connect directly to `http://127.0.0.1:3000/sse`.

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
python client.py --test-resource fusion://mcp-capabilities
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

## Generic Fusion API bridge

The server now includes a generic execution path so an MCP client is not limited to the fixed built-in tool list.

- `inspect_fusion_object` inspects a live object path such as `design.rootComponent.features`, `root_component.sketches`, or `cam.setups`.
- `execute_fusion_api` runs Python inside the live Fusion session with useful prebound objects and helpers.

The execution context includes:

- `app`, `ui`, `doc`, `active_product`
- `design`, `drawing`, `cam`
- `root_component`
- `selected_entities`, `selected_tokens`
- `find_component`, `find_sketch`, `find_body`, `find_plane`, `find_axis`, `find_entity`
- `find_profile_entity`
- `point3d`, `point2d`
- `length_value`, `length_input`, `angle_value`, `angle_input`
- `exports_dir`, `comm_dir`
- `state` for lightweight cross-call memory

This means a Codex session can reach most of the public Fusion API surface even if a dedicated MCP tool has not been implemented yet.

## Fusion Electronics support

The server now includes a first-class Fusion Electronics layer in addition to the mechanical modeling tools.

- `fusion://electronics-context` reports the active electronics product state and linked documents.
- `fusion://electronics-schematic` and `fusion://electronics-board` provide lightweight live summaries that are safer to query repeatedly than deep raw serialization.
- `list_electronics_documents`, `open_electronics_document`, and `activate_electronics_document` help recover and control active `FSCH`, `FBRD`, `FPRJ`, and `FLBR` documents.
- `upload_electronics_project` supports loading external electronics content into Fusion-managed documents.
- `export_electronics_file` supports exporting the active live electronics document back out for handoff, verification, or versioning.
- `execute_electronics_api` and `execute_text_command` provide an escape hatch for advanced electronics workflows that do not yet have a fixed MCP tool.

In practice, this means a Codex workflow can inspect existing Fusion Electronics projects, create or activate sheets, upload generated electronics files, reopen them inside Fusion, verify their live schematic and board state, and export the result again without leaving the MCP loop.

## Troubleshooting

- If Fusion cannot import the add-in, confirm that `MCPserve/MCPserve.manifest` exists and matches the folder name.
- If the add-in starts but the client cannot connect, inspect `mcp_comm/server_status.json` and `mcp_comm/mcp_server_error.txt`.
- If the SSE endpoint is unavailable, use the file-based path from `client.py`.
- If package imports fail inside Fusion, rerun `install_mcp_for_fusion.py` against Fusion's bundled Python.
- If the add-in reports missing packages on first run, wait for the automatic installer to finish and inspect `mcp_comm/mcp_runtime_install.log` for the exact `pip` output.
- If the active document is a single-part design, component creation may be limited by Fusion 360 document rules.
- If a needed feature is not covered by the fixed tools, use the generic bridge described above instead of treating it as unsupported.
- If Fusion Electronics reports an intermittent document-validation error while opening an uploaded project document, inspect `fusion://electronics-documents` and activate the schematic or board document directly. The linked documents may still be valid even when the project wrapper reports a Fusion-side validation issue.

## References

- [Fusion 360 API: Creating a Script or Add-In](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/WritingDebugging_UM.htm)
- [OpenAI Codex Help Center](https://help.openai.com/en/collections/14937394-codex)
