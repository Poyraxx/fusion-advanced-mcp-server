#!/usr/bin/env python3

import adsk.core
import adsk.cam
import adsk.drawing
import adsk.fusion
try:
    import adsk.electron
    HAS_ELECTRON_API = True
except Exception:
    HAS_ELECTRON_API = False
import os
import sys
import traceback
import threading
import time
import json
import asyncio
import math
import re
from pathlib import Path

# Global variables
app = adsk.core.Application.get()
ui = app.userInterface
server_thread = None
server_running = False
message_command_handlers = []  # Store command handlers to prevent garbage collection

# Initialize the global handlers list
handlers = []
SCRIPT_STATE = {}
FUSION_MAIN_THREAD_ID = None
MAIN_THREAD_EVENT_ID = "MCPserve_MainThreadCall"
main_thread_custom_event = None
main_thread_event_handler = None
main_thread_requests = {}
main_thread_requests_lock = threading.Lock()
main_thread_request_counter = 0

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 3000
SERVER_URL = f"http://{SERVER_HOST}:{SERVER_PORT}/sse"

RESOURCE_URIS = [
    "fusion://active-document-info",
    "fusion://design-structure",
    "fusion://parameters",
    "fusion://components",
    "fusion://sketches",
    "fusion://bodies",
    "fusion://electronics-context",
    "fusion://electronics-schematic",
    "fusion://electronics-board",
    "fusion://electronics-library",
    "fusion://electronics-libraries",
    "fusion://electronics-documents",
    "fusion://electronics-errors",
    "fusion://mcp-capabilities",
]

TOOL_METADATA = [
    {"name": "message_box", "description": "Display a message box in Fusion 360"},
    {"name": "create_new_sketch", "description": "Create a new sketch on the specified plane"},
    {"name": "create_parameter", "description": "Create a new parameter in the active design"},
    {"name": "create_component", "description": "Create a new component in the active design"},
    {"name": "create_offset_plane", "description": "Create a construction plane offset from an existing plane"},
    {"name": "list_sketch_entities", "description": "List points and curves in a sketch, including entity tokens"},
    {"name": "list_sketch_profiles", "description": "List the available profile indices for a sketch"},
    {"name": "create_sketch_point", "description": "Create a sketch point using sketch-space coordinates"},
    {"name": "create_sketch_line", "description": "Create a sketch line between two sketch-space points"},
    {"name": "create_sketch_lines", "description": "Create a polyline from multiple sketch-space points"},
    {"name": "create_sketch_circle", "description": "Create a sketch circle from a center point and radius"},
    {"name": "create_sketch_rectangle", "description": "Create a two-point sketch rectangle"},
    {"name": "create_sketch_center_rectangle", "description": "Create a center-point sketch rectangle"},
    {"name": "create_sketch_arc", "description": "Create a sketch arc from center, start point, and sweep angle"},
    {"name": "create_sketch_spline", "description": "Create a fitted spline through multiple sketch-space points"},
    {"name": "add_sketch_constraint", "description": "Apply a geometric constraint using sketch entity tokens"},
    {"name": "add_sketch_dimension", "description": "Add a driving sketch dimension using sketch entity tokens"},
    {"name": "create_extrude", "description": "Extrude a sketch profile into a 3D feature"},
    {"name": "create_revolve", "description": "Revolve a sketch profile around an axis entity token"},
    {"name": "delete_body", "description": "Delete a body from the active design"},
    {"name": "export_sketch_dxf", "description": "Export a sketch to a DXF file"},
    {"name": "export_design_file", "description": "Export the active design to STEP, IGES, SAT, STL, 3MF, or OBJ"},
    {"name": "export_active_drawing_pdf", "description": "Export the active drawing document to PDF"},
    {"name": "create_electronics_sheet", "description": "Create a new sheet in the active electronics schematic"},
    {"name": "begin_electronics_change", "description": "Begin an electronics design-change transaction for schematic, board, library, or design"},
    {"name": "end_electronics_change", "description": "Commit the current electronics design-change transaction"},
    {"name": "cancel_electronics_change", "description": "Cancel the current electronics design-change transaction"},
    {"name": "list_electronics_documents", "description": "List open and available electronics schematic, board, project, and library documents"},
    {"name": "upload_electronics_project", "description": "Upload schematic/board/library files into the active Fusion data folder and optionally open them"},
    {"name": "open_electronics_document", "description": "Open an uploaded electronics schematic, board, library, or related project files by name"},
    {"name": "activate_electronics_document", "description": "Activate an already open electronics schematic, board, library, or related project files by name"},
    {"name": "export_electronics_file", "description": "Export the active electronics design as EAGLE SCH, BRD, or LBR"},
    {"name": "execute_text_command", "description": "Run a Fusion text command directly through the active application"},
    {"name": "inspect_fusion_object", "description": "Inspect any live Fusion API object by Python path and list its members"},
    {"name": "execute_fusion_api", "description": "Execute arbitrary Python against the live Fusion API with design, drawing, and CAM context"},
    {"name": "execute_electronics_api", "description": "Execute arbitrary Python against the live Fusion Electronics API with schematic, board, library, and design context"},
]

PROMPT_METADATA = [
    {"name": "create_sketch_prompt", "description": "Create a prompt for creating a sketch based on a description"},
    {"name": "parameter_setup_prompt", "description": "Create a prompt for setting up parameters based on a description"},
    {"name": "feature_strategy_prompt", "description": "Create a prompt for planning sketches and features for a Fusion model"},
]

ADDIN_DIR = Path(__file__).resolve().parents[1]


def _find_repo_root():
    candidate = ADDIN_DIR.parent
    required_entries = ("client.py", "install_mcp_for_fusion.py", "README.md")
    if all((candidate / entry).exists() for entry in required_entries):
        return candidate
    return None


REPO_ROOT = _find_repo_root()
ADDIN_COMM_DIR = ADDIN_DIR / "mcp_comm"
REPO_COMM_DIR = REPO_ROOT / "mcp_comm" if REPO_ROOT else None
PRIMARY_COMM_DIR = REPO_COMM_DIR or ADDIN_COMM_DIR
COMM_DIRS = [ADDIN_COMM_DIR]
if REPO_COMM_DIR and REPO_COMM_DIR not in COMM_DIRS:
    COMM_DIRS.append(REPO_COMM_DIR)


def ensure_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    return path


def primary_comm_dir():
    return ensure_dir(PRIMARY_COMM_DIR)


def comm_file(filename, prefer_addin=False):
    base_dir = ADDIN_COMM_DIR if prefer_addin or REPO_COMM_DIR is None else PRIMARY_COMM_DIR
    return ensure_dir(base_dir) / filename


def all_comm_dirs():
    return [ensure_dir(path) for path in COMM_DIRS]


def ready_file_paths():
    ready_files = [ensure_dir(ADDIN_COMM_DIR) / "mcp_server_ready.txt"]
    if REPO_COMM_DIR:
        ready_files.append(ensure_dir(REPO_COMM_DIR) / "mcp_server_ready.txt")
        ready_files.append(REPO_ROOT / "mcp_server_ready.txt")
    ready_files.append(Path.home() / "Desktop" / "mcp_server_ready.txt")

    unique_paths = []
    for path in ready_files:
        if path not in unique_paths:
            unique_paths.append(path)
    return unique_paths


def write_text(path, content, mode="w"):
    ensure_dir(path.parent)
    with open(path, mode, encoding="utf-8") as handle:
        handle.write(content)


def append_text(path, content):
    write_text(path, content, mode="a")


def capture_fusion_main_thread():
    global FUSION_MAIN_THREAD_ID

    if FUSION_MAIN_THREAD_ID is None:
        FUSION_MAIN_THREAD_ID = threading.get_ident()


def is_fusion_main_thread():
    return FUSION_MAIN_THREAD_ID is not None and threading.get_ident() == FUSION_MAIN_THREAD_ID


def settle_fusion_processing(cycles=3, delay_seconds=0.05):
    for _ in range(max(0, int(cycles))):
        try:
            adsk.doEvents()
        except Exception:
            pass

        if delay_seconds > 0:
            time.sleep(delay_seconds)


class FusionMainThreadEventHandler(adsk.core.CustomEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        event_args = adsk.core.CustomEventArgs.cast(args)
        request_id = ""
        try:
            request_id = event_args.additionalInfo if event_args else ""
        except Exception:
            request_id = ""

        if is_blank(request_id):
            return

        with main_thread_requests_lock:
            request = main_thread_requests.get(request_id)

        if not request:
            return

        try:
            request["result"] = request["callback"]()
            settle_fusion_processing()
        except Exception as exc:
            request["error"] = exc
            request["traceback"] = traceback.format_exc()
        finally:
            request["event"].set()


def ensure_main_thread_event_registered():
    global main_thread_custom_event
    global main_thread_event_handler

    if main_thread_custom_event and main_thread_event_handler:
        return main_thread_custom_event

    capture_fusion_main_thread()

    try:
        main_thread_custom_event = app.registerCustomEvent(MAIN_THREAD_EVENT_ID)
    except Exception:
        try:
            app.unregisterCustomEvent(MAIN_THREAD_EVENT_ID)
        except Exception:
            pass
        main_thread_custom_event = app.registerCustomEvent(MAIN_THREAD_EVENT_ID)

    main_thread_event_handler = FusionMainThreadEventHandler()
    main_thread_custom_event.add(main_thread_event_handler)
    handlers.append(main_thread_event_handler)
    return main_thread_custom_event


def unregister_main_thread_event():
    global main_thread_custom_event
    global main_thread_event_handler

    if not main_thread_custom_event:
        return

    try:
        app.unregisterCustomEvent(MAIN_THREAD_EVENT_ID)
    except Exception:
        pass

    main_thread_custom_event = None
    main_thread_event_handler = None

    with main_thread_requests_lock:
        pending_requests = list(main_thread_requests.values())
        main_thread_requests.clear()

    for request in pending_requests:
        request["error"] = RuntimeError("Fusion main-thread event was unregistered before the request finished")
        request["traceback"] = ""
        request["event"].set()


def run_in_fusion_main_thread(callback, timeout_seconds=120.0):
    if not callable(callback):
        raise TypeError("callback must be callable")

    capture_fusion_main_thread()

    if is_fusion_main_thread():
        result = callback()
        settle_fusion_processing()
        return result

    ensure_main_thread_event_registered()

    global main_thread_request_counter
    with main_thread_requests_lock:
        main_thread_request_counter += 1
        request_id = f"{int(time.time() * 1000)}_{main_thread_request_counter}"
        request = {
            "callback": callback,
            "event": threading.Event(),
            "result": None,
            "error": None,
            "traceback": "",
        }
        main_thread_requests[request_id] = request

    app.fireCustomEvent(MAIN_THREAD_EVENT_ID, request_id)

    if not request["event"].wait(timeout_seconds):
        with main_thread_requests_lock:
            main_thread_requests.pop(request_id, None)
        raise TimeoutError(f"Timed out waiting for Fusion main-thread execution: {request_id}")

    with main_thread_requests_lock:
        main_thread_requests.pop(request_id, None)

    if request["error"]:
        exc = request["error"]
        if getattr(exc, "_fusion_main_thread_traceback", None) is None:
            setattr(exc, "_fusion_main_thread_traceback", request.get("traceback", ""))
        raise exc

    return request["result"]


def call_fusion_api(callback, timeout_seconds=120.0):
    try:
        return run_in_fusion_main_thread(callback, timeout_seconds)
    except Exception as exc:
        trace_text = getattr(exc, "_fusion_main_thread_traceback", "")
        payload = error_payload("Fusion API call failed", exc)
        if trace_text:
            payload["traceback"] = trace_text
        return payload


def get_document_type_name(doc):
    try:
        return str(doc.documentType)
    except Exception:
        try:
            if doc.products.itemByProductType("DesignProductType"):
                return "FusionDesignDocumentType"
        except Exception:
            pass
        return type(doc).__name__


def get_design(doc):
    if not doc:
        return None, "No active document"

    try:
        design_product = doc.products.itemByProductType("DesignProductType")
    except Exception as exc:
        return None, str(exc)

    if not design_product:
        return None, "Active document is not a Fusion design document"

    design = adsk.fusion.Design.cast(design_product)
    if not design:
        return None, "Failed to get design from document"

    return design, None


NUMERIC_LITERAL_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")


def is_blank(value):
    return value is None or (isinstance(value, str) and value.strip() == "")


def iter_collection(collection):
    if not collection:
        return

    try:
        for item in collection:
            yield item
        return
    except TypeError:
        pass

    try:
        count = collection.count
    except Exception:
        count = 0

    for index in range(count):
        try:
            item = collection.item(index)
        except Exception:
            item = None
        if item:
            yield item


def safe_count(collection):
    try:
        return collection.count
    except Exception:
        try:
            return len(collection)
        except Exception:
            return 0


def safe_getattr(value, name, default=None):
    try:
        return getattr(value, name)
    except Exception:
        return default


def safe_get_name(entity):
    try:
        return entity.name
    except Exception:
        return ""


def safe_object_type(entity):
    try:
        return entity.objectType
    except Exception:
        return type(entity).__name__


def safe_entity_token(entity):
    try:
        return entity.entityToken
    except Exception:
        return ""


def normalize_file_extension(extension):
    return str(extension or "").strip().lower().lstrip(".")


def get_data_file(entity):
    try:
        if safe_object_type(entity) == "adsk::core::Document":
            return entity.dataFile
        return entity
    except Exception:
        return None


def get_data_file_name(entity):
    data_file = get_data_file(entity)
    return safe_get_name(data_file)


def get_data_file_extension(entity):
    data_file = get_data_file(entity)
    return normalize_file_extension(safe_getattr(data_file, "fileExtension", ""))


def get_document_base_name(doc):
    return get_data_file_name(doc) or safe_get_name(doc)


def get_document_active_product(doc):
    if not doc:
        return None

    try:
        products = doc.products
    except Exception:
        products = None

    if not products:
        return None

    try:
        active_product = products.activeProduct
        if active_product:
            return active_product
    except Exception:
        pass

    try:
        if products.count:
            return products.item(0)
    except Exception:
        pass

    return None


def iter_open_documents():
    try:
        documents = app.documents
    except Exception:
        return

    if not documents:
        return

    try:
        count = documents.count
    except Exception:
        count = 0

    for index in range(count):
        try:
            document = documents.item(index)
        except Exception:
            document = None
        if document:
            yield document


def find_open_document(base_name="", extensions=None):
    normalized_name = normalize_key(base_name) if not is_blank(base_name) else ""
    normalized_extensions = None
    if extensions:
        normalized_extensions = {
            normalize_file_extension(extension)
            for extension in extensions
            if not is_blank(extension)
        }

    for document in iter_open_documents():
        if normalized_name and normalize_key(get_document_base_name(document)) != normalized_name:
            continue

        if normalized_extensions is not None:
            document_extension = get_data_file_extension(document)
            if document_extension not in normalized_extensions:
                continue

        return document

    return None


def get_active_data_folder():
    try:
        return app.data.activeFolder
    except Exception:
        return None


def get_matching_data_files(base_name="", extensions=None, folder=None):
    data_folder = folder or get_active_data_folder()
    if not data_folder:
        return []

    data_files = safe_getattr(data_folder, "dataFiles")
    if not data_files:
        return []

    normalized_name = normalize_key(base_name) if not is_blank(base_name) else ""
    normalized_extensions = None
    if extensions:
        normalized_extensions = {
            normalize_file_extension(extension)
            for extension in extensions
            if not is_blank(extension)
        }

    matches = []
    for data_file in iter_collection(data_files):
        if normalized_name and normalize_key(get_data_file_name(data_file)) != normalized_name:
            continue

        if normalized_extensions is not None:
            if get_data_file_extension(data_file) not in normalized_extensions:
                continue

        matches.append(data_file)

    return matches


def get_document_summary(doc):
    active_document = safe_getattr(app, "activeDocument")
    data_file = get_data_file(doc)
    active_product = get_document_active_product(doc)

    summary = {
        "name": safe_get_name(doc),
        "object_type": safe_object_type(doc),
        "is_active": bool(active_document and doc == active_document),
        "is_saved": safe_getattr(doc, "isSaved"),
        "base_name": get_document_base_name(doc),
        "file_extension": get_data_file_extension(doc),
        "data_file_id": safe_getattr(data_file, "id"),
        "product_type": safe_getattr(active_product, "productType"),
        "product_object_type": safe_object_type(active_product) if active_product else None,
    }

    if HAS_ELECTRON_API and active_product:
        summary["electronics"] = serialize_electronics_document_summary(active_product)

    return summary


def get_data_file_summary(data_file):
    return {
        "name": get_data_file_name(data_file),
        "extension": get_data_file_extension(data_file),
        "id": safe_getattr(data_file, "id"),
        "complete": safe_getattr(data_file, "isComplete"),
        "read_only": safe_getattr(data_file, "isReadOnly"),
        "in_use": safe_getattr(data_file, "isInUse"),
        "version": safe_getattr(data_file, "versionNumber"),
        "latest_version": safe_getattr(data_file, "latestVersionNumber"),
    }


def get_sketch_points_collection(sketch):
    try:
        return sketch.sketchPoints
    except Exception:
        getter = getattr(sketch, "_get_sketchPoints", None)
        if getter:
            return getter()
        raise


def get_sketch_curves_collection(sketch):
    try:
        return sketch.sketchCurves
    except Exception:
        getter = getattr(sketch, "_get_sketchCurves", None)
        if getter:
            return getter()
        raise


def get_sketch_dimensions_collection(sketch):
    try:
        return sketch.sketchDimensions
    except Exception:
        getter = getattr(sketch, "_get_sketchDimensions", None)
        if getter:
            return getter()
        raise


def get_geometric_constraints_collection(sketch):
    try:
        return sketch.geometricConstraints
    except Exception:
        getter = getattr(sketch, "_get_geometricConstraints", None)
        if getter:
            return getter()
        raise


def get_component_bodies_collection(component):
    try:
        return component.bodies
    except Exception:
        pass

    try:
        return component.bRepBodies
    except Exception:
        getter = getattr(component, "_get_bRepBodies", None)
        if getter:
            return getter()
        getter = getattr(component, "_get_bodies", None)
        if getter:
            return getter()
        raise


def get_sketch_lines_collection(sketch):
    curves = get_sketch_curves_collection(sketch)
    try:
        return curves.sketchLines
    except Exception:
        getter = getattr(curves, "_get_sketchLines", None)
        if getter:
            return getter()
        raise


def get_sketch_circles_collection(sketch):
    curves = get_sketch_curves_collection(sketch)
    try:
        return curves.sketchCircles
    except Exception:
        getter = getattr(curves, "_get_sketchCircles", None)
        if getter:
            return getter()
        raise


def get_sketch_arcs_collection(sketch):
    curves = get_sketch_curves_collection(sketch)
    try:
        return curves.sketchArcs
    except Exception:
        getter = getattr(curves, "_get_sketchArcs", None)
        if getter:
            return getter()
        raise


def get_sketch_fitted_splines_collection(sketch):
    curves = get_sketch_curves_collection(sketch)
    try:
        return curves.sketchFittedSplines
    except Exception:
        getter = getattr(curves, "_get_sketchFittedSplines", None)
        if getter:
            return getter()
        raise


def point_to_dict(point):
    if not point:
        return None
    return {
        "x": point.x,
        "y": point.y,
        "z": point.z,
    }


def point2d_to_dict(point):
    if not point:
        return None
    return {
        "x": point.x,
        "y": point.y,
    }


def safe_repr(value, max_length=240):
    try:
        text = repr(value)
    except Exception as exc:
        text = f"<unreprable {type(value).__name__}: {exc}>"

    if len(text) > max_length:
        return text[: max_length - 3] + "..."
    return text


def error_payload(prefix, exc):
    return {
        "error": f"{prefix}: {exc}",
        "traceback": traceback.format_exc(),
    }


def error_message(prefix, exc):
    return f"{prefix}: {exc}"


def normalize_key(value):
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def sanitize_filename(name):
    cleaned = re.sub(r"[<>:\"/\\\\|?*]+", "_", str(name or "").strip())
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned.strip("._") or "fusion_export"


def resolve_output_path(filename, default_stem, extension):
    base_dir = REPO_ROOT or ADDIN_DIR
    export_dir = ensure_dir(base_dir / "exports")

    if filename:
        path = Path(filename)
        if not path.is_absolute():
            path = base_dir / path
    else:
        path = export_dir / sanitize_filename(default_stem)

    if extension and path.suffix.lower() != extension.lower():
        path = path.with_suffix(extension)

    ensure_dir(path.parent)
    return path


def expression_from_value(value, default_unit=None):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value} {default_unit}".strip() if default_unit else str(value)

    text = str(value).strip()
    if default_unit and NUMERIC_LITERAL_RE.match(text):
        return f"{text} {default_unit}"
    return text


def get_active_design_context():
    doc = app.activeDocument
    design, error = get_design(doc)
    if error:
        raise RuntimeError(error)
    return doc, design


def get_units_manager(design):
    return design.fusionUnitsManager


def length_value(design, value, default_unit=None):
    units_manager = get_units_manager(design)
    unit = default_unit or units_manager.defaultLengthUnits or "cm"
    return units_manager.evaluateExpression(expression_from_value(value, unit), unit)


def angle_value(design, value, default_unit="deg"):
    units_manager = get_units_manager(design)
    unit = default_unit or "deg"
    return units_manager.evaluateExpression(expression_from_value(value, unit), unit)


def length_value_input(design, value, default_unit=None):
    units_manager = get_units_manager(design)
    unit = default_unit or units_manager.defaultLengthUnits or "cm"
    return adsk.core.ValueInput.createByString(expression_from_value(value, unit))


def angle_value_input(design, value, default_unit="deg"):
    unit = default_unit or "deg"
    return adsk.core.ValueInput.createByString(expression_from_value(value, unit))


def make_point3d(design, x, y, z=0.0, default_unit=None):
    return adsk.core.Point3D.create(
        length_value(design, x, default_unit),
        length_value(design, y, default_unit),
        length_value(design, z, default_unit),
    )


def point3d_from_data(design, point_data, default_unit=None):
    if isinstance(point_data, dict):
        return make_point3d(
            design,
            point_data.get("x", 0),
            point_data.get("y", 0),
            point_data.get("z", 0),
            default_unit,
        )

    if isinstance(point_data, (list, tuple)):
        values = list(point_data) + [0, 0, 0]
        return make_point3d(design, values[0], values[1], values[2], default_unit)

    raise ValueError(f"Unsupported point format: {point_data!r}")


def get_active_products_context():
    doc = app.activeDocument
    design, design_error = get_design(doc)

    drawing = None
    cam = None
    active_product = None

    if doc:
        active_product = get_document_active_product(doc)

        if not active_product:
            try:
                active_product = app.activeProduct
            except Exception:
                active_product = None

        try:
            drawing_product = doc.products.itemByProductType("DrawingProductType")
            if drawing_product:
                drawing = adsk.drawing.Drawing.cast(drawing_product)
        except Exception:
            drawing = None

        try:
            cam_product = doc.products.itemByProductType("CAMProductType")
            if cam_product:
                cam = adsk.cam.CAM.cast(cam_product)
        except Exception:
            cam = None

    electronics_context = get_active_electronics_context(doc, active_product)

    return {
        "doc": doc,
        "design": design,
        "design_error": design_error,
        "drawing": drawing,
        "cam": cam,
        "active_product": active_product,
        "document_products": electronics_context["document_products"],
        "ecad_document": electronics_context["ecad_document"],
        "ecad_design": electronics_context["ecad_design"],
        "schematic": electronics_context["schematic"],
        "board": electronics_context["board"],
        "library": electronics_context["library"],
    }


def iter_document_products(doc):
    if not doc:
        return []

    try:
        products = doc.products
    except Exception:
        return []

    if not products:
        return []

    result = []
    try:
        count = products.count
    except Exception:
        count = 0

    for index in range(count):
        try:
            product = products.item(index)
        except Exception:
            continue
        if product:
            result.append(product)
    return result


def safe_electron_cast(caster, value):
    if not HAS_ELECTRON_API or not value:
        return None
    try:
        return caster(value)
    except Exception:
        return None


def first_electronics_product(products, caster):
    for product in products:
        cast_value = safe_electron_cast(caster, product)
        if cast_value:
            return cast_value
    return None


def get_active_electronics_context(doc=None, active_product=None):
    products = iter_document_products(doc)
    active = active_product
    if not active:
        try:
            active = app.activeProduct
        except Exception:
            active = None

    ecad_document = safe_electron_cast(adsk.electron.EcadDocument.cast, active) if HAS_ELECTRON_API else None
    ecad_design = safe_electron_cast(adsk.electron.EcadDesign.cast, active) if HAS_ELECTRON_API else None
    schematic = safe_electron_cast(adsk.electron.Schematic.cast, active) if HAS_ELECTRON_API else None
    board = safe_electron_cast(adsk.electron.Board.cast, active) if HAS_ELECTRON_API else None
    library = safe_electron_cast(adsk.electron.Library.cast, active) if HAS_ELECTRON_API else None

    if HAS_ELECTRON_API:
        ecad_document = ecad_document or first_electronics_product(products, adsk.electron.EcadDocument.cast)
        ecad_design = ecad_design or first_electronics_product(products, adsk.electron.EcadDesign.cast)
        schematic = schematic or first_electronics_product(products, adsk.electron.Schematic.cast)
        board = board or first_electronics_product(products, adsk.electron.Board.cast)
        library = library or first_electronics_product(products, adsk.electron.Library.cast)

    base_name = get_document_base_name(doc)
    if HAS_ELECTRON_API and not is_blank(base_name):
        if not schematic:
            schematic_doc = find_open_document(base_name, ("fsch", "sch"))
            if schematic_doc:
                schematic = first_electronics_product(iter_document_products(schematic_doc), adsk.electron.Schematic.cast)

        if not board:
            board_doc = find_open_document(base_name, ("fbrd", "brd"))
            if board_doc:
                board = first_electronics_product(iter_document_products(board_doc), adsk.electron.Board.cast)

        if not library:
            library_doc = find_open_document(base_name, ("flbr", "lbr"))
            if library_doc:
                library = first_electronics_product(iter_document_products(library_doc), adsk.electron.Library.cast)

        if not ecad_design:
            project_doc = find_open_document(base_name, ("fprj", "prj"))
            if project_doc:
                ecad_design = first_electronics_product(iter_document_products(project_doc), adsk.electron.EcadDesign.cast)
                ecad_document = ecad_document or first_electronics_product(iter_document_products(project_doc), adsk.electron.EcadDocument.cast)

    if schematic and not board:
        try:
            board = schematic.linkedBoard
        except Exception:
            board = None

    if board and not schematic:
        try:
            schematic = board.linkedSchematic
        except Exception:
            schematic = None

    if ecad_design:
        try:
            schematic = schematic or ecad_design.schematic
        except Exception:
            pass
        try:
            board = board or ecad_design.board
        except Exception:
            pass

    if not ecad_design:
        try:
            if schematic and schematic.parentDesign:
                ecad_design = schematic.parentDesign
        except Exception:
            pass
        try:
            if board and board.parentDesign:
                ecad_design = board.parentDesign
        except Exception:
            pass
        try:
            if library and library.parentDesign:
                ecad_design = library.parentDesign
        except Exception:
            pass

    if not ecad_document:
        ecad_document = ecad_design or schematic or board or library

    return {
        "document_products": products,
        "ecad_document": ecad_document,
        "ecad_design": ecad_design,
        "schematic": schematic,
        "board": board,
        "library": library,
    }


def get_selected_entities():
    entities = []
    try:
        selections = ui.activeSelections
        for index in range(selections.count):
            selection = selections.item(index)
            if selection and selection.entity:
                entities.append(selection.entity)
    except Exception:
        pass
    return entities


def get_all_components(design):
    components = []
    seen = set()

    def add_component(component):
        if not component:
            return
        token = safe_entity_token(component) or f"component:{safe_get_name(component)}:{len(seen)}"
        if token in seen:
            return
        seen.add(token)
        components.append(component)

    root_component = design.rootComponent
    add_component(root_component)

    try:
        occurrences = root_component.allOccurrences
    except Exception:
        occurrences = []

    for occurrence in iter_collection(occurrences):
        try:
            add_component(occurrence.component)
        except Exception:
            continue

    return components


def find_entity_by_token(design, token):
    if is_blank(token):
        return None

    entities = design.findEntityByToken(token)
    for entity in entities or []:
        if entity:
            return entity
    return None


def get_entity_parent_component(entity):
    for attr_name in ("parentComponent",):
        try:
            component = getattr(entity, attr_name)
            if component:
                return component
        except Exception:
            pass

    try:
        parent_sketch = entity.parentSketch
        if parent_sketch and parent_sketch.parentComponent:
            return parent_sketch.parentComponent
    except Exception:
        pass

    try:
        parent = entity.parent
        if parent:
            parent_component = getattr(parent, "parentComponent", None)
            if parent_component:
                return parent_component
    except Exception:
        pass

    return None


def find_occurrence_for_component(design, component):
    root_component = design.rootComponent
    for occurrence in iter_collection(root_component.allOccurrences):
        try:
            if occurrence.component == component:
                return occurrence
        except Exception:
            continue
    return None


def find_component(design, component_name=""):
    root_component = design.rootComponent
    if is_blank(component_name):
        return root_component

    entity = find_entity_by_token(design, component_name)
    component = adsk.fusion.Component.cast(entity) if entity else None
    if component:
        return component

    normalized = normalize_key(component_name)
    if normalized in ("root", "root_component"):
        return root_component

    components = get_all_components(design)
    for candidate in components:
        if safe_get_name(candidate) == component_name:
            return candidate
    for candidate in components:
        if normalize_key(safe_get_name(candidate)) == normalized:
            return candidate

    raise ValueError(f"Could not find component: {component_name}")


def iter_all_sketches(design):
    for component in get_all_components(design):
        for sketch in iter_collection(component.sketches):
            yield component, sketch


def iter_all_bodies(design):
    for component in get_all_components(design):
        for body in iter_collection(get_component_bodies_collection(component)):
            yield component, body


def find_sketch(design, sketch_name, component_name=""):
    if is_blank(sketch_name):
        raise ValueError("sketch_name is required")

    entity = find_entity_by_token(design, sketch_name)
    sketch = adsk.fusion.Sketch.cast(entity) if entity else None
    if sketch:
        return sketch

    normalized = normalize_key(sketch_name)
    if not is_blank(component_name):
        component = find_component(design, component_name)
        sketch = component.sketches.itemByName(sketch_name)
        if sketch:
            return sketch
        for candidate in iter_collection(component.sketches):
            if normalize_key(safe_get_name(candidate)) == normalized:
                return candidate
    else:
        for _, candidate in iter_all_sketches(design):
            if safe_get_name(candidate) == sketch_name:
                return candidate
        for _, candidate in iter_all_sketches(design):
            if normalize_key(safe_get_name(candidate)) == normalized:
                return candidate

    raise ValueError(f"Could not find sketch: {sketch_name}")


def find_planar_entity(design, plane_name, component_name=""):
    if is_blank(plane_name):
        return design.rootComponent.xYConstructionPlane

    entity = find_entity_by_token(design, plane_name)
    if entity:
        if adsk.fusion.ConstructionPlane.cast(entity) or adsk.fusion.BRepFace.cast(entity):
            return entity

    components = [find_component(design, component_name)] if not is_blank(component_name) else get_all_components(design)
    normalized = normalize_key(plane_name)

    for component in components:
        standard_planes = {
            "xy": component.xYConstructionPlane,
            "xy_plane": component.xYConstructionPlane,
            "yz": component.yZConstructionPlane,
            "yz_plane": component.yZConstructionPlane,
            "xz": component.xZConstructionPlane,
            "xz_plane": component.xZConstructionPlane,
        }
        if normalized in standard_planes:
            return standard_planes[normalized]

        for plane in (
            component.xYConstructionPlane,
            component.yZConstructionPlane,
            component.xZConstructionPlane,
        ):
            if normalize_key(safe_get_name(plane)) == normalized:
                return plane

        construction_plane = component.constructionPlanes.itemByName(plane_name)
        if construction_plane:
            return construction_plane

        for candidate in iter_collection(component.constructionPlanes):
            if normalize_key(safe_get_name(candidate)) == normalized:
                return candidate

    raise ValueError(f"Could not find plane: {plane_name}")


def find_axis_entity(design, axis_name, component_name=""):
    if is_blank(axis_name):
        raise ValueError("axis_name or axis_token is required")

    entity = find_entity_by_token(design, axis_name)
    if entity:
        return entity

    components = [find_component(design, component_name)] if not is_blank(component_name) else get_all_components(design)
    normalized = normalize_key(axis_name)

    for component in components:
        standard_axes = {
            "x": getattr(component, "xConstructionAxis", None),
            "x_axis": getattr(component, "xConstructionAxis", None),
            "y": getattr(component, "yConstructionAxis", None),
            "y_axis": getattr(component, "yConstructionAxis", None),
            "z": getattr(component, "zConstructionAxis", None),
            "z_axis": getattr(component, "zConstructionAxis", None),
        }
        if normalized in standard_axes and standard_axes[normalized]:
            return standard_axes[normalized]

        for key in ("xConstructionAxis", "yConstructionAxis", "zConstructionAxis"):
            axis = getattr(component, key, None)
            if axis and normalize_key(safe_get_name(axis)) == normalized:
                return axis

        try:
            construction_axis = component.constructionAxes.itemByName(axis_name)
        except Exception:
            construction_axis = None
        if construction_axis:
            return construction_axis

        try:
            axes = component.constructionAxes
        except Exception:
            axes = []
        for candidate in iter_collection(axes):
            if normalize_key(safe_get_name(candidate)) == normalized:
                return candidate

    raise ValueError(f"Could not find axis: {axis_name}")


def find_body(design, body_name, component_name=""):
    if is_blank(body_name):
        raise ValueError("body_name is required")

    entity = find_entity_by_token(design, body_name)
    body = adsk.fusion.BRepBody.cast(entity) if entity else None
    if body:
        return body

    normalized = normalize_key(body_name)
    if not is_blank(component_name):
        component = find_component(design, component_name)
        bodies = get_component_bodies_collection(component)
        body = bodies.itemByName(body_name)
        if body:
            return body
        for candidate in iter_collection(bodies):
            if normalize_key(safe_get_name(candidate)) == normalized:
                return candidate
    else:
        for _, candidate in iter_all_bodies(design):
            if safe_get_name(candidate) == body_name:
                return candidate
        for _, candidate in iter_all_bodies(design):
            if normalize_key(safe_get_name(candidate)) == normalized:
                return candidate

    raise ValueError(f"Could not find body: {body_name}")


def find_profile(sketch, profile_index=0):
    try:
        index = int(profile_index)
    except Exception as exc:
        raise ValueError(f"Invalid profile_index: {profile_index}") from exc

    profiles = sketch.profiles
    if index < 0 or index >= safe_count(profiles):
        raise ValueError(f"Profile index {index} is out of range for sketch {sketch.name}")
    return profiles.item(index)


def resolve_profile_entity(design, sketch_name="", component_name="", profile_index=0, profile_token=""):
    entity = find_entity_by_token(design, profile_token)
    profile = adsk.fusion.Profile.cast(entity) if entity else None
    if profile:
        return profile

    sketch = find_sketch(design, sketch_name, component_name)
    return find_profile(sketch, profile_index)


def feature_operation_from_name(name):
    operations = {
        "join": adsk.fusion.FeatureOperations.JoinFeatureOperation,
        "cut": adsk.fusion.FeatureOperations.CutFeatureOperation,
        "intersect": adsk.fusion.FeatureOperations.IntersectFeatureOperation,
        "new_body": adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        "newbody": adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        "new_component": adsk.fusion.FeatureOperations.NewComponentFeatureOperation,
        "newcomponent": adsk.fusion.FeatureOperations.NewComponentFeatureOperation,
    }
    key = normalize_key(name or "new_body")
    if key not in operations:
        raise ValueError(f"Unsupported operation: {name}")
    return operations[key]


def extent_direction_from_name(name):
    directions = {
        "positive": adsk.fusion.ExtentDirections.PositiveExtentDirection,
        "negative": adsk.fusion.ExtentDirections.NegativeExtentDirection,
    }
    key = normalize_key(name or "positive")
    if key not in directions:
        raise ValueError(f"Unsupported direction: {name}")
    return directions[key]


def dimension_orientation_from_name(name):
    orientations = {
        "aligned": adsk.fusion.DimensionOrientations.AlignedDimensionOrientation,
        "horizontal": adsk.fusion.DimensionOrientations.HorizontalDimensionOrientation,
        "vertical": adsk.fusion.DimensionOrientations.VerticalDimensionOrientation,
    }
    key = normalize_key(name or "aligned")
    if key not in orientations:
        raise ValueError(f"Unsupported dimension orientation: {name}")
    return orientations[key]


def require_sketch_point(entity, label="entity"):
    point = adsk.fusion.SketchPoint.cast(entity)
    if not point:
        raise ValueError(f"{label} must be a SketchPoint token")
    return point


def require_sketch_line(entity, label="entity"):
    line = adsk.fusion.SketchLine.cast(entity)
    if not line:
        raise ValueError(f"{label} must be a SketchLine token")
    return line


def require_sketch_curve(entity, label="entity"):
    curve = adsk.fusion.SketchCurve.cast(entity)
    if not curve:
        raise ValueError(f"{label} must be a SketchCurve token")
    return curve


def serialize_component(component):
    bodies = get_component_bodies_collection(component)
    return {
        "name": safe_get_name(component),
        "token": safe_entity_token(component),
        "bodies_count": safe_count(bodies),
        "sketches_count": safe_count(component.sketches),
        "occurrences_count": safe_count(component.occurrences),
    }


def serialize_sketch(sketch):
    return {
        "name": safe_get_name(sketch),
        "token": safe_entity_token(sketch),
        "parent_component": safe_get_name(sketch.parentComponent),
        "points_count": safe_count(get_sketch_points_collection(sketch)),
        "curves_count": safe_count(get_sketch_curves_collection(sketch)),
        "profiles_count": safe_count(sketch.profiles),
    }


def serialize_body(body):
    return {
        "name": safe_get_name(body),
        "token": safe_entity_token(body),
        "parent_component": safe_get_name(body.parentComponent),
        "is_solid": getattr(body, "isSolid", None),
    }


def serialize_sketch_entity(entity):
    data = {
        "token": safe_entity_token(entity),
        "name": safe_get_name(entity),
        "object_type": safe_object_type(entity),
        "is_construction": getattr(entity, "isConstruction", False),
    }

    point = adsk.fusion.SketchPoint.cast(entity)
    if point:
        data["entity_type"] = "SketchPoint"
        data["geometry"] = point_to_dict(point.geometry)
        return data

    line = adsk.fusion.SketchLine.cast(entity)
    if line:
        data["entity_type"] = "SketchLine"
        data["start"] = point_to_dict(line.startSketchPoint.geometry)
        data["end"] = point_to_dict(line.endSketchPoint.geometry)
        return data

    circle = adsk.fusion.SketchCircle.cast(entity)
    if circle:
        data["entity_type"] = "SketchCircle"
        data["center"] = point_to_dict(circle.centerSketchPoint.geometry)
        data["radius"] = circle.radius
        return data

    arc = adsk.fusion.SketchArc.cast(entity)
    if arc:
        data["entity_type"] = "SketchArc"
        data["center"] = point_to_dict(arc.centerSketchPoint.geometry)
        data["start"] = point_to_dict(arc.startSketchPoint.geometry)
        data["end"] = point_to_dict(arc.endSketchPoint.geometry)
        data["radius"] = arc.radius
        return data

    spline = adsk.fusion.SketchFittedSpline.cast(entity)
    if spline:
        data["entity_type"] = "SketchFittedSpline"
        data["start"] = point_to_dict(spline.startSketchPoint.geometry)
        data["end"] = point_to_dict(spline.endSketchPoint.geometry)
        data["fit_point_count"] = safe_count(spline.fitPoints)
        return data

    return data


def serialize_profile(profile, index):
    loop_count = safe_count(profile.profileLoops)
    loop_data = []
    for loop_index, loop in enumerate(iter_collection(profile.profileLoops)):
        curve_tokens = []
        for profile_curve in iter_collection(loop.profileCurves):
            try:
                curve_tokens.append(safe_entity_token(profile_curve.sketchEntity))
            except Exception:
                continue
        loop_data.append(
            {
                "index": loop_index,
                "is_outer": getattr(loop, "isOuter", None),
                "curve_tokens": curve_tokens,
            }
        )

    profile_data = {
        "index": index,
        "token": safe_entity_token(profile),
        "loop_count": loop_count,
        "loops": loop_data,
    }

    try:
        profile_data["area"] = profile.areaProperties().area
    except Exception:
        pass

    return profile_data


def serialize_dimension(dimension):
    parameter = dimension.parameter
    data = {
        "token": safe_entity_token(dimension),
        "object_type": safe_object_type(dimension),
    }
    if parameter:
        data["parameter_name"] = parameter.name
        data["expression"] = parameter.expression
        data["value"] = parameter.value
    return data


def serialize_workspace_list(workspaces, max_items=20):
    items = []
    if not workspaces:
        return items

    for index, workspace in enumerate(iter_collection(workspaces)):
        if index >= max_items:
            items.append({"__truncated__": max(0, safe_count(workspaces) - max_items)})
            break
        items.append(
            {
                "id": getattr(workspace, "id", None),
                "name": safe_get_name(workspace),
                "object_type": safe_object_type(workspace),
            }
        )
    return items


def preview_serialized_items(collection, serializer, limit=10):
    items = []
    if not collection:
        return items

    for index, item in enumerate(iter_collection(collection)):
        if index >= limit:
            break
        items.append(serializer(item))
    return items


def serialize_electronics_error(error):
    if not error:
        return None
    return {
        "id": safe_getattr(error, "id"),
        "code": safe_getattr(error, "code"),
        "description": safe_getattr(error, "description"),
        "module_name": safe_getattr(error, "moduleName"),
        "sheet": safe_getattr(error, "sheet"),
        "layer": safe_getattr(error, "layer"),
        "error_type": safe_repr(safe_getattr(error, "errorType")),
        "state": safe_repr(safe_getattr(error, "state")),
        "signature": safe_getattr(error, "signature"),
        "x": safe_getattr(error, "x"),
        "y": safe_getattr(error, "y"),
    }


def serialize_electronics_sheet(sheet):
    if not sheet:
        return None
    return {
        "name": safe_get_name(sheet),
        "object_type": safe_object_type(sheet),
        "parts_count": safe_count(safe_getattr(sheet, "parts")),
        "nets_count": safe_count(safe_getattr(sheet, "nets")),
        "wires_count": safe_count(safe_getattr(sheet, "wires")),
        "frames_count": safe_count(safe_getattr(sheet, "frames")),
    }


def serialize_electronics_part(part):
    if not part:
        return None

    device = safe_getattr(part, "device")
    device_set = safe_getattr(part, "deviceset")
    package3d = safe_getattr(part, "package3d")
    return {
        "name": safe_get_name(part),
        "object_type": safe_object_type(part),
        "value": safe_getattr(part, "value"),
        "device": safe_get_name(device),
        "device_set": safe_get_name(device_set),
        "package3d": safe_get_name(package3d),
        "instance_count": safe_count(safe_getattr(part, "instances")),
    }


def serialize_electronics_net(net):
    if not net:
        return None

    net_class = safe_getattr(net, "netClass")
    return {
        "name": safe_get_name(net),
        "object_type": safe_object_type(net),
        "class": safe_get_name(net_class),
        "pin_ref_count": safe_count(safe_getattr(net, "pinRefs")),
        "port_ref_count": safe_count(safe_getattr(net, "portRefs")),
        "segment_count": safe_count(safe_getattr(net, "segments")),
        "row_range": safe_getattr(net, "rowRange"),
        "column_range": safe_getattr(net, "columnRange"),
    }


def serialize_electronics_element(element):
    if not element:
        return None

    package = safe_getattr(element, "package")
    return {
        "name": safe_get_name(element),
        "object_type": safe_object_type(element),
        "value": safe_getattr(element, "value"),
        "x": safe_getattr(element, "x"),
        "y": safe_getattr(element, "y"),
        "angle": safe_getattr(element, "angle"),
        "mirror": safe_getattr(element, "mirror"),
        "locked": safe_getattr(element, "locked"),
        "populate": safe_getattr(element, "populate"),
        "package": safe_get_name(package),
    }


def serialize_electronics_signal(signal):
    if not signal:
        return None

    net_class = safe_getattr(signal, "netClass")
    return {
        "name": safe_get_name(signal),
        "object_type": safe_object_type(signal),
        "class": safe_get_name(net_class),
        "wire_count": safe_count(safe_getattr(signal, "wires")),
        "via_count": safe_count(safe_getattr(signal, "vias")),
        "contact_ref_count": safe_count(safe_getattr(signal, "contactRefs")),
        "air_wires_hidden": safe_getattr(signal, "airWiresHidden"),
    }


def serialize_electronics_device(device):
    if not device:
        return None

    package = safe_getattr(device, "package")
    return {
        "name": safe_get_name(device),
        "object_type": safe_object_type(device),
        "library": safe_getattr(device, "library"),
        "prefix": safe_getattr(device, "prefix"),
        "value": safe_getattr(device, "value"),
        "package": safe_get_name(package),
        "gate_count": safe_count(safe_getattr(device, "gates")),
        "package3d_count": safe_count(safe_getattr(device, "packages3d")),
    }


def serialize_electronics_device_set(device_set):
    if not device_set:
        return None

    return {
        "name": safe_get_name(device_set),
        "object_type": safe_object_type(device_set),
        "library": safe_getattr(device_set, "library"),
        "prefix": safe_getattr(device_set, "prefix"),
        "user_value": safe_getattr(device_set, "userValue"),
        "locally_modified": safe_getattr(device_set, "locallyModified"),
        "library_locally_modified": safe_getattr(device_set, "libraryLocallyModified"),
        "gate_count": safe_count(safe_getattr(device_set, "gates")),
        "device_count": safe_count(safe_getattr(device_set, "devices")),
    }


def serialize_electronics_library(library):
    if not library:
        return None

    return {
        "name": safe_get_name(library),
        "object_type": safe_object_type(library),
        "product_type": safe_getattr(library, "productType"),
        "id": safe_getattr(library, "id"),
        "editable": safe_getattr(library, "editable"),
        "description": safe_getattr(library, "description"),
        "symbol_count": safe_count(safe_getattr(library, "symbols")),
        "package_count": safe_count(safe_getattr(library, "packages")),
        "package3d_count": safe_count(safe_getattr(library, "packages3d")),
        "device_set_count": safe_count(safe_getattr(library, "deviceSets")),
        "device_count": safe_count(safe_getattr(library, "devices")),
        "workspaces": serialize_workspace_list(safe_getattr(library, "workspaces"), 10),
        "symbols_preview": preview_serialized_items(safe_getattr(library, "symbols"), lambda item: serialize_adsk_object(item, 1, 2, 6), 6),
        "packages_preview": preview_serialized_items(safe_getattr(library, "packages"), lambda item: serialize_adsk_object(item, 1, 2, 6), 6),
        "device_sets_preview": preview_serialized_items(safe_getattr(library, "deviceSets"), serialize_electronics_device_set, 6),
        "devices_preview": preview_serialized_items(safe_getattr(library, "devices"), serialize_electronics_device, 6),
    }


def serialize_electronics_library_summary(library):
    if not library:
        return None

    return {
        "name": safe_get_name(library),
        "object_type": safe_object_type(library),
        "product_type": safe_getattr(library, "productType"),
        "id": safe_getattr(library, "id"),
        "editable": safe_getattr(library, "editable"),
        "description": safe_getattr(library, "description"),
        "symbol_count": safe_count(safe_getattr(library, "symbols")),
        "package_count": safe_count(safe_getattr(library, "packages")),
        "package3d_count": safe_count(safe_getattr(library, "packages3d")),
        "device_set_count": safe_count(safe_getattr(library, "deviceSets")),
        "device_count": safe_count(safe_getattr(library, "devices")),
    }


def serialize_electronics_board(board):
    if not board:
        return None

    linked_schematic = safe_getattr(board, "linkedSchematic")
    return {
        "name": safe_get_name(board),
        "object_type": safe_object_type(board),
        "product_type": safe_getattr(board, "productType"),
        "linked_schematic": safe_get_name(linked_schematic),
        "checked": safe_getattr(board, "checked"),
        "element_count": safe_count(safe_getattr(board, "elements")),
        "signal_count": safe_count(safe_getattr(board, "signals")),
        "wire_count": safe_count(safe_getattr(board, "wires")),
        "hole_count": safe_count(safe_getattr(board, "holes")),
        "class_count": safe_count(safe_getattr(board, "classes")),
        "error_count": safe_count(safe_getattr(board, "errors")),
        "library_count": safe_count(safe_getattr(board, "libraries")),
        "workspaces": serialize_workspace_list(safe_getattr(board, "workspaces"), 10),
        "elements_preview": preview_serialized_items(safe_getattr(board, "elements"), serialize_electronics_element, 12),
        "signals_preview": preview_serialized_items(safe_getattr(board, "signals"), serialize_electronics_signal, 12),
        "errors_preview": preview_serialized_items(safe_getattr(board, "errors"), serialize_electronics_error, 12),
        "libraries_preview": preview_serialized_items(safe_getattr(board, "libraries"), serialize_electronics_library, 8),
    }


def serialize_electronics_board_summary(board):
    if not board:
        return None

    linked_schematic = safe_getattr(board, "linkedSchematic")
    return {
        "name": safe_get_name(board),
        "object_type": safe_object_type(board),
        "product_type": safe_getattr(board, "productType"),
        "linked_schematic": safe_get_name(linked_schematic),
        "checked": safe_getattr(board, "checked"),
        "element_count": safe_count(safe_getattr(board, "elements")),
        "signal_count": safe_count(safe_getattr(board, "signals")),
        "wire_count": safe_count(safe_getattr(board, "wires")),
        "hole_count": safe_count(safe_getattr(board, "holes")),
        "class_count": safe_count(safe_getattr(board, "classes")),
        "error_count": safe_count(safe_getattr(board, "errors")),
        "library_count": safe_count(safe_getattr(board, "libraries")),
    }


def serialize_electronics_schematic(schematic):
    if not schematic:
        return None

    linked_board = safe_getattr(schematic, "linkedBoard")
    return {
        "name": safe_get_name(schematic),
        "object_type": safe_object_type(schematic),
        "product_type": safe_getattr(schematic, "productType"),
        "linked_board": safe_get_name(linked_board),
        "checked": safe_getattr(schematic, "checked"),
        "sheet_count": safe_count(safe_getattr(schematic, "sheets")),
        "part_count": safe_count(safe_getattr(schematic, "parts")),
        "net_count": safe_count(safe_getattr(schematic, "nets")),
        "module_count": safe_count(safe_getattr(schematic, "modules")),
        "class_count": safe_count(safe_getattr(schematic, "classes")),
        "error_count": safe_count(safe_getattr(schematic, "errors")),
        "library_count": safe_count(safe_getattr(schematic, "libraries")),
        "workspaces": serialize_workspace_list(safe_getattr(schematic, "workspaces"), 10),
        "sheets_preview": preview_serialized_items(safe_getattr(schematic, "sheets"), serialize_electronics_sheet, 12),
        "parts_preview": preview_serialized_items(safe_getattr(schematic, "parts"), serialize_electronics_part, 12),
        "nets_preview": preview_serialized_items(safe_getattr(schematic, "nets"), serialize_electronics_net, 12),
        "errors_preview": preview_serialized_items(safe_getattr(schematic, "errors"), serialize_electronics_error, 12),
        "libraries_preview": preview_serialized_items(safe_getattr(schematic, "libraries"), serialize_electronics_library, 8),
    }


def serialize_electronics_schematic_summary(schematic):
    if not schematic:
        return None

    linked_board = safe_getattr(schematic, "linkedBoard")
    return {
        "name": safe_get_name(schematic),
        "object_type": safe_object_type(schematic),
        "product_type": safe_getattr(schematic, "productType"),
        "linked_board": safe_get_name(linked_board),
        "checked": safe_getattr(schematic, "checked"),
        "sheet_count": safe_count(safe_getattr(schematic, "sheets")),
        "part_count": safe_count(safe_getattr(schematic, "parts")),
        "net_count": safe_count(safe_getattr(schematic, "nets")),
        "module_count": safe_count(safe_getattr(schematic, "modules")),
        "class_count": safe_count(safe_getattr(schematic, "classes")),
        "error_count": safe_count(safe_getattr(schematic, "errors")),
        "library_count": safe_count(safe_getattr(schematic, "libraries")),
        "sheets_preview": preview_serialized_items(safe_getattr(schematic, "sheets"), serialize_electronics_sheet, 6),
    }


def serialize_electronics_design(ecad_design):
    if not ecad_design:
        return None

    schematic = safe_getattr(ecad_design, "schematic")
    board = safe_getattr(ecad_design, "board")
    return {
        "name": safe_get_name(ecad_design),
        "object_type": safe_object_type(ecad_design),
        "product_type": safe_getattr(ecad_design, "productType"),
        "schematic": safe_get_name(schematic),
        "board": safe_get_name(board),
        "workspaces": serialize_workspace_list(safe_getattr(ecad_design, "workspaces"), 10),
    }


def serialize_electronics_design_summary(ecad_design):
    if not ecad_design:
        return None

    schematic = safe_getattr(ecad_design, "schematic")
    board = safe_getattr(ecad_design, "board")
    return {
        "name": safe_get_name(ecad_design),
        "object_type": safe_object_type(ecad_design),
        "product_type": safe_getattr(ecad_design, "productType"),
        "schematic": safe_get_name(schematic),
        "board": safe_get_name(board),
        "has_schematic": bool(schematic),
        "has_board": bool(board),
    }


def serialize_electronics_document_summary(document):
    if not document or not HAS_ELECTRON_API:
        return None

    ecad_design = safe_electron_cast(adsk.electron.EcadDesign.cast, document)
    if ecad_design:
        data = serialize_electronics_design_summary(ecad_design)
        data["entity_type"] = "EcadDesign"
        return data

    schematic = safe_electron_cast(adsk.electron.Schematic.cast, document)
    if schematic:
        data = serialize_electronics_schematic_summary(schematic)
        data["entity_type"] = "Schematic"
        return data

    board = safe_electron_cast(adsk.electron.Board.cast, document)
    if board:
        data = serialize_electronics_board_summary(board)
        data["entity_type"] = "Board"
        return data

    library = safe_electron_cast(adsk.electron.Library.cast, document)
    if library:
        data = serialize_electronics_library_summary(library)
        data["entity_type"] = "Library"
        return data

    return {
        "name": safe_get_name(document),
        "object_type": safe_object_type(document),
        "product_type": safe_getattr(document, "productType"),
    }


def is_adsk_like(value):
    module_name = getattr(type(value), "__module__", "")
    return module_name.startswith("adsk") or hasattr(value, "objectType")


def serialize_collection_preview(collection, depth=0, max_depth=3, max_items=10):
    count = safe_count(collection)
    preview = []
    if depth < max_depth:
        for index, item in enumerate(iter_collection(collection)):
            if index >= max_items:
                break
            preview.append(make_jsonable(item, depth + 1, max_depth, max_items))

    return {
        "python_type": type(collection).__name__,
        "count": count,
        "items": preview,
        "truncated": count > len(preview),
    }


def serialize_adsk_object(value, depth=0, max_depth=3, max_items=10):
    try:
        point3d = adsk.core.Point3D.cast(value)
    except Exception:
        point3d = None
    if point3d:
        return point_to_dict(point3d)

    try:
        point2d = adsk.core.Point2D.cast(value)
    except Exception:
        point2d = None
    if point2d:
        return point2d_to_dict(point2d)

    try:
        vector3d = adsk.core.Vector3D.cast(value)
    except Exception:
        vector3d = None
    if vector3d:
        return {"x": vector3d.x, "y": vector3d.y, "z": vector3d.z}

    component = adsk.fusion.Component.cast(value)
    if component:
        data = serialize_component(component)
        data["entity_type"] = "Component"
        return data

    sketch = adsk.fusion.Sketch.cast(value)
    if sketch:
        data = serialize_sketch(sketch)
        data["entity_type"] = "Sketch"
        return data

    body = adsk.fusion.BRepBody.cast(value)
    if body:
        data = serialize_body(body)
        data["entity_type"] = "BRepBody"
        return data

    profile = adsk.fusion.Profile.cast(value)
    if profile:
        data = serialize_profile(profile, -1)
        data["entity_type"] = "Profile"
        return data

    sketch_entity = adsk.fusion.SketchEntity.cast(value)
    if sketch_entity:
        return serialize_sketch_entity(sketch_entity)

    if HAS_ELECTRON_API:
        ecad_design = adsk.electron.EcadDesign.cast(value)
        if ecad_design:
            data = serialize_electronics_design(ecad_design)
            data["entity_type"] = "EcadDesign"
            return data

        schematic = adsk.electron.Schematic.cast(value)
        if schematic:
            data = serialize_electronics_schematic(schematic)
            data["entity_type"] = "Schematic"
            return data

        board = adsk.electron.Board.cast(value)
        if board:
            data = serialize_electronics_board(board)
            data["entity_type"] = "Board"
            return data

        library = adsk.electron.Library.cast(value)
        if library:
            data = serialize_electronics_library(library)
            data["entity_type"] = "Library"
            return data

        sheet = adsk.electron.Sheet.cast(value)
        if sheet:
            data = serialize_electronics_sheet(sheet)
            data["entity_type"] = "Sheet"
            return data

        part = adsk.electron.Part.cast(value)
        if part:
            data = serialize_electronics_part(part)
            data["entity_type"] = "Part"
            return data

        net = adsk.electron.Net.cast(value)
        if net:
            data = serialize_electronics_net(net)
            data["entity_type"] = "Net"
            return data

        element = adsk.electron.Element.cast(value)
        if element:
            data = serialize_electronics_element(element)
            data["entity_type"] = "Element"
            return data

        signal = adsk.electron.Signal.cast(value)
        if signal:
            data = serialize_electronics_signal(signal)
            data["entity_type"] = "Signal"
            return data

        device = adsk.electron.Device.cast(value)
        if device:
            data = serialize_electronics_device(device)
            data["entity_type"] = "Device"
            return data

        device_set = adsk.electron.DeviceSet.cast(value)
        if device_set:
            data = serialize_electronics_device_set(device_set)
            data["entity_type"] = "DeviceSet"
            return data

        electron_error = adsk.electron.Error.cast(value)
        if electron_error:
            data = serialize_electronics_error(electron_error)
            data["entity_type"] = "ElectronicsError"
            return data

    data = {
        "python_type": type(value).__name__,
        "object_type": safe_object_type(value),
        "repr": safe_repr(value),
    }

    name = safe_get_name(value)
    if name:
        data["name"] = name

    token = safe_entity_token(value)
    if token:
        data["token"] = token

    count = safe_count(value)
    if count:
        data["count"] = count
        if depth < max_depth:
            preview = []
            for index, item in enumerate(iter_collection(value)):
                if index >= max_items:
                    break
                preview.append(make_jsonable(item, depth + 1, max_depth, max_items))
            data["items"] = preview
            data["truncated"] = count > len(preview)

    return data


def make_jsonable(value, depth=0, max_depth=3, max_items=10):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Path):
        return str(value)

    if isinstance(value, dict):
        items = list(value.items())
        result = {}
        for index, (key, item_value) in enumerate(items):
            if index >= max_items:
                result["__truncated__"] = f"{len(items) - max_items} additional entries omitted"
                break
            result[str(key)] = make_jsonable(item_value, depth + 1, max_depth, max_items)
        return result

    if isinstance(value, (list, tuple, set)):
        items = list(value)
        result = [
            make_jsonable(item, depth + 1, max_depth, max_items)
            for item in items[:max_items]
        ]
        if len(items) > max_items:
            result.append({"__truncated__": len(items) - max_items})
        return result

    if callable(value):
        return {
            "callable": True,
            "name": getattr(value, "__name__", type(value).__name__),
            "repr": safe_repr(value),
        }

    if depth >= max_depth:
        return safe_repr(value)

    if is_adsk_like(value):
        return serialize_adsk_object(value, depth, max_depth, max_items)

    count = safe_count(value)
    if count:
        return serialize_collection_preview(value, depth, max_depth, max_items)

    return safe_repr(value)


def build_script_builtins(print_fn):
    return {
        "__import__": __import__,
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "callable": callable,
        "dict": dict,
        "dir": dir,
        "enumerate": enumerate,
        "Exception": Exception,
        "filter": filter,
        "float": float,
        "getattr": getattr,
        "hasattr": hasattr,
        "int": int,
        "isinstance": isinstance,
        "len": len,
        "list": list,
        "max": max,
        "map": map,
        "min": min,
        "next": next,
        "object": object,
        "pow": pow,
        "print": print_fn,
        "range": range,
        "repr": repr,
        "reversed": reversed,
        "round": round,
        "set": set,
        "setattr": setattr,
        "slice": slice,
        "sorted": sorted,
        "str": str,
        "sum": sum,
        "tuple": tuple,
        "RuntimeError": RuntimeError,
        "TypeError": TypeError,
        "type": type,
        "ValueError": ValueError,
        "zip": zip,
    }


def build_fusion_script_context(input_data=None, print_fn=None):
    products = get_active_products_context()
    doc = products["doc"]
    design = products["design"]
    drawing = products["drawing"]
    cam = products["cam"]
    ecad_document = products["ecad_document"]
    ecad_design = products["ecad_design"]
    schematic = products["schematic"]
    board = products["board"]
    library = products["library"]
    active_product = products["active_product"] or ecad_document or design or drawing or cam
    document_products = products["document_products"]
    selected_entities = get_selected_entities()
    exports_dir = ensure_dir((REPO_ROOT or ADDIN_DIR) / "exports")
    printer = print_fn or (lambda *args, **kwargs: None)

    def make_local_point3d(x, y, z=0.0, unit=None):
        if design:
            return make_point3d(design, x, y, z, unit)
        return adsk.core.Point3D.create(float(x), float(y), float(z))

    def make_local_point2d(x, y):
        return adsk.core.Point2D.create(float(x), float(y))

    def local_length_value(value, unit=None):
        if not design:
            return value
        return length_value(design, value, unit)

    def local_length_input(value, unit=None):
        if not design:
            return adsk.core.ValueInput.createByString(str(value))
        return length_value_input(design, value, unit)

    def local_angle_value(value, unit="deg"):
        if not design:
            return value
        return angle_value(design, value, unit)

    def local_angle_input(value, unit="deg"):
        if not design:
            return adsk.core.ValueInput.createByString(str(value))
        return angle_value_input(design, value, unit)

    def execute_text_command_local(command):
        if is_blank(command):
            raise ValueError("command is required")
        return app.executeTextCommand(str(command))

    def require_ecad_document():
        if not ecad_document:
            raise RuntimeError("Active document is not an electronics document")
        return ecad_document

    def begin_design_change_local(change_id=""):
        target = require_ecad_document()
        target.beginDesignChange(str(change_id or "MCP Electronics Change"))
        return True

    def end_design_change_local():
        target = require_ecad_document()
        target.endDesignChange()
        return True

    def cancel_design_change_local():
        target = require_ecad_document()
        target.cancelDesignChange()
        return True

    return {
        "adsk": adsk,
        "app": app,
        "ui": ui,
        "doc": doc,
        "document": doc,
        "active_product": active_product,
        "design": design,
        "design_error": products["design_error"],
        "drawing": drawing,
        "cam": cam,
        "ecad_document": ecad_document,
        "ecad_design": ecad_design,
        "electronics_document": ecad_document,
        "electronics_design": ecad_design,
        "schematic": schematic,
        "board": board,
        "library": library,
        "document_products": document_products,
        "root_component": design.rootComponent if design else None,
        "selected_entities": selected_entities,
        "selected_tokens": [safe_entity_token(entity) for entity in selected_entities],
        "input_data": input_data if input_data is not None else {},
        "state": SCRIPT_STATE,
        "math": math,
        "json": json,
        "re": re,
        "time": time,
        "Path": Path,
        "exports_dir": exports_dir,
        "comm_dir": primary_comm_dir(),
        "serialize": make_jsonable,
        "log": printer,
        "print": printer,
        "point3d": make_local_point3d,
        "point2d": make_local_point2d,
        "length_value": local_length_value,
        "length_input": local_length_input,
        "angle_value": local_angle_value,
        "angle_input": local_angle_input,
        "find_component": (lambda component_name="": find_component(design, component_name)) if design else (lambda component_name="": None),
        "find_sketch": (lambda sketch_name, component_name="": find_sketch(design, sketch_name, component_name)) if design else (lambda sketch_name, component_name="": None),
        "find_body": (lambda body_name, component_name="": find_body(design, body_name, component_name)) if design else (lambda body_name, component_name="": None),
        "find_plane": (lambda plane_name, component_name="": find_planar_entity(design, plane_name, component_name)) if design else (lambda plane_name, component_name="": None),
        "find_axis": (lambda axis_name, component_name="": find_axis_entity(design, axis_name, component_name)) if design else (lambda axis_name, component_name="": None),
        "find_entity": (lambda token: find_entity_by_token(design, token)) if design else (lambda token: None),
        "find_profile_entity": (
            lambda sketch_name="", component_name="", profile_index=0, profile_token="": resolve_profile_entity(
                design,
                sketch_name,
                component_name,
                profile_index,
                profile_token,
            )
        ) if design else (lambda **kwargs: None),
        "occurrence_for": (lambda component: find_occurrence_for_component(design, component)) if design else (lambda component: None),
        "entity_token": safe_entity_token,
        "entity_name": safe_get_name,
        "execute_text_command": execute_text_command_local,
        "text_command": execute_text_command_local,
        "begin_design_change": begin_design_change_local,
        "end_design_change": end_design_change_local,
        "cancel_design_change": cancel_design_change_local,
    }


def inspect_fusion_object_impl(path="root_component", include_private=False, max_members=200, include_values=True):
    try:
        expression = path if not is_blank(path) else "root_component"
        namespace = build_fusion_script_context()
        namespace["__builtins__"] = build_script_builtins(namespace["print"])
        target = eval(expression, namespace, namespace)

        member_names = sorted(dir(target))
        if not include_private:
            member_names = [name for name in member_names if not name.startswith("_")]

        members = []
        member_limit = max(0, int(max_members))
        for name in member_names[:member_limit]:
            entry = {"name": name}
            try:
                attr = getattr(target, name)
                entry["kind"] = "method" if callable(attr) else "attribute"
                entry["python_type"] = type(attr).__name__
                if callable(attr):
                    entry["repr"] = safe_repr(attr, 120)
                elif include_values:
                    entry["value"] = make_jsonable(attr, 1, 1, 5)
            except Exception as exc:
                entry["kind"] = "error"
                entry["error"] = str(exc)
            members.append(entry)

        return {
            "path": expression,
            "summary": make_jsonable(target, 0, 2, 8),
            "member_count": len(member_names),
            "members": members,
            "truncated": len(member_names) > len(members),
            "context_keys": sorted(key for key in namespace.keys() if not key.startswith("__")),
        }
    except Exception as exc:
        return error_payload("inspect_fusion_object failed", exc)


def execute_fusion_api_impl(script="", expression="", input_data=None, result_variable="result"):
    logs = []

    def capture_print(*values, sep=" ", end="\n"):
        message = sep.join(str(value) for value in values)
        if end:
            message += end.rstrip("\n")
        logs.append(message)

    try:
        if is_blank(script) and is_blank(expression):
            raise ValueError("script or expression is required")

        namespace = build_fusion_script_context(input_data, capture_print)
        namespace["__builtins__"] = build_script_builtins(capture_print)
        namespace[result_variable] = None

        if not is_blank(expression):
            namespace[result_variable] = eval(expression, namespace, namespace)

        if not is_blank(script):
            code = compile(str(script), "<fusion_mcp>", "exec")
            exec(code, namespace, namespace)

        # Give Fusion a brief chance to settle after scripts that open documents,
        # switch products, or trigger asynchronous UI/model updates.
        settle_fusion_processing(cycles=10, delay_seconds=0.1)

        raw_result = namespace.get(result_variable)
        SCRIPT_STATE["last_result"] = raw_result

        return {
            "success": True,
            "result_variable": result_variable,
            "result": make_jsonable(raw_result, 0, 4, 20),
            "logs": logs,
            "state_keys": sorted(str(key) for key in SCRIPT_STATE.keys()),
            "context": {
                "document_type": get_document_type_name(namespace["doc"]) if namespace.get("doc") else None,
                "has_design": bool(namespace.get("design")),
                "has_drawing": bool(namespace.get("drawing")),
                "has_cam": bool(namespace.get("cam")),
                "selected_count": len(namespace.get("selected_entities", [])),
            },
        }
    except Exception as exc:
        payload = error_payload("execute_fusion_api failed", exc)
        payload["logs"] = logs
        return payload


def get_active_document_info_resource():
    try:
        doc = app.activeDocument
        if not doc:
            return {"error": "No active document"}

        path = "Unsaved"
        try:
            if hasattr(doc, "dataFile") and doc.dataFile:
                path = doc.dataFile.name
        except Exception:
            pass

        return {
            "name": doc.name,
            "path": path,
            "type": get_document_type_name(doc),
        }
    except Exception as exc:
        return error_payload("Error reading active document", exc)


def get_components_resource():
    try:
        _, design = get_active_design_context()
        return {
            "components": [serialize_component(component) for component in get_all_components(design)]
        }
    except Exception as exc:
        return error_payload("Error reading components", exc)


def get_sketches_resource():
    try:
        _, design = get_active_design_context()
        sketches = []
        for _, sketch in iter_all_sketches(design):
            sketches.append(serialize_sketch(sketch))
        return {"sketches": sketches}
    except Exception as exc:
        return error_payload("Error reading sketches", exc)


def get_bodies_resource():
    try:
        _, design = get_active_design_context()
        bodies = []
        for _, body in iter_all_bodies(design):
            bodies.append(serialize_body(body))
        return {"bodies": bodies}
    except Exception as exc:
        return error_payload("Error reading bodies", exc)


def get_electronics_target_context():
    products = get_active_products_context()
    return {
        "ecad_document": products["ecad_document"],
        "ecad_design": products["ecad_design"],
        "schematic": products["schematic"],
        "board": products["board"],
        "library": products["library"],
        "document_products": products["document_products"],
        "active_product": products["active_product"] or products["ecad_document"],
    }


def resolve_electronics_target_document(target="active"):
    context = get_electronics_target_context()
    key = normalize_key(target or "active")

    if key in ("active", "document", "ecad", "electronics", "electronics_document"):
        document = context["ecad_document"] or context["schematic"] or context["board"] or context["library"] or context["ecad_design"]
    elif key in ("schematic", "sch"):
        document = context["schematic"]
    elif key in ("board", "brd", "pcb"):
        document = context["board"]
    elif key in ("library", "lbr"):
        document = context["library"]
    elif key in ("design", "ecad_design", "electronics_design"):
        document = context["ecad_design"]
    else:
        raise ValueError(f"Unsupported electronics target: {target}")

    if not document:
        raise RuntimeError(f"No active electronics document available for target: {target}")
    return document


def get_electronics_context_resource():
    try:
        products = get_active_products_context()
        context = {
            "document_name": getattr(products["doc"], "name", None),
            "active_product_type": safe_object_type(products["active_product"]),
            "document_products": [
                {
                    "name": safe_get_name(product),
                    "object_type": safe_object_type(product),
                    "product_type": safe_getattr(product, "productType"),
                }
                for product in products["document_products"]
            ],
            # Keep this resource compact so recently opened electronics projects
            # don't trigger deep object walks while Fusion is still settling.
            "ecad_document": serialize_electronics_document_summary(products["ecad_document"]),
            "ecad_design": serialize_electronics_design_summary(products["ecad_design"]),
            "schematic": serialize_electronics_schematic_summary(products["schematic"]),
            "board": serialize_electronics_board_summary(products["board"]),
            "library": serialize_electronics_library_summary(products["library"]),
        }
        return context
    except Exception as exc:
        return error_payload("Error reading electronics context", exc)


def get_electronics_schematic_resource():
    try:
        return {"schematic": serialize_electronics_schematic_summary(resolve_electronics_target_document("schematic"))}
    except Exception as exc:
        return error_payload("Error reading electronics schematic", exc)


def get_electronics_board_resource():
    try:
        return {"board": serialize_electronics_board_summary(resolve_electronics_target_document("board"))}
    except Exception as exc:
        return error_payload("Error reading electronics board", exc)


def get_electronics_library_resource():
    try:
        return {"library": serialize_electronics_library_summary(resolve_electronics_target_document("library"))}
    except Exception as exc:
        return error_payload("Error reading electronics library", exc)


def get_electronics_libraries_resource():
    try:
        context = get_electronics_target_context()
        libraries = []
        if context["schematic"] and getattr(context["schematic"], "libraries", None):
            libraries.extend(iter_collection(context["schematic"].libraries))
        elif context["board"] and getattr(context["board"], "libraries", None):
            libraries.extend(iter_collection(context["board"].libraries))
        elif context["library"]:
            libraries.append(context["library"])

        return {
            "libraries": [serialize_electronics_library_summary(library) for library in libraries]
        }
    except Exception as exc:
        return error_payload("Error reading electronics libraries", exc)


def get_electronics_documents_resource():
    try:
        active_folder = get_active_data_folder()
        open_documents = [get_document_summary(document) for document in iter_open_documents()]
        electronics_data_files = []
        if active_folder:
            for data_file in iter_collection(safe_getattr(active_folder, "dataFiles")):
                extension = get_data_file_extension(data_file)
                if extension in ("fsch", "fbrd", "fprj", "flbr", "sch", "brd", "lbr", "prj"):
                    electronics_data_files.append(get_data_file_summary(data_file))

        return {
            "active_folder_name": safe_get_name(active_folder),
            "active_folder_id": safe_getattr(active_folder, "id"),
            "open_documents": open_documents,
            "folder_data_files": electronics_data_files,
        }
    except Exception as exc:
        return error_payload("Error reading electronics documents", exc)


def get_electronics_errors_resource():
    try:
        context = get_electronics_target_context()
        schematic_errors = []
        board_errors = []

        schematic = context["schematic"]
        if schematic and getattr(schematic, "errors", None):
            schematic_errors = [serialize_electronics_error(item) for item in iter_collection(schematic.errors)]

        board = context["board"]
        if board and getattr(board, "errors", None):
            board_errors = [serialize_electronics_error(item) for item in iter_collection(board.errors)]

        return {
            "schematic_errors": schematic_errors,
            "board_errors": board_errors,
        }
    except Exception as exc:
        return error_payload("Error reading electronics errors", exc)


def get_mcp_capabilities_resource():
    try:
        context_keys = sorted(build_fusion_script_context().keys())
        return {
            "resources": RESOURCE_URIS,
            "tools": TOOL_METADATA,
            "prompts": PROMPT_METADATA,
            "generic_bridge": {
                "inspect_tool": "inspect_fusion_object",
                "execute_tool": "execute_fusion_api",
                "stateful": True,
                "context_keys": context_keys,
                "notes": [
                    "execute_fusion_api exposes design, drawing, cam, selection, and helper find_* functions.",
                    "execute_electronics_api exposes ecad_document, ecad_design, schematic, board, library, and transaction helpers.",
                    "Use inspect_fusion_object with Python paths such as design.rootComponent.features or cam.setups.",
                    "Interactive UI commands may still need manual user interaction, but API coverage is no longer limited to the fixed tool list.",
                ],
            },
        }
    except Exception as exc:
        return error_payload("Error reading MCP capabilities", exc)


def get_parameters_resource():
    try:
        _, design = get_active_design_context()
        parameters = []
        for parameter in design.allParameters:
            parameters.append(
                {
                    "name": parameter.name,
                    "value": parameter.value,
                    "expression": parameter.expression,
                    "unit": parameter.unit,
                    "comment": parameter.comment,
                    "token": safe_entity_token(parameter),
                }
            )
        return {"parameters": parameters}
    except Exception as exc:
        return error_payload("Error reading parameters", exc)


def get_design_structure_resource():
    try:
        doc, design = get_active_design_context()
        root_component = design.rootComponent
        occurrences = []
        for occurrence in iter_collection(root_component.allOccurrences):
            occurrences.append(
                {
                    "name": safe_get_name(occurrence),
                    "token": safe_entity_token(occurrence),
                    "component_name": safe_get_name(occurrence.component),
                    "component_token": safe_entity_token(occurrence.component),
                }
            )

        return {
            "design_name": doc.name,
            "root_component": serialize_component(root_component),
            "components": [serialize_component(component) for component in get_all_components(design)],
            "occurrences": occurrences,
        }
    except Exception as exc:
        return error_payload("Error reading design structure", exc)


def message_box_impl(message):
    try:
        debug_path = comm_file("message_tool_debug.txt")
        append_text(debug_path, f"Message box tool called with: {message} at {time.ctime()}\n")
        success = show_message_box(message)
        append_text(debug_path, f"Direct show result: {success} at {time.ctime()}\n")
        return "Message displayed successfully (queued if not shown immediately)"
    except Exception as exc:
        return error_message("Error displaying message", exc)


def create_new_sketch_impl(plane_name, component_name="", sketch_name=""):
    try:
        _, design = get_active_design_context()
        planar_entity = find_planar_entity(design, plane_name, component_name)
        target_component = find_component(design, component_name) if not is_blank(component_name) else get_entity_parent_component(planar_entity) or design.rootComponent
        sketch = target_component.sketches.add(planar_entity)
        sketch.name = sketch_name or f"Sketch_MCP_{int(time.time()) % 10000}"
        return f"Sketch created successfully: {sketch.name}"
    except Exception as exc:
        return error_message("Error creating sketch", exc)


def create_parameter_impl(name, expression, unit, comment=""):
    try:
        _, design = get_active_design_context()
        parameter_name = name or f"Param_{int(time.time()) % 10000}"
        normalized_expression = expression_from_value(expression, unit)

        try:
            parameter = design.userParameters.add(
                parameter_name,
                adsk.core.ValueInput.createByString(normalized_expression),
                unit,
                comment,
            )
            return f"Parameter created successfully: {parameter.name} = {parameter.expression}"
        except Exception:
            existing_parameter = design.userParameters.itemByName(parameter_name)
            if not existing_parameter:
                raise
            existing_parameter.expression = normalized_expression
            existing_parameter.unit = unit
            if comment:
                existing_parameter.comment = comment
            return f"Parameter updated: {existing_parameter.name} = {existing_parameter.expression}"
    except Exception as exc:
        return error_message("Error creating parameter", exc)


def create_component_impl(name, reuse_existing=True):
    try:
        _, design = get_active_design_context()
        component_name = name or f"Component_MCP_{int(time.time()) % 10000}"

        if reuse_existing:
            try:
                existing = find_component(design, component_name)
                if existing and existing != design.rootComponent:
                    return {
                        "message": f"Component already exists: {existing.name}",
                        "created": False,
                        "component": serialize_component(existing),
                    }
            except Exception:
                pass

        occurrence = design.rootComponent.occurrences.addNewComponent(adsk.core.Matrix3D.create())
        component = occurrence.component
        component.name = component_name
        return {
            "message": f"Component created successfully: {component.name}",
            "created": True,
            "component": serialize_component(component),
            "occurrence": {
                "name": safe_get_name(occurrence),
                "token": safe_entity_token(occurrence),
            },
        }
    except Exception as exc:
        return error_payload("Error creating component", exc)


def create_offset_plane_impl(base_plane_name, offset, component_name="", plane_name=""):
    try:
        _, design = get_active_design_context()
        base_entity = find_planar_entity(design, base_plane_name, component_name)
        target_component = find_component(design, component_name) if not is_blank(component_name) else get_entity_parent_component(base_entity) or design.rootComponent
        plane_input = target_component.constructionPlanes.createInput()
        if not plane_input.setByOffset(base_entity, length_value_input(design, offset)):
            raise RuntimeError("Fusion rejected the offset plane input")
        plane = target_component.constructionPlanes.add(plane_input)
        plane.name = plane_name or f"OffsetPlane_MCP_{int(time.time()) % 10000}"
        return {
            "message": f"Construction plane created successfully: {plane.name}",
            "plane": {
                "name": plane.name,
                "token": safe_entity_token(plane),
                "parent_component": safe_get_name(target_component),
            },
        }
    except Exception as exc:
        return error_payload("Error creating offset plane", exc)


def list_sketch_entities_impl(sketch_name, component_name=""):
    try:
        _, design = get_active_design_context()
        sketch = find_sketch(design, sketch_name, component_name)
        return {
            "sketch": serialize_sketch(sketch),
            "points": [serialize_sketch_entity(point) for point in iter_collection(get_sketch_points_collection(sketch))],
            "curves": [serialize_sketch_entity(curve) for curve in iter_collection(get_sketch_curves_collection(sketch))],
        }
    except Exception as exc:
        return error_payload("Error listing sketch entities", exc)


def list_sketch_profiles_impl(sketch_name, component_name=""):
    try:
        _, design = get_active_design_context()
        sketch = find_sketch(design, sketch_name, component_name)
        profiles = []
        for index, profile in enumerate(iter_collection(sketch.profiles)):
            profiles.append(serialize_profile(profile, index))
        return {
            "sketch": serialize_sketch(sketch),
            "profiles": profiles,
        }
    except Exception as exc:
        return error_payload("Error listing sketch profiles", exc)


def create_sketch_point_impl(sketch_name, x, y, z=0.0, component_name=""):
    try:
        _, design = get_active_design_context()
        sketch = find_sketch(design, sketch_name, component_name)
        point = get_sketch_points_collection(sketch).add(make_point3d(design, x, y, z))
        return {
            "message": "Sketch point created successfully",
            "sketch": serialize_sketch(sketch),
            "point": serialize_sketch_entity(point),
        }
    except Exception as exc:
        return error_payload("Error creating sketch point", exc)


def create_sketch_line_impl(sketch_name, start_x, start_y, end_x, end_y, start_z=0.0, end_z=0.0, component_name=""):
    try:
        _, design = get_active_design_context()
        sketch = find_sketch(design, sketch_name, component_name)
        line = get_sketch_lines_collection(sketch).addByTwoPoints(
            make_point3d(design, start_x, start_y, start_z),
            make_point3d(design, end_x, end_y, end_z),
        )
        return {
            "message": "Sketch line created successfully",
            "sketch": serialize_sketch(sketch),
            "line": serialize_sketch_entity(line),
        }
    except Exception as exc:
        return error_payload("Error creating sketch line", exc)


def create_sketch_lines_impl(sketch_name, points, component_name=""):
    try:
        if not points or len(points) < 2:
            raise ValueError("At least two points are required")

        _, design = get_active_design_context()
        sketch = find_sketch(design, sketch_name, component_name)
        created_lines = []

        for index in range(len(points) - 1):
            start_point = point3d_from_data(design, points[index])
            end_point = point3d_from_data(design, points[index + 1])
            line = get_sketch_lines_collection(sketch).addByTwoPoints(start_point, end_point)
            created_lines.append(serialize_sketch_entity(line))

        return {
            "message": f"{len(created_lines)} sketch lines created successfully",
            "sketch": serialize_sketch(sketch),
            "lines": created_lines,
        }
    except Exception as exc:
        return error_payload("Error creating sketch lines", exc)


def create_sketch_circle_impl(sketch_name, center_x, center_y, radius, center_z=0.0, component_name=""):
    try:
        _, design = get_active_design_context()
        sketch = find_sketch(design, sketch_name, component_name)
        circle = get_sketch_circles_collection(sketch).addByCenterRadius(
            make_point3d(design, center_x, center_y, center_z),
            length_value(design, radius),
        )
        return {
            "message": "Sketch circle created successfully",
            "sketch": serialize_sketch(sketch),
            "circle": serialize_sketch_entity(circle),
        }
    except Exception as exc:
        return error_payload("Error creating sketch circle", exc)


def create_sketch_rectangle_impl(sketch_name, x1, y1, x2, y2, z1=0.0, z2=0.0, component_name=""):
    try:
        _, design = get_active_design_context()
        sketch = find_sketch(design, sketch_name, component_name)
        rectangle = get_sketch_lines_collection(sketch).addTwoPointRectangle(
            make_point3d(design, x1, y1, z1),
            make_point3d(design, x2, y2, z2),
        )
        return {
            "message": "Sketch rectangle created successfully",
            "sketch": serialize_sketch(sketch),
            "lines": [serialize_sketch_entity(line) for line in iter_collection(rectangle)],
        }
    except Exception as exc:
        return error_payload("Error creating sketch rectangle", exc)


def create_sketch_center_rectangle_impl(sketch_name, center_x, center_y, corner_x, corner_y, center_z=0.0, corner_z=0.0, component_name=""):
    try:
        _, design = get_active_design_context()
        sketch = find_sketch(design, sketch_name, component_name)
        rectangle = get_sketch_lines_collection(sketch).addCenterPointRectangle(
            make_point3d(design, center_x, center_y, center_z),
            make_point3d(design, corner_x, corner_y, corner_z),
        )
        return {
            "message": "Center-point sketch rectangle created successfully",
            "sketch": serialize_sketch(sketch),
            "lines": [serialize_sketch_entity(line) for line in iter_collection(rectangle)],
        }
    except Exception as exc:
        return error_payload("Error creating center-point sketch rectangle", exc)


def create_sketch_arc_impl(sketch_name, center_x, center_y, start_x, start_y, sweep_angle, center_z=0.0, start_z=0.0, component_name=""):
    try:
        _, design = get_active_design_context()
        sketch = find_sketch(design, sketch_name, component_name)
        arc = get_sketch_arcs_collection(sketch).addByCenterStartSweep(
            make_point3d(design, center_x, center_y, center_z),
            make_point3d(design, start_x, start_y, start_z),
            angle_value(design, sweep_angle),
        )
        return {
            "message": "Sketch arc created successfully",
            "sketch": serialize_sketch(sketch),
            "arc": serialize_sketch_entity(arc),
        }
    except Exception as exc:
        return error_payload("Error creating sketch arc", exc)


def create_sketch_spline_impl(sketch_name, points, component_name=""):
    try:
        if not points or len(points) < 2:
            raise ValueError("At least two points are required")

        _, design = get_active_design_context()
        sketch = find_sketch(design, sketch_name, component_name)
        fit_points = adsk.core.ObjectCollection.create()
        for point_data in points:
            fit_points.add(point3d_from_data(design, point_data))

        spline = get_sketch_fitted_splines_collection(sketch).add(fit_points)
        return {
            "message": "Sketch spline created successfully",
            "sketch": serialize_sketch(sketch),
            "spline": serialize_sketch_entity(spline),
        }
    except Exception as exc:
        return error_payload("Error creating sketch spline", exc)


def add_sketch_constraint_impl(sketch_name, constraint_type, entity_one_token="", entity_two_token="", entity_three_token="", component_name=""):
    try:
        _, design = get_active_design_context()
        sketch = find_sketch(design, sketch_name, component_name)
        constraints = get_geometric_constraints_collection(sketch)

        entity_one = find_entity_by_token(design, entity_one_token)
        entity_two = find_entity_by_token(design, entity_two_token)
        entity_three = find_entity_by_token(design, entity_three_token)
        key = normalize_key(constraint_type)

        if key == "coincident":
            constraint = constraints.addCoincident(
                require_sketch_point(entity_one, "entity_one_token"),
                adsk.fusion.SketchEntity.cast(entity_two),
            )
        elif key == "concentric":
            constraint = constraints.addConcentric(
                require_sketch_curve(entity_one, "entity_one_token"),
                require_sketch_curve(entity_two, "entity_two_token"),
            )
        elif key in ("midpoint", "mid_point"):
            constraint = constraints.addMidPoint(
                require_sketch_point(entity_one, "entity_one_token"),
                require_sketch_curve(entity_two, "entity_two_token"),
            )
        elif key == "parallel":
            constraint = constraints.addParallel(
                require_sketch_line(entity_one, "entity_one_token"),
                require_sketch_line(entity_two, "entity_two_token"),
            )
        elif key == "perpendicular":
            constraint = constraints.addPerpendicular(
                require_sketch_line(entity_one, "entity_one_token"),
                require_sketch_line(entity_two, "entity_two_token"),
            )
        elif key == "horizontal":
            constraint = constraints.addHorizontal(require_sketch_line(entity_one, "entity_one_token"))
        elif key == "vertical":
            constraint = constraints.addVertical(require_sketch_line(entity_one, "entity_one_token"))
        elif key == "equal":
            constraint = constraints.addEqual(
                require_sketch_curve(entity_one, "entity_one_token"),
                require_sketch_curve(entity_two, "entity_two_token"),
            )
        elif key == "symmetry":
            constraint = constraints.addSymmetry(
                adsk.fusion.SketchEntity.cast(entity_one),
                adsk.fusion.SketchEntity.cast(entity_two),
                require_sketch_line(entity_three, "entity_three_token"),
            )
        elif key == "tangent":
            constraint = constraints.addTangent(
                require_sketch_curve(entity_one, "entity_one_token"),
                require_sketch_curve(entity_two, "entity_two_token"),
            )
        elif key == "collinear" and hasattr(constraints, "addCollinear"):
            constraint = constraints.addCollinear(
                require_sketch_line(entity_one, "entity_one_token"),
                require_sketch_line(entity_two, "entity_two_token"),
            )
        elif key == "smooth" and hasattr(constraints, "addSmooth"):
            constraint = constraints.addSmooth(
                require_sketch_curve(entity_one, "entity_one_token"),
                require_sketch_curve(entity_two, "entity_two_token"),
            )
        else:
            raise ValueError(f"Unsupported constraint type: {constraint_type}")

        return {
            "message": f"Constraint added successfully: {constraint_type}",
            "sketch": serialize_sketch(sketch),
            "constraint_type": constraint_type,
            "constraint_object_type": safe_object_type(constraint),
        }
    except Exception as exc:
        return error_payload("Error adding sketch constraint", exc)


def add_sketch_dimension_impl(sketch_name, dimension_type, entity_one_token, entity_two_token="", text_x=0.0, text_y=0.0, text_z=0.0, orientation="aligned", expression="", component_name=""):
    try:
        _, design = get_active_design_context()
        sketch = find_sketch(design, sketch_name, component_name)
        dimensions = get_sketch_dimensions_collection(sketch)
        entity_one = find_entity_by_token(design, entity_one_token)
        entity_two = find_entity_by_token(design, entity_two_token)
        text_point = make_point3d(design, text_x, text_y, text_z)
        dimension_key = normalize_key(dimension_type)

        if dimension_key in ("distance", "linear", "length"):
            dimension = dimensions.addDistanceDimension(
                require_sketch_point(entity_one, "entity_one_token"),
                require_sketch_point(entity_two, "entity_two_token"),
                dimension_orientation_from_name(orientation),
                text_point,
                True,
            )
            expression_unit = get_units_manager(design).defaultLengthUnits or "cm"
        elif dimension_key in ("diameter", "diametric"):
            dimension = dimensions.addDiameterDimension(
                require_sketch_curve(entity_one, "entity_one_token"),
                text_point,
                True,
            )
            expression_unit = get_units_manager(design).defaultLengthUnits or "cm"
        elif dimension_key in ("radial", "radius"):
            dimension = dimensions.addRadialDimension(
                require_sketch_curve(entity_one, "entity_one_token"),
                text_point,
                True,
            )
            expression_unit = get_units_manager(design).defaultLengthUnits or "cm"
        else:
            raise ValueError(f"Unsupported dimension type: {dimension_type}")

        if not is_blank(expression) and dimension.parameter:
            dimension.parameter.expression = expression_from_value(expression, expression_unit)

        return {
            "message": f"Sketch dimension created successfully: {dimension_type}",
            "sketch": serialize_sketch(sketch),
            "dimension": serialize_dimension(dimension),
        }
    except Exception as exc:
        return error_payload("Error adding sketch dimension", exc)


def create_extrude_impl(sketch_name="", distance="10 mm", profile_index=0, operation="new_body", component_name="", feature_name="", body_name="", direction="positive", profile_token=""):
    try:
        _, design = get_active_design_context()
        profile = resolve_profile_entity(design, sketch_name, component_name, profile_index, profile_token)
        sketch = profile.parentSketch
        component = sketch.parentComponent
        extrudes = component.features.extrudeFeatures
        extrude_input = extrudes.createInput(profile, feature_operation_from_name(operation))
        extent = adsk.fusion.DistanceExtentDefinition.create(length_value_input(design, distance))
        extrude_input.setOneSideExtent(extent, extent_direction_from_name(direction))
        feature = extrudes.add(extrude_input)

        if feature_name:
            feature.name = feature_name

        bodies = []
        try:
            for index, body in enumerate(iter_collection(feature.bodies)):
                if index == 0 and body_name:
                    body.name = body_name
                bodies.append(serialize_body(body))
        except Exception:
            pass

        return {
            "message": "Extrude feature created successfully",
            "feature_name": safe_get_name(feature),
            "component": safe_get_name(component),
            "bodies": bodies,
        }
    except Exception as exc:
        return error_payload("Error creating extrude", exc)


def create_revolve_impl(sketch_name="", axis_token="", angle="360 deg", profile_index=0, operation="new_body", component_name="", feature_name="", body_name="", profile_token="", axis_name=""):
    try:
        _, design = get_active_design_context()
        profile = resolve_profile_entity(design, sketch_name, component_name, profile_index, profile_token)
        sketch = profile.parentSketch
        component = sketch.parentComponent
        axis_entity = find_entity_by_token(design, axis_token) if not is_blank(axis_token) else find_axis_entity(design, axis_name, component.name)
        revolves = component.features.revolveFeatures
        revolve_input = revolves.createInput(profile, axis_entity, feature_operation_from_name(operation))
        revolve_input.setAngleExtent(False, angle_value_input(design, angle))
        feature = revolves.add(revolve_input)

        if feature_name:
            feature.name = feature_name

        bodies = []
        try:
            for index, body in enumerate(iter_collection(feature.bodies)):
                if index == 0 and body_name:
                    body.name = body_name
                bodies.append(serialize_body(body))
        except Exception:
            pass

        return {
            "message": "Revolve feature created successfully",
            "feature_name": safe_get_name(feature),
            "component": safe_get_name(component),
            "bodies": bodies,
        }
    except Exception as exc:
        return error_payload("Error creating revolve", exc)


def delete_body_impl(body_name, component_name="", allow_partial_match=False, delete_all_matches=False):
    try:
        _, design = get_active_design_context()
        normalized_name = normalize_key(body_name)
        matched_bodies = []

        if is_blank(body_name):
            raise ValueError("body_name is required")

        if not is_blank(component_name):
            component = find_component(design, component_name)
            candidates = [(component, body) for body in iter_collection(get_component_bodies_collection(component))]
        else:
            candidates = list(iter_all_bodies(design))

        for parent_component, candidate in candidates:
            candidate_name = safe_get_name(candidate)
            candidate_key = normalize_key(candidate_name)
            exact_match = candidate_name == body_name or candidate_key == normalized_name
            partial_match = allow_partial_match and normalized_name and normalized_name in candidate_key
            if exact_match or partial_match:
                matched_bodies.append((parent_component, candidate))

        if not matched_bodies:
            raise ValueError(f"Could not find body: {body_name}")

        deleted_bodies = []
        errors = []
        for index, (parent_component, body) in enumerate(matched_bodies):
            if index > 0 and not delete_all_matches:
                break

            body_info = {
                "name": safe_get_name(body),
                "parent_component": safe_get_name(parent_component),
                "token": safe_entity_token(body),
            }
            try:
                body.deleteMe()
                deleted_bodies.append(body_info)
            except Exception as exc:
                body_info["error"] = str(exc)
                errors.append(body_info)

        if not deleted_bodies:
            raise ValueError(f"Found matching bodies but failed to delete them: {errors}")

        try:
            app.activeViewport.refresh()
        except Exception:
            pass

        return {
            "message": "Body deleted successfully" if len(deleted_bodies) == 1 else "Bodies deleted successfully",
            "deleted_count": len(deleted_bodies),
            "deleted_bodies": deleted_bodies,
            "skipped_matches": max(0, len(matched_bodies) - len(deleted_bodies)),
            "errors": errors,
        }
    except Exception as exc:
        return error_payload("Error deleting body", exc)


def export_sketch_dxf_impl(sketch_name, filename="", component_name=""):
    try:
        doc, design = get_active_design_context()
        sketch = find_sketch(design, sketch_name, component_name)
        output_path = resolve_output_path(filename, f"{doc.name}_{sketch.name}", ".dxf")
        export_options = design.exportManager.createDXFSketchExportOptions(str(output_path), sketch)
        success = design.exportManager.execute(export_options)
        return {
            "message": "Sketch DXF export completed" if success else "Sketch DXF export failed",
            "success": success,
            "path": str(output_path),
        }
    except Exception as exc:
        return error_payload("Error exporting sketch DXF", exc)


def export_design_file_impl(format, filename="", component_name="", body_name=""):
    try:
        doc, design = get_active_design_context()
        format_key = normalize_key(format)
        extension_map = {
            "step": ".step",
            "stp": ".step",
            "iges": ".iges",
            "igs": ".iges",
            "sat": ".sat",
            "stl": ".stl",
            "3mf": ".3mf",
            "c3mf": ".3mf",
            "obj": ".obj",
        }
        if format_key not in extension_map:
            raise ValueError(f"Unsupported export format: {format}")

        output_path = resolve_output_path(filename, f"{doc.name}_{format_key}", extension_map[format_key])
        export_manager = design.exportManager
        target_component = None if is_blank(component_name) else find_component(design, component_name)
        target_body = None if is_blank(body_name) else find_body(design, body_name, component_name)

        if format_key in ("step", "stp", "iges", "igs", "sat"):
            if target_body:
                raise ValueError("STEP, IGES, and SAT exports support whole-design or component exports, not individual bodies")
            if target_component and target_component != design.rootComponent:
                geometry = find_occurrence_for_component(design, target_component)
                if not geometry:
                    raise ValueError(f"Could not find occurrence for component: {target_component.name}")
            else:
                geometry = design.rootComponent

            if format_key in ("step", "stp"):
                export_options = export_manager.createSTEPExportOptions(str(output_path), geometry)
            elif format_key in ("iges", "igs"):
                export_options = export_manager.createIGESExportOptions(str(output_path), geometry)
            else:
                export_options = export_manager.createSATExportOptions(str(output_path), geometry)
        else:
            geometry = target_body
            if not geometry:
                if target_component and target_component != design.rootComponent:
                    geometry = find_occurrence_for_component(design, target_component)
                    if not geometry:
                        raise ValueError(f"Could not find occurrence for component: {target_component.name}")
                else:
                    geometry = design.rootComponent

            if format_key == "stl":
                export_options = export_manager.createSTLExportOptions(geometry, str(output_path))
            elif format_key in ("3mf", "c3mf"):
                export_options = export_manager.createC3MFExportOptions(geometry, str(output_path))
            else:
                export_options = export_manager.createOBJExportOptions(geometry, str(output_path))

        success = export_manager.execute(export_options)
        return {
            "message": "Design export completed" if success else "Design export failed",
            "success": success,
            "format": format,
            "path": str(output_path),
        }
    except Exception as exc:
        return error_payload("Error exporting design file", exc)


def export_active_drawing_pdf_impl(filename=""):
    try:
        doc = app.activeDocument
        if not doc:
            raise RuntimeError("No active document")

        drawing_document = adsk.drawing.DrawingDocument.cast(doc)
        drawing = drawing_document.drawing if drawing_document else None
        if not drawing:
            raise RuntimeError("Active document is not a Fusion drawing")

        output_path = resolve_output_path(filename, f"{doc.name}_drawing", ".pdf")
        export_options = drawing.exportManager.createPDFExportOptions(str(output_path))
        success = drawing.exportManager.execute(export_options)
        return {
            "message": "Drawing PDF export completed" if success else "Drawing PDF export failed",
            "success": success,
            "path": str(output_path),
        }
    except Exception as exc:
        return error_payload("Error exporting drawing PDF", exc)


def create_electronics_sheet_impl(name=""):
    schematic = resolve_electronics_target_document("schematic")
    change_started = False
    try:
        schematic.beginDesignChange(f"Create sheet {name or ''}".strip())
        change_started = True
        sheet = schematic.sheets.create(name) if not is_blank(name) else schematic.sheets.create()
        schematic.endDesignChange()
        change_started = False
        return {
            "message": "Electronics sheet created successfully",
            "sheet": serialize_electronics_sheet(sheet),
            "schematic": serialize_electronics_schematic(schematic),
        }
    except Exception as exc:
        if change_started:
            try:
                schematic.cancelDesignChange()
            except Exception:
                pass
        return error_payload("Error creating electronics sheet", exc)


def begin_electronics_change_impl(change_id="", target="active"):
    try:
        document = resolve_electronics_target_document(target)
        document.beginDesignChange(str(change_id or "MCP Electronics Change"))
        return {
            "message": "Electronics design-change started",
            "target": safe_get_name(document),
            "object_type": safe_object_type(document),
        }
    except Exception as exc:
        return error_payload("Error beginning electronics design change", exc)


def end_electronics_change_impl(target="active"):
    try:
        document = resolve_electronics_target_document(target)
        document.endDesignChange()
        return {
            "message": "Electronics design-change committed",
            "target": safe_get_name(document),
            "object_type": safe_object_type(document),
        }
    except Exception as exc:
        return error_payload("Error ending electronics design change", exc)


def cancel_electronics_change_impl(target="active"):
    try:
        document = resolve_electronics_target_document(target)
        document.cancelDesignChange()
        return {
            "message": "Electronics design-change cancelled",
            "target": safe_get_name(document),
            "object_type": safe_object_type(document),
        }
    except Exception as exc:
        return error_payload("Error cancelling electronics design change", exc)


def resolve_electronics_target_extensions(target):
    key = normalize_key(target or "active")
    if key in ("schematic", "sch"):
        return ("fsch", "sch")
    if key in ("board", "brd", "pcb"):
        return ("fbrd", "brd")
    if key in ("library", "lbr"):
        return ("flbr", "lbr")
    if key in ("project", "design", "ecad_design", "electronics_design", "fprj", "prj"):
        return ("fprj", "prj", "fsch", "sch", "fbrd", "brd")
    if key in ("all", "related"):
        return ("fsch", "sch", "fbrd", "brd", "fprj", "prj", "flbr", "lbr")
    return ("fsch", "sch", "fbrd", "brd", "fprj", "prj", "flbr", "lbr")


def open_data_file_if_needed(data_file):
    target_id = safe_getattr(data_file, "id")
    for document in iter_open_documents():
        document_data_file = get_data_file(document)
        if document_data_file and safe_getattr(document_data_file, "id") == target_id:
            return document
    return app.documents.open(data_file)


def list_electronics_documents_impl():
    try:
        return get_electronics_documents_resource()
    except Exception as exc:
        return error_payload("Error listing electronics documents", exc)


def open_electronics_document_impl(name, target="schematic", activate=True, open_related=False):
    try:
        if is_blank(name):
            raise ValueError("name is required")

        requested_extensions = resolve_electronics_target_extensions(target)
        matching_data_files = get_matching_data_files(name, requested_extensions)
        if not matching_data_files:
            raise ValueError(f"Could not find uploaded electronics files for {name} with target {target}")

        open_targets = list(matching_data_files)
        preferred_extension_order = list(requested_extensions)
        open_targets.sort(key=lambda item: preferred_extension_order.index(get_data_file_extension(item)) if get_data_file_extension(item) in preferred_extension_order else 999)

        opened_documents = []
        errors = []

        for data_file in open_targets:
            try:
                document = open_data_file_if_needed(data_file)
                if document:
                    opened_documents.append(document)
                    if not open_related:
                        break
            except Exception as exc:
                errors.append({
                    "name": get_data_file_name(data_file),
                    "extension": get_data_file_extension(data_file),
                    "error": str(exc),
                })

        if not opened_documents:
            raise RuntimeError(f"Unable to open any electronics documents for {name}: {errors}")

        activated_document = None
        if activate:
            preferred_extensions = list(requested_extensions)
            opened_documents.sort(key=lambda document: preferred_extensions.index(get_data_file_extension(document)) if get_data_file_extension(document) in preferred_extensions else 999)
            activated_document = opened_documents[0]
            activated_document.activate()

        settle_fusion_processing(cycles=12, delay_seconds=0.1)

        return {
            "message": "Electronics document opened successfully" if len(opened_documents) == 1 else "Electronics documents opened successfully",
            "requested_name": name,
            "target": target,
            "opened_documents": [get_document_summary(document) for document in opened_documents],
            "activated_document": get_document_summary(activated_document) if activated_document else None,
            "errors": errors,
        }
    except Exception as exc:
        return error_payload("Error opening electronics document", exc)


def activate_electronics_document_impl(name, target="schematic"):
    try:
        if is_blank(name):
            raise ValueError("name is required")

        requested_extensions = resolve_electronics_target_extensions(target)
        document = find_open_document(name, requested_extensions)
        if not document:
            return open_electronics_document_impl(name, target, True, False)

        document.activate()
        settle_fusion_processing(cycles=12, delay_seconds=0.1)

        return {
            "message": "Electronics document activated successfully",
            "requested_name": name,
            "target": target,
            "document": get_document_summary(document),
        }
    except Exception as exc:
        return error_payload("Error activating electronics document", exc)


def upload_electronics_project_impl(schematic_path="", board_path="", library_paths=None, open_documents=True, activate_target="schematic"):
    try:
        library_paths = library_paths or []
        input_paths = []

        if not is_blank(schematic_path):
            input_paths.append(Path(str(schematic_path)).expanduser())
        if not is_blank(board_path):
            input_paths.append(Path(str(board_path)).expanduser())

        normalized_library_paths = []
        for library_path in library_paths:
            if is_blank(library_path):
                continue
            normalized_library_paths.append(Path(str(library_path)).expanduser())

        if not input_paths and not normalized_library_paths:
            raise ValueError("At least one schematic_path, board_path, or library_paths entry is required")

        for path in input_paths + normalized_library_paths:
            if not path.exists():
                raise FileNotFoundError(f"Path does not exist: {path}")

        folder = get_active_data_folder()
        if not folder:
            raise RuntimeError("No active Fusion data folder available")

        project_base_name = ""
        if input_paths:
            project_base_name = input_paths[0].stem
        elif normalized_library_paths:
            project_base_name = normalized_library_paths[0].stem

        upload_state_history = []
        uploaded_files = []

        if len(input_paths) > 1:
            future = folder.uploadAssembly([str(path) for path in input_paths])
            if not future:
                raise RuntimeError("uploadAssembly returned null")

            state_names = {
                getattr(adsk.core.UploadStates, "UploadProcessing", None): "UploadProcessing",
                getattr(adsk.core.UploadStates, "UploadFinished", None): "UploadFinished",
                getattr(adsk.core.UploadStates, "UploadFailed", None): "UploadFailed",
            }

            for _ in range(480):
                adsk.doEvents()
                time.sleep(0.25)
                upload_state = future.uploadState
                state_name = state_names.get(upload_state, str(upload_state))
                if not upload_state_history or upload_state_history[-1] != state_name:
                    upload_state_history.append(state_name)
                if upload_state == adsk.core.UploadStates.UploadFinished:
                    break
                if upload_state == adsk.core.UploadStates.UploadFailed:
                    raise RuntimeError("uploadAssembly failed")
            else:
                raise RuntimeError("uploadAssembly timed out")
        elif input_paths:
            future = folder.uploadFile(str(input_paths[0]))
            if not future:
                raise RuntimeError("uploadFile returned null")

            state_names = {
                getattr(adsk.core.UploadStates, "UploadProcessing", None): "UploadProcessing",
                getattr(adsk.core.UploadStates, "UploadFinished", None): "UploadFinished",
                getattr(adsk.core.UploadStates, "UploadFailed", None): "UploadFailed",
            }

            for _ in range(480):
                adsk.doEvents()
                time.sleep(0.25)
                upload_state = future.uploadState
                state_name = state_names.get(upload_state, str(upload_state))
                if not upload_state_history or upload_state_history[-1] != state_name:
                    upload_state_history.append(state_name)
                if upload_state == adsk.core.UploadStates.UploadFinished:
                    break
                if upload_state == adsk.core.UploadStates.UploadFailed:
                    raise RuntimeError("uploadFile failed")
            else:
                raise RuntimeError("uploadFile timed out")

        for library_path in normalized_library_paths:
            future = folder.uploadFile(str(library_path))
            if not future:
                raise RuntimeError(f"uploadFile returned null for library: {library_path}")

            for _ in range(480):
                adsk.doEvents()
                time.sleep(0.25)
                if future.uploadState == adsk.core.UploadStates.UploadFinished:
                    break
                if future.uploadState == adsk.core.UploadStates.UploadFailed:
                    raise RuntimeError(f"Library upload failed: {library_path}")
            else:
                raise RuntimeError(f"Library upload timed out: {library_path}")

        settle_fusion_processing(cycles=12, delay_seconds=0.1)

        for data_file in get_matching_data_files(project_base_name):
            extension = get_data_file_extension(data_file)
            if extension in ("fsch", "fbrd", "fprj", "flbr", "sch", "brd", "lbr", "prj"):
                uploaded_files.append(get_data_file_summary(data_file))

        open_result = None
        if open_documents and not is_blank(project_base_name):
            open_target = activate_target or "schematic"
            open_related = bool(not is_blank(board_path) or len(input_paths) > 1)
            open_result = open_electronics_document_impl(project_base_name, open_target, True, open_related)

        return {
            "message": "Electronics project uploaded successfully",
            "project_name": project_base_name,
            "active_folder_name": safe_get_name(folder),
            "upload_state_history": upload_state_history,
            "uploaded_files": uploaded_files,
            "open_result": open_result,
        }
    except Exception as exc:
        return error_payload("Error uploading electronics project", exc)


def export_electronics_file_impl(format, filename="", target="active"):
    try:
        format_key = normalize_key(format)
        if format_key in ("sch", "eagle_sch", "schematic"):
            schematic = resolve_electronics_target_document("schematic")
            output_path = resolve_output_path(filename, f"{safe_get_name(schematic)}_schematic", ".sch")
            export_options = schematic.exportManager.createEagleSchExportOptions(str(output_path))
            success = schematic.exportManager.execute(export_options)
            return {"message": "Electronics schematic export completed" if success else "Electronics schematic export failed", "success": success, "format": "sch", "path": str(output_path)}

        if format_key in ("brd", "eagle_brd", "board", "pcb"):
            board = resolve_electronics_target_document("board")
            output_path = resolve_output_path(filename, f"{safe_get_name(board)}_board", ".brd")
            export_options = board.exportManager.createEagleBrdExportOptions(str(output_path))
            success = board.exportManager.execute(export_options)
            return {"message": "Electronics board export completed" if success else "Electronics board export failed", "success": success, "format": "brd", "path": str(output_path)}

        if format_key in ("lbr", "eagle_lbr", "library"):
            library = resolve_electronics_target_document("library")
            output_path = resolve_output_path(filename, f"{safe_get_name(library)}_library", ".lbr")
            export_options = library.exportManager.createEagleLbrExportOptions(str(output_path))
            success = library.exportManager.execute(export_options)
            return {"message": "Electronics library export completed" if success else "Electronics library export failed", "success": success, "format": "lbr", "path": str(output_path)}

        raise ValueError(f"Unsupported electronics export format: {format}")
    except Exception as exc:
        return error_payload("Error exporting electronics file", exc)


def execute_text_command_impl(command):
    try:
        if is_blank(command):
            raise ValueError("command is required")
        result = app.executeTextCommand(str(command))
        return {
            "message": "Text command executed",
            "command": command,
            "result": result,
        }
    except Exception as exc:
        return error_payload("Error executing text command", exc)


def execute_electronics_api_impl(script="", expression="", input_data=None, result_variable="result"):
    try:
        context = get_active_products_context()
        if not context["ecad_document"] and not context["schematic"] and not context["board"] and not context["library"] and not context["ecad_design"]:
            raise RuntimeError("Active document is not an electronics document")
    except Exception as exc:
        return error_payload("execute_electronics_api failed", exc)

    return execute_fusion_api_impl(script, expression, input_data, result_variable)


def create_sketch_prompt_impl(description):
    return {
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an expert in Fusion 360 CAD modeling. Help the user turn the description "
                    "into a sketch plan with planes, entities, dimensions, and constraints."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"I want to create a sketch with these requirements: {description}\n\n"
                    "Please provide step-by-step instructions for creating this sketch in Fusion 360."
                ),
            },
        ]
    }


def parameter_setup_prompt_impl(description):
    return {
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an expert in Fusion 360 parametric design. Suggest a clean parameter set "
                    "with values, units, and short explanations."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"I want to set up parameters for: {description}\n\n"
                    "What parameters should I create, and what values, units, and comments should they have?"
                ),
            },
        ]
    }


def feature_strategy_prompt_impl(description):
    return {
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an expert Fusion 360 modeling planner. Break the user's part down into a "
                    "practical sequence of components, construction geometry, sketches, constraints, and features."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"I want to model this in Fusion 360: {description}\n\n"
                    "Please propose a robust modeling strategy with sketch and feature order."
                ),
            },
        ]
    }

# Function to check if MCP package is installed
def check_mcp_installed():
    missing_packages = []
    
    try:
        import mcp
        print(f"Found MCP package at: {mcp.__file__}")
    except ImportError as e:
        print(f"Error importing MCP package: {str(e)}")
        missing_packages.append("mcp[cli]")
    
    try:
        import uvicorn
        print(f"Found uvicorn package at: {uvicorn.__file__}")
    except ImportError as e:
        print(f"Error importing uvicorn package: {str(e)}")
        missing_packages.append("uvicorn")
    
    if missing_packages:
        print(f"Missing required packages: {', '.join(missing_packages)}")
        return False
    
    return True

# Function to run MCP server
def run_mcp_server():
    try:
        # Import required MCP modules
        import mcp
        from mcp.server.fastmcp import FastMCP
        import uvicorn
        import threading
        
        # Also, directly test showing a message box when the server starts
        def test_direct_message():
            try:
                test_message = "MCP Server startup test message"
                debug_path = comm_file("startup_test_message.txt")
                append_text(debug_path, f"Trying command-based test message at server startup: {time.ctime()}\n")
                
                # Try to show the message box using command-based approach
                run_in_fusion_main_thread(lambda: create_message_box_command(test_message))

                append_text(debug_path, f"Command-based test message triggered at {time.ctime()}\n")
            except Exception as e:
                append_text(debug_path, f"Command-based test message failed: {str(e)} at {time.ctime()}\n")
        
        # Schedule the test message using threading.Timer
        test_timer = threading.Timer(3.0, test_direct_message)
        test_timer.daemon = True
        test_timer.start()
        
        # Create communication directories and diagnostics in a repo-local location when available.
        workspace_comm_dir = primary_comm_dir()
        
        # Write diagnostic info without relying on __version__
        diagnostic_log = workspace_comm_dir / "mcp_server_diagnostics.log"
        with open(diagnostic_log, "w", encoding="utf-8") as f:
            f.write(f"MCP Server Diagnostics - {time.ctime()}\n\n")
            f.write(f"Server URL: {SERVER_URL}\n")
            f.write(f"Workspace directory: {REPO_ROOT if REPO_ROOT else ADDIN_DIR}\n")
            f.write(f"Communication directory: {workspace_comm_dir}\n\n")
            f.write(f"Python version: {sys.version}\n\n")
            
            # Get MCP version safely if available
            try:
                mcp_version = getattr(mcp, "__version__", "Unknown")
                f.write(f"MCP Version: {mcp_version}\n\n")
            except:
                f.write("MCP Version: Unable to determine\n\n")
            
            f.write(f"Registered Resources:\n  (Method available_resources() not available in this MCP SDK version)\n\n")
            f.write(f"Registered Tools:\n  (Method available_tools() not available in this MCP SDK version)\n\n")
            f.write(f"Registered Prompts:\n  (Method available_prompts() not available in this MCP SDK version)\n\n")
            f.write(f"Environment:\n  Python version: {sys.version}\n  MCP SDK available: True\n\n")
        
        print("Creating FastMCP server instance...")
        # Create the MCP server
        fusion_mcp = FastMCP("Fusion 360 MCP Server")
        
        # Write more diagnostics about the FastMCP object
        with open(diagnostic_log, "a", encoding="utf-8") as f:
            f.write(f"FastMCP Object Attributes:\n")
            for attr in dir(fusion_mcp):
                if not attr.startswith('_'):
                    f.write(f"  - {attr}\n")
            f.write("\n")
        
        print("Registering resources...")

        def execute_on_fusion_thread(callback):
            return call_fusion_api(callback)

        @fusion_mcp.resource("fusion://active-document-info")
        def get_active_document_info():
            """Get information about the active document in Fusion 360."""
            return execute_on_fusion_thread(get_active_document_info_resource)

        @fusion_mcp.resource("fusion://design-structure")
        def get_design_structure():
            """Get the structure of the active design in Fusion 360."""
            return execute_on_fusion_thread(get_design_structure_resource)

        @fusion_mcp.resource("fusion://parameters")
        def get_parameters():
            """Get the parameters of the active design in Fusion 360."""
            return execute_on_fusion_thread(get_parameters_resource)

        @fusion_mcp.resource("fusion://components")
        def get_components():
            """Get the components in the active design."""
            return execute_on_fusion_thread(get_components_resource)

        @fusion_mcp.resource("fusion://sketches")
        def get_sketches():
            """Get the sketches in the active design."""
            return execute_on_fusion_thread(get_sketches_resource)

        @fusion_mcp.resource("fusion://bodies")
        def get_bodies():
            """Get the bodies in the active design."""
            return execute_on_fusion_thread(get_bodies_resource)

        @fusion_mcp.resource("fusion://electronics-context")
        def get_electronics_context():
            """Get the active Fusion Electronics context, including schematic, board, and library state."""
            return execute_on_fusion_thread(get_electronics_context_resource)

        @fusion_mcp.resource("fusion://electronics-schematic")
        def get_electronics_schematic():
            """Get the active Fusion Electronics schematic state."""
            return execute_on_fusion_thread(get_electronics_schematic_resource)

        @fusion_mcp.resource("fusion://electronics-board")
        def get_electronics_board():
            """Get the active Fusion Electronics board state."""
            return execute_on_fusion_thread(get_electronics_board_resource)

        @fusion_mcp.resource("fusion://electronics-library")
        def get_electronics_library():
            """Get the active Fusion Electronics library state."""
            return execute_on_fusion_thread(get_electronics_library_resource)

        @fusion_mcp.resource("fusion://electronics-libraries")
        def get_electronics_libraries():
            """List the libraries referenced by the active electronics document."""
            return execute_on_fusion_thread(get_electronics_libraries_resource)

        @fusion_mcp.resource("fusion://electronics-documents")
        def get_electronics_documents():
            """List open and available electronics documents and data files."""
            return execute_on_fusion_thread(get_electronics_documents_resource)

        @fusion_mcp.resource("fusion://electronics-errors")
        def get_electronics_errors():
            """Get ERC and DRC errors from the active electronics schematic and board."""
            return execute_on_fusion_thread(get_electronics_errors_resource)

        @fusion_mcp.resource("fusion://mcp-capabilities")
        def get_mcp_capabilities():
            """Describe the fixed MCP surface and the generic Fusion API bridge."""
            return execute_on_fusion_thread(get_mcp_capabilities_resource)

        print("Registering tools...")

        @fusion_mcp.tool()
        def message_box(message: str) -> str:
            """Display a message box in Fusion 360."""
            return execute_on_fusion_thread(lambda: message_box_impl(message))

        @fusion_mcp.tool()
        def create_new_sketch(plane_name: str, component_name: str = "", sketch_name: str = "") -> str:
            """Create a new sketch on the specified plane."""
            return execute_on_fusion_thread(lambda: create_new_sketch_impl(plane_name, component_name, sketch_name))

        @fusion_mcp.tool()
        def create_parameter(name: str, expression: str, unit: str, comment: str = "") -> str:
            """Create or update a parameter in the active design."""
            return execute_on_fusion_thread(lambda: create_parameter_impl(name, expression, unit, comment))

        @fusion_mcp.tool()
        def create_component(name: str, reuse_existing: bool = True) -> dict:
            """Create a new component in the active design."""
            return execute_on_fusion_thread(lambda: create_component_impl(name, reuse_existing))

        @fusion_mcp.tool()
        def create_offset_plane(base_plane_name: str, offset: float | str, component_name: str = "", plane_name: str = "") -> dict:
            """Create a construction plane offset from an existing plane."""
            return execute_on_fusion_thread(lambda: create_offset_plane_impl(base_plane_name, offset, component_name, plane_name))

        @fusion_mcp.tool()
        def list_sketch_entities(sketch_name: str, component_name: str = "") -> dict:
            """List sketch points and curves, including entity tokens."""
            return execute_on_fusion_thread(lambda: list_sketch_entities_impl(sketch_name, component_name))

        @fusion_mcp.tool()
        def list_sketch_profiles(sketch_name: str, component_name: str = "") -> dict:
            """List sketch profiles for a sketch."""
            return execute_on_fusion_thread(lambda: list_sketch_profiles_impl(sketch_name, component_name))

        @fusion_mcp.tool()
        def create_sketch_point(sketch_name: str, x: float | str, y: float | str, z: float | str = 0.0, component_name: str = "") -> dict:
            """Create a sketch point using sketch-space coordinates."""
            return execute_on_fusion_thread(lambda: create_sketch_point_impl(sketch_name, x, y, z, component_name))

        @fusion_mcp.tool()
        def create_sketch_line(sketch_name: str, start_x: float | str, start_y: float | str, end_x: float | str, end_y: float | str, start_z: float | str = 0.0, end_z: float | str = 0.0, component_name: str = "") -> dict:
            """Create a sketch line between two sketch-space points."""
            return execute_on_fusion_thread(lambda: create_sketch_line_impl(sketch_name, start_x, start_y, end_x, end_y, start_z, end_z, component_name))

        @fusion_mcp.tool()
        def create_sketch_lines(sketch_name: str, points: list[dict], component_name: str = "") -> dict:
            """Create a polyline from multiple sketch-space points."""
            return execute_on_fusion_thread(lambda: create_sketch_lines_impl(sketch_name, points, component_name))

        @fusion_mcp.tool()
        def create_sketch_circle(sketch_name: str, center_x: float | str, center_y: float | str, radius: float | str, center_z: float | str = 0.0, component_name: str = "") -> dict:
            """Create a sketch circle from a center point and radius."""
            return execute_on_fusion_thread(lambda: create_sketch_circle_impl(sketch_name, center_x, center_y, radius, center_z, component_name))

        @fusion_mcp.tool()
        def create_sketch_rectangle(sketch_name: str, x1: float | str, y1: float | str, x2: float | str, y2: float | str, z1: float | str = 0.0, z2: float | str = 0.0, component_name: str = "") -> dict:
            """Create a two-point sketch rectangle."""
            return execute_on_fusion_thread(lambda: create_sketch_rectangle_impl(sketch_name, x1, y1, x2, y2, z1, z2, component_name))

        @fusion_mcp.tool()
        def create_sketch_center_rectangle(sketch_name: str, center_x: float | str, center_y: float | str, corner_x: float | str, corner_y: float | str, center_z: float | str = 0.0, corner_z: float | str = 0.0, component_name: str = "") -> dict:
            """Create a center-point sketch rectangle."""
            return execute_on_fusion_thread(lambda: create_sketch_center_rectangle_impl(sketch_name, center_x, center_y, corner_x, corner_y, center_z, corner_z, component_name))

        @fusion_mcp.tool()
        def create_sketch_arc(sketch_name: str, center_x: float | str, center_y: float | str, start_x: float | str, start_y: float | str, sweep_angle: float | str, center_z: float | str = 0.0, start_z: float | str = 0.0, component_name: str = "") -> dict:
            """Create a sketch arc from center, start point, and sweep angle."""
            return execute_on_fusion_thread(lambda: create_sketch_arc_impl(sketch_name, center_x, center_y, start_x, start_y, sweep_angle, center_z, start_z, component_name))

        @fusion_mcp.tool()
        def create_sketch_spline(sketch_name: str, points: list[dict], component_name: str = "") -> dict:
            """Create a fitted spline through multiple sketch-space points."""
            return execute_on_fusion_thread(lambda: create_sketch_spline_impl(sketch_name, points, component_name))

        @fusion_mcp.tool()
        def add_sketch_constraint(sketch_name: str, constraint_type: str, entity_one_token: str = "", entity_two_token: str = "", entity_three_token: str = "", component_name: str = "") -> dict:
            """Apply a geometric constraint using sketch entity tokens."""
            return execute_on_fusion_thread(lambda: add_sketch_constraint_impl(sketch_name, constraint_type, entity_one_token, entity_two_token, entity_three_token, component_name))

        @fusion_mcp.tool()
        def add_sketch_dimension(sketch_name: str, dimension_type: str, entity_one_token: str, entity_two_token: str = "", text_x: float | str = 0.0, text_y: float | str = 0.0, text_z: float | str = 0.0, orientation: str = "aligned", expression: str = "", component_name: str = "") -> dict:
            """Add a driving sketch dimension using sketch entity tokens."""
            return execute_on_fusion_thread(lambda: add_sketch_dimension_impl(sketch_name, dimension_type, entity_one_token, entity_two_token, text_x, text_y, text_z, orientation, expression, component_name))

        @fusion_mcp.tool()
        def create_extrude(sketch_name: str = "", distance: float | str = "10 mm", profile_index: int = 0, operation: str = "new_body", component_name: str = "", feature_name: str = "", body_name: str = "", direction: str = "positive", profile_token: str = "") -> dict:
            """Extrude a sketch profile into a 3D feature."""
            return execute_on_fusion_thread(lambda: create_extrude_impl(sketch_name, distance, profile_index, operation, component_name, feature_name, body_name, direction, profile_token))

        @fusion_mcp.tool()
        def create_revolve(sketch_name: str = "", axis_token: str = "", angle: float | str = "360 deg", profile_index: int = 0, operation: str = "new_body", component_name: str = "", feature_name: str = "", body_name: str = "", profile_token: str = "", axis_name: str = "") -> dict:
            """Revolve a sketch profile around an axis entity token."""
            return execute_on_fusion_thread(lambda: create_revolve_impl(sketch_name, axis_token, angle, profile_index, operation, component_name, feature_name, body_name, profile_token, axis_name))

        @fusion_mcp.tool()
        def delete_body(body_name: str, component_name: str = "", allow_partial_match: bool = False, delete_all_matches: bool = False) -> dict:
            """Delete one or more bodies from the active design."""
            return execute_on_fusion_thread(lambda: delete_body_impl(body_name, component_name, allow_partial_match, delete_all_matches))

        @fusion_mcp.tool()
        def export_sketch_dxf(sketch_name: str, filename: str = "", component_name: str = "") -> dict:
            """Export a sketch to a DXF file."""
            return execute_on_fusion_thread(lambda: export_sketch_dxf_impl(sketch_name, filename, component_name))

        @fusion_mcp.tool()
        def export_design_file(format: str, filename: str = "", component_name: str = "", body_name: str = "") -> dict:
            """Export the active design to STEP, IGES, SAT, STL, 3MF, or OBJ."""
            return execute_on_fusion_thread(lambda: export_design_file_impl(format, filename, component_name, body_name))

        @fusion_mcp.tool()
        def export_active_drawing_pdf(filename: str = "") -> dict:
            """Export the active drawing document to PDF."""
            return execute_on_fusion_thread(lambda: export_active_drawing_pdf_impl(filename))

        @fusion_mcp.tool()
        def create_electronics_sheet(name: str = "") -> dict:
            """Create a new sheet in the active electronics schematic."""
            return execute_on_fusion_thread(lambda: create_electronics_sheet_impl(name))

        @fusion_mcp.tool()
        def begin_electronics_change(change_id: str = "", target: str = "active") -> dict:
            """Begin an electronics design-change transaction."""
            return execute_on_fusion_thread(lambda: begin_electronics_change_impl(change_id, target))

        @fusion_mcp.tool()
        def end_electronics_change(target: str = "active") -> dict:
            """Commit the current electronics design-change transaction."""
            return execute_on_fusion_thread(lambda: end_electronics_change_impl(target))

        @fusion_mcp.tool()
        def cancel_electronics_change(target: str = "active") -> dict:
            """Cancel the current electronics design-change transaction."""
            return execute_on_fusion_thread(lambda: cancel_electronics_change_impl(target))

        @fusion_mcp.tool()
        def list_electronics_documents() -> dict:
            """List open and available electronics schematic, board, project, and library documents."""
            return execute_on_fusion_thread(list_electronics_documents_impl)

        @fusion_mcp.tool()
        def upload_electronics_project(schematic_path: str = "", board_path: str = "", library_paths: list[str] = None, open_documents: bool = True, activate_target: str = "schematic") -> dict:
            """Upload schematic, board, and library files into Fusion and optionally open them."""
            return execute_on_fusion_thread(lambda: upload_electronics_project_impl(schematic_path, board_path, library_paths, open_documents, activate_target))

        @fusion_mcp.tool()
        def open_electronics_document(name: str, target: str = "schematic", activate: bool = True, open_related: bool = False) -> dict:
            """Open an uploaded electronics schematic, board, project, or library by name."""
            return execute_on_fusion_thread(lambda: open_electronics_document_impl(name, target, activate, open_related))

        @fusion_mcp.tool()
        def activate_electronics_document(name: str, target: str = "schematic") -> dict:
            """Activate an already open electronics schematic, board, project, or library by name."""
            return execute_on_fusion_thread(lambda: activate_electronics_document_impl(name, target))

        @fusion_mcp.tool()
        def export_electronics_file(format: str, filename: str = "", target: str = "active") -> dict:
            """Export the active electronics design as EAGLE SCH, BRD, or LBR."""
            return execute_on_fusion_thread(lambda: export_electronics_file_impl(format, filename, target))

        @fusion_mcp.tool()
        def execute_text_command(command: str) -> dict:
            """Run a Fusion text command directly through the active application."""
            return execute_on_fusion_thread(lambda: execute_text_command_impl(command))

        @fusion_mcp.tool()
        def inspect_fusion_object(path: str = "root_component", include_private: bool = False, max_members: int = 200, include_values: bool = True) -> dict:
            """Inspect any live Fusion API object by Python path and list its members."""
            return execute_on_fusion_thread(lambda: inspect_fusion_object_impl(path, include_private, max_members, include_values))

        @fusion_mcp.tool()
        def execute_fusion_api(script: str = "", expression: str = "", input_data: dict = None, result_variable: str = "result") -> dict:
            """Execute arbitrary Python against the live Fusion API with design, drawing, and CAM context."""
            return execute_on_fusion_thread(lambda: execute_fusion_api_impl(script, expression, input_data, result_variable))

        @fusion_mcp.tool()
        def execute_electronics_api(script: str = "", expression: str = "", input_data: dict = None, result_variable: str = "result") -> dict:
            """Execute arbitrary Python against the live Fusion Electronics API with schematic, board, library, and design context."""
            return execute_on_fusion_thread(lambda: execute_electronics_api_impl(script, expression, input_data, result_variable))

        print("Registering prompts...")

        @fusion_mcp.prompt()
        def create_sketch_prompt(description: str) -> dict:
            """Create a prompt for creating a sketch based on a description."""
            return execute_on_fusion_thread(lambda: create_sketch_prompt_impl(description))

        @fusion_mcp.prompt()
        def parameter_setup_prompt(description: str) -> dict:
            """Create a prompt for setting up parameters based on a description."""
            return execute_on_fusion_thread(lambda: parameter_setup_prompt_impl(description))

        @fusion_mcp.prompt()
        def feature_strategy_prompt(description: str) -> dict:
            """Create a prompt for planning sketches and features for a Fusion model."""
            return execute_on_fusion_thread(lambda: feature_strategy_prompt_impl(description))

        resource_dispatch = {
            "fusion://active-document-info": get_active_document_info,
            "fusion://design-structure": get_design_structure,
            "fusion://parameters": get_parameters,
            "fusion://components": get_components,
            "fusion://sketches": get_sketches,
            "fusion://bodies": get_bodies,
            "fusion://electronics-context": get_electronics_context,
            "fusion://electronics-schematic": get_electronics_schematic,
            "fusion://electronics-board": get_electronics_board,
            "fusion://electronics-library": get_electronics_library,
            "fusion://electronics-libraries": get_electronics_libraries,
            "fusion://electronics-documents": get_electronics_documents,
            "fusion://electronics-errors": get_electronics_errors,
            "fusion://mcp-capabilities": get_mcp_capabilities,
        }

        tool_dispatch = {
            "message_box": lambda params: message_box(**params),
            "create_new_sketch": lambda params: create_new_sketch(**params),
            "create_parameter": lambda params: create_parameter(**params),
            "create_component": lambda params: create_component(**params),
            "create_offset_plane": lambda params: create_offset_plane(**params),
            "list_sketch_entities": lambda params: list_sketch_entities(**params),
            "list_sketch_profiles": lambda params: list_sketch_profiles(**params),
            "create_sketch_point": lambda params: create_sketch_point(**params),
            "create_sketch_line": lambda params: create_sketch_line(**params),
            "create_sketch_lines": lambda params: create_sketch_lines(**params),
            "create_sketch_circle": lambda params: create_sketch_circle(**params),
            "create_sketch_rectangle": lambda params: create_sketch_rectangle(**params),
            "create_sketch_center_rectangle": lambda params: create_sketch_center_rectangle(**params),
            "create_sketch_arc": lambda params: create_sketch_arc(**params),
            "create_sketch_spline": lambda params: create_sketch_spline(**params),
            "add_sketch_constraint": lambda params: add_sketch_constraint(**params),
            "add_sketch_dimension": lambda params: add_sketch_dimension(**params),
            "create_extrude": lambda params: create_extrude(**params),
            "create_revolve": lambda params: create_revolve(**params),
            "delete_body": lambda params: delete_body(**params),
            "export_sketch_dxf": lambda params: export_sketch_dxf(**params),
            "export_design_file": lambda params: export_design_file(**params),
            "export_active_drawing_pdf": lambda params: export_active_drawing_pdf(**params),
            "create_electronics_sheet": lambda params: create_electronics_sheet(**params),
            "begin_electronics_change": lambda params: begin_electronics_change(**params),
            "end_electronics_change": lambda params: end_electronics_change(**params),
            "cancel_electronics_change": lambda params: cancel_electronics_change(**params),
            "list_electronics_documents": lambda params: list_electronics_documents(**params),
            "upload_electronics_project": lambda params: upload_electronics_project(**params),
            "open_electronics_document": lambda params: open_electronics_document(**params),
            "activate_electronics_document": lambda params: activate_electronics_document(**params),
            "export_electronics_file": lambda params: export_electronics_file(**params),
            "execute_text_command": lambda params: execute_text_command(**params),
            "inspect_fusion_object": lambda params: inspect_fusion_object(**params),
            "execute_fusion_api": lambda params: execute_fusion_api(**params),
            "execute_electronics_api": lambda params: execute_electronics_api(**params),
        }

        prompt_dispatch = {
            "create_sketch_prompt": lambda args: create_sketch_prompt(**args),
            "parameter_setup_prompt": lambda args: parameter_setup_prompt(**args),
            "feature_strategy_prompt": lambda args: feature_strategy_prompt(**args),
        }
        
        # Set up file-based communication
        print("Setting up file-based communication...")
        
        addon_comm_dir = ensure_dir(ADDIN_COMM_DIR)
        workspace_comm_dir = primary_comm_dir()
        comm_dirs = [str(path) for path in all_comm_dirs()]
        
        # Create server info file
        server_info_file = workspace_comm_dir / "mcp_server_info.txt"
        with open(server_info_file, "w", encoding="utf-8") as f:
            f.write(f"MCP Server started at {time.ctime()}\n")
            f.write(f"Python version: {sys.version}\n")
        
        # Create server status file with JSON structure
        server_status_file = workspace_comm_dir / "server_status.json"
        with open(server_status_file, "w", encoding="utf-8") as f:
            status_data = {
                "status": "running",
                "started_at": time.ctime(),
                "server_url": SERVER_URL,
                "fusion_version": app.version,
                "available_resources": RESOURCE_URIS,
                "available_tools": [tool["name"] for tool in TOOL_METADATA],
                "available_prompts": [prompt["name"] for prompt in PROMPT_METADATA],
            }
            json.dump(status_data, f, indent=2)
        
        # Create all ready files
        for ready_file in ready_file_paths():
            try:
                ensure_dir(ready_file.parent)
                with open(ready_file, "w", encoding="utf-8") as f:
                    f.write(f"MCP Server Ready - {time.ctime()}")
                print(f"Created ready file: {ready_file}")
            except Exception as e:
                print(f"Error creating ready file at {ready_file}: {str(e)}")
        
        # Run the FastMCP server
        print("Starting MCP server using FastMCP with uvicorn")
        
        # Get the Starlette app from the sse_app method
        sse_app = fusion_mcp.sse_app()
        
        # Port and host for the server
        host = SERVER_HOST
        port = SERVER_PORT
        
        # Create a Config instance for uvicorn
        config = uvicorn.Config(
            sse_app,
            host=host,
            port=port,
            log_level="info"
        )
        
        # Create server instance
        server = uvicorn.Server(config)
        
        # Run server in a separate thread
        def uvicorn_thread():
            try:
                # Create initialization log
                init_log_file = workspace_comm_dir / "mcp_server_init.log"
                with open(init_log_file, "w", encoding="utf-8") as f:
                    f.write(f"Starting uvicorn server at {time.ctime()}\n")
                    f.write(f"Host: {host}, Port: {port}\n")
                
                # Run the server
                server.run()
            except Exception as e:
                error_msg = f"Error in uvicorn server: {str(e)}"
                print(error_msg)
                
                # Write error to file
                error_file = workspace_comm_dir / "mcp_server_uvicorn_error.txt"
                with open(error_file, "w", encoding="utf-8") as f:
                    f.write(error_msg + "\n")
                    f.write(traceback.format_exc())
        
        # Start the server in a thread
        uvicorn_thread = threading.Thread(target=uvicorn_thread)
        uvicorn_thread.daemon = True
        uvicorn_thread.start()
        
        print(f"MCP server started at http://{host}:{port}/sse")
        
        # Monitor for command files in a separate thread
        def file_monitor_thread():
            try:
                print("Starting file monitor thread...")
                
                # Create a file to track thread status
                monitor_file = workspace_comm_dir / "file_monitor_status.txt"
                with open(monitor_file, "w", encoding="utf-8") as f:
                    f.write(f"File monitor thread started at {time.ctime()}\n")
                
                while server_running:
                    # Check each communication directory for command files
                    for comm_dir in comm_dirs:
                        try:
                            # Create directory if it doesn't exist
                            os.makedirs(comm_dir, exist_ok=True)
                            
                            # Check for message box files
                            message_file = os.path.join(comm_dir, "message_box.txt")
                            if os.path.exists(message_file):
                                try:
                                    # Create debug logs for every step
                                    debug_file = workspace_comm_dir / "message_box_processing.txt"
                                    with open(debug_file, "a", encoding="utf-8") as f:
                                        f.write(f"\n--- Found message_box.txt at {time.ctime()} ---\n")
                                    
                                    # Read the message
                                    with open(message_file, "r") as f:
                                        message = f.read().strip()
                                    
                                    # Log the message content
                                    with open(debug_file, "a", encoding="utf-8") as f:
                                        f.write(f"Message content: {message}\n")
                                    
                                    # Queue the message for display
                                    print(f"Displaying message box: {message}")
                                    
                                    # Log that we queued the message
                                    with open(debug_file, "a", encoding="utf-8") as f:
                                        f.write(f"Message being processed via command approach\n")
                                    
                                    # Try to display the message directly as well
                                    try:
                                        # Use command-based approach for the most reliable display
                                        run_in_fusion_main_thread(lambda: create_message_box_command(message))
                                        with open(debug_file, "a", encoding="utf-8") as f:
                                            f.write(f"Command-based display triggered\n")
                                    except Exception as e:
                                        with open(debug_file, "a", encoding="utf-8") as f:
                                            f.write(f"Command-based display attempt failed: {str(e)}\n")
                                    
                                    # Rename the file to avoid processing it again
                                    processed_file = os.path.join(comm_dir, f"processed_message_{int(time.time())}.txt")
                                    with open(debug_file, "a", encoding="utf-8") as f:
                                        f.write(f"Renaming file to: {processed_file}\n")
                                    
                                    os.rename(message_file, processed_file)
                                    
                                    with open(debug_file, "a", encoding="utf-8") as f:
                                        f.write(f"File renamed successfully\n")
                                        
                                except Exception as e:
                                    print(f"Error processing message file {message_file}: {str(e)}")
                                    
                                    # Log the error
                                    try:
                                        with open(debug_file, "a") as f:
                                            f.write(f"ERROR processing message file: {str(e)}\n")
                                            f.write(traceback.format_exc())
                                    except:
                                        pass
                            
                            # Check for command files
                            for file in os.listdir(comm_dir):
                                if file.startswith("command_") and file.endswith(".json"):
                                    command_file = os.path.join(comm_dir, file)
                                    try:
                                        # Extract the command ID from the filename
                                        command_id = file[len("command_"):-len(".json")]
                                        
                                        # Check if we've already processed this command
                                        processed_file = os.path.join(comm_dir, f"processed_command_{command_id}.json")
                                        response_file = os.path.join(comm_dir, f"response_{command_id}.json")
                                        
                                        if os.path.exists(processed_file) or os.path.exists(response_file):
                                            continue  # Skip if already processed
                                        
                                        print(f"Processing command file: {command_file}")
                                        
                                        # Read command data
                                        try:
                                            with open(command_file, "r", encoding="utf-8") as f:
                                                command_data = json.load(f)
                                            
                                            command = command_data.get("command")
                                            params = command_data.get("params", {}) or {}
                                            
                                            print(f"Processing command {command_id}: {command} with params {params}")
                                            
                                            result = None
                                            
                                            if command == "list_resources":
                                                result = RESOURCE_URIS
                                            elif command == "list_tools":
                                                result = TOOL_METADATA
                                            elif command == "list_prompts":
                                                result = PROMPT_METADATA
                                            elif command == "read_resource":
                                                uri = params.get("uri", "")
                                                resource_reader = resource_dispatch.get(uri)
                                                if resource_reader:
                                                    result = resource_reader()
                                                else:
                                                    result = {"error": f"Unknown resource URI: {uri}"}
                                            elif command == "get_prompt":
                                                prompt_name = params.get("name", "")
                                                prompt_args = params.get("args", {})
                                                prompt_reader = prompt_dispatch.get(prompt_name)
                                                if prompt_reader:
                                                    result = prompt_reader(prompt_args)
                                                else:
                                                    result = {"error": f"Unknown prompt: {prompt_name}"}
                                            else:
                                                tool_runner = tool_dispatch.get(command)
                                                if tool_runner:
                                                    result = tool_runner(params)
                                                else:
                                                    result = {"error": f"Unknown command: {command}"}
                                            
                                            with open(response_file, "w", encoding="utf-8") as f:
                                                json.dump({"result": result}, f, indent=2)
                                            
                                            # Rename the command file to avoid processing it again
                                            os.rename(command_file, processed_file)
                                        except json.JSONDecodeError as e:
                                            # Handle JSON parsing error
                                            print(f"Error parsing JSON in {command_file}: {str(e)}")
                                            with open(response_file, "w", encoding="utf-8") as f:
                                                json.dump({"error": f"Invalid JSON format: {str(e)}"}, f, indent=2)
                                    except Exception as e:
                                        print(f"Error processing command file {command_file}: {str(e)}")
                                        traceback.print_exc()
                                        
                                        # Try to create an error response anyway
                                        try:
                                            with open(os.path.join(comm_dir, f"response_{command_id}.json"), "w", encoding="utf-8") as f:
                                                json.dump({"error": str(e)}, f, indent=2)
                                        except Exception:
                                            pass
                        except Exception as e:
                            print(f"Error processing directory {comm_dir}: {str(e)}")
                            error_file = workspace_comm_dir / "error.txt"
                            with open(error_file, "w", encoding="utf-8") as f:
                                f.write(f"Error in file monitor for directory {comm_dir}: {str(e)}\n\n{traceback.format_exc()}")
                    
                    # Sleep to avoid high CPU usage
                    time.sleep(0.5)
            except Exception as e:
                print(f"Error in file monitor thread: {str(e)}")
                error_file = workspace_comm_dir / "error.txt"
                with open(error_file, "w", encoding="utf-8") as f:
                    f.write(f"File Monitor Error: {str(e)}\n\n{traceback.format_exc()}")
        
        # Start the file monitor thread
        file_monitor = threading.Thread(target=file_monitor_thread)
        file_monitor.daemon = True
        file_monitor.start()
        
        # Keep thread running
        while server_running:
            time.sleep(1)
            
        # Shutdown the server
        print("Shutting down server...")
        server.should_exit = True
        
        return True
        
    except Exception as e:
        print(f"Error in MCP server: {str(e)}")
        
        # Create error file
        error_file = comm_file("mcp_server_error.txt")
        with open(error_file, "w", encoding="utf-8") as f:
            f.write(f"MCP Server Error: {str(e)}\n\n{traceback.format_exc()}")
        
        return False

# Function to start the server
def start_server():
    global server_thread
    global server_running
    
    print("Starting MCP server...")
    capture_fusion_main_thread()
    ensure_main_thread_event_registered()
    
    # Create workspace comm directory if it doesn't exist
    workspace_comm_dir = primary_comm_dir()
    log_file = workspace_comm_dir / "mcp_server_log.txt"
    with open(log_file, "w", encoding="utf-8") as f:
        f.write(f"MCP Server starting at {time.ctime()}\n")
    
    # Check if MCP is installed
    if not check_mcp_installed():
        print("Required packages not installed. Cannot start server.")
        ui.messageBox("Required packages are not installed. Please install them with:\npip install \"mcp[cli]\" uvicorn")
        return False
    
    # Check if server is already running
    if server_running and server_thread and server_thread.is_alive():
        print("MCP server is already running")
        return True
    
    # Reset server state
    server_running = True
    
    # Start server in a separate thread
    def server_thread_func():
        global server_running
        try:
            success = run_mcp_server()
            if not success:
                print("Failed to start MCP server")
                server_running = False
                ui.messageBox("Failed to start MCP server. See error log for details.")
        except Exception as e:
            print(f"Error in server thread: {str(e)}")
            server_running = False
            error_file = workspace_comm_dir / "mcp_server_error.txt"
            with open(error_file, "w", encoding="utf-8") as f:
                f.write(f"MCP Server Thread Error: {str(e)}\n\n{traceback.format_exc()}")
    
    server_thread = threading.Thread(target=server_thread_func)
    server_thread.daemon = True
    server_thread.start()
    
    print("MCP server thread started")
    
    # Wait a moment for the server to initialize
    time.sleep(1)
    
    # Check if the thread is still alive
    if not server_thread.is_alive():
        print("MCP server thread stopped unexpectedly")
        server_running = False
        return False
    
    print("MCP server started successfully")
    return True

# Function to stop the server
def stop_server():
    global server_running
    
    if not server_running:
        print("MCP server is not running")
        unregister_main_thread_event()
        return
    
    # Set server running flag to stop the server loop
    server_running = False
    
    # Wait for the thread to finish
    if server_thread and server_thread.is_alive():
        server_thread.join(timeout=2.0)
    
    unregister_main_thread_event()
    print("MCP server stopped")

# Command event handlers
class MCPServerCommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self):
        super().__init__()
    
    def notify(self, args):
        try:
            # Get command inputs
            cmd = args.command
            inputs = cmd.commandInputs
            
            # Add information text
            info_input = inputs.addTextBoxCommandInput('infoInput', '', 
                'Click OK to start the MCP Server.\n\n' +
                'This will enable communication between Fusion 360 and MCP clients.\n\n' +
                'Current server status: ' + ('Running' if server_running else 'Not Running'), 
                4, True)
            
            # Events
            onExecute = MCPServerCommandExecuteHandler()
            cmd.execute.add(onExecute)
            handlers.append(onExecute)
            
            onDestroy = MCPServerCommandDestroyHandler()
            cmd.destroy.add(onDestroy)
            handlers.append(onDestroy)
        except:
            if ui:
                ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))

class MCPServerCommandExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self):
        super().__init__()
    
    def notify(self, args):
        try:
            # Start the server
            success = start_server()
            
            # Try to show a test message directly for debugging
            debug_path = comm_file("execute_debug.txt")
            with open(debug_path, "a", encoding="utf-8") as f:
                f.write(f"Execute handler called at {time.ctime()}\n")
                f.write(f"Trying command-based test message\n")
            
            try:
                create_message_box_command("MCP Server started - Test Message")
                with open(debug_path, "a", encoding="utf-8") as f:
                    f.write(f"Command-based test message triggered at {time.ctime()}\n")
            except Exception as e:
                with open(debug_path, "a", encoding="utf-8") as f:
                    f.write(f"Command-based test message failed: {str(e)} at {time.ctime()}\n")
            
            if success:
                workspace_comm_dir = primary_comm_dir()
                
                # Create a startup log file
                startup_log_file = workspace_comm_dir / "mcp_server_startup_log.txt"
                with open(startup_log_file, "w", encoding="utf-8") as f:
                    f.write(f"MCP Server started successfully at {time.ctime()}\n")
                    f.write(f"Server URL: {SERVER_URL}\n")
                    f.write(f"Communication directory: {workspace_comm_dir}\n")
                
                ui.messageBox(f"MCP Server started successfully!\n\nServer is running at {SERVER_URL}\n\nReady for client connections.")
            else:
                workspace_comm_dir = primary_comm_dir()
                
                # Check for error file
                error_file = workspace_comm_dir / "mcp_server_error.txt"
                error_message = "Unknown error. See error log for details."
                
                if os.path.exists(error_file):
                    try:
                        with open(error_file, "r", encoding="utf-8") as f:
                            error_message = f.read()
                    except:
                        pass
                
                ui.messageBox(f"Failed to start MCP Server. Error: {error_message}")
        except:
            if ui:
                ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))

class MCPServerCommandDestroyHandler(adsk.core.CommandEventHandler):
    def __init__(self):
        super().__init__()
    
    def notify(self, args):
        try:
            # Clean up as needed
            pass
        except:
            if ui:
                ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))

# Function to stop server on add-in stop
def stop_server_on_stop(context):
    try:
        global server_running
        
        if server_running:
            print("Stopping MCP server...")
            server_running = False
            
            # Create a shutdown log file
            workspace_comm_dir = primary_comm_dir()
            
            shutdown_log_file = workspace_comm_dir / "mcp_server_shutdown_log.txt"
            with open(shutdown_log_file, "w", encoding="utf-8") as f:
                f.write(f"MCP Server stopped at {time.ctime()}\n")
            
            # Wait for the thread to finish
            if server_thread and server_thread.is_alive():
                server_thread.join(timeout=2.0)
            
            print("MCP server stopped")
        unregister_main_thread_event()
    except:
        if ui:
            ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))

# Function to create the UI elements
def create_ui():
    try:
        # Get the command definitions
        command_definitions = ui.commandDefinitions
        
        # Create a command definition for the MCP server command
        mcp_server_cmd_def = command_definitions.itemById('MCPServerCommand')
        if not mcp_server_cmd_def:
            mcp_server_cmd_def = command_definitions.addButtonDefinition('MCPServerCommand', 'MCP Server', 'Start the MCP Server for Fusion 360')
        
        # Connect to the command created event
        on_command_created = MCPServerCommandCreatedHandler()
        mcp_server_cmd_def.commandCreated.add(on_command_created)
        handlers.append(on_command_created)
        
        # Add to the add-ins panel
        add_ins_panel = ui.allToolbarPanels.itemById('SolidScriptsAddinsPanel')
        control = add_ins_panel.controls.itemById('MCPServerCommand')
        if not control:
            add_ins_panel.controls.addCommand(mcp_server_cmd_def)
        
        print("MCP Server command added to UI")
    except:
        if ui:
            ui.messageBox('Failed to create UI:\n{}'.format(traceback.format_exc()))

# Define the required start() and stop() functions for the add-in system
def start():
    """Called when the add-in is started."""
    try:
        create_ui()
        start_server()
    except:
        if ui:
            ui.messageBox('Failed to initialize add-in:\n{}'.format(traceback.format_exc()))

def stop():
    """Called when the add-in is stopped."""
    try:
        # Stop the server
        stop_server_on_stop(None)
        unregister_main_thread_event()
        
        # Clean up UI
        command_definitions = ui.commandDefinitions
        mcp_server_cmd_def = command_definitions.itemById('MCPServerCommand')
        if mcp_server_cmd_def:
            mcp_server_cmd_def.deleteMe()
        
        # Clean up any panels
        add_ins_panel = ui.allToolbarPanels.itemById('SolidScriptsAddinsPanel')
        control = add_ins_panel.controls.itemById('MCPServerCommand')
        if control:
            control.deleteMe()

        handlers.clear()
        message_command_handlers.clear()
            
        print("MCP Server add-in stopped")
    except:
        if ui:
            ui.messageBox('Failed to clean up add-in:\n{}'.format(traceback.format_exc()))

# Main entry point
def run(context):
    try:
        create_ui()
        start_server()
    except:
        if ui:
            ui.messageBox('Failed to run:\n{}'.format(traceback.format_exc()))

# Function to create a message box command
def create_message_box_command(message):
    try:
        debug_file = comm_file("message_command_debug.txt")
        with open(debug_file, "a", encoding="utf-8") as f:
            f.write(f"\nCreating message box command for: {message} at {time.ctime()}\n")
        
        # Create a unique command ID
        command_id = f"MCPMessageBox_{int(time.time() * 1000)}"
        
        # Get or create the command definition
        cmdDefs = ui.commandDefinitions
        cmdDef = cmdDefs.itemById(command_id)
        if cmdDef:
            cmdDef.deleteMe()
        
        # Create a new command definition
        cmdDef = cmdDefs.addButtonDefinition(
            command_id, 
            "MCP Message Box", 
            f"Display message: {message}", 
            ""  # No resource folder needed
        )
        
        # Connect to the command created event
        onCommandCreated = MessageBoxCommandCreatedHandler(message)
        cmdDef.commandCreated.add(onCommandCreated)
        message_command_handlers.append(onCommandCreated)
        
        with open(debug_file, "a", encoding="utf-8") as f:
            f.write(f"Command definition created with ID: {command_id} at {time.ctime()}\n")
        
        # Execute the command
        cmdDef.execute()
        
        with open(debug_file, "a", encoding="utf-8") as f:
            f.write(f"Command execution triggered at {time.ctime()}\n")
        
        return True
    except Exception as e:
        try:
            with open(debug_file, "a", encoding="utf-8") as f:
                f.write(f"Error creating message box command: {str(e)} at {time.ctime()}\n")
                f.write(traceback.format_exc())
        except:
            pass
        return False

# Simple function to directly try showing a message box
def show_message_box(message):
    """Display a message box in Fusion 360."""
    try:
        # Log message for debugging
        debug_path = comm_file("message_debug.txt")
        with open(debug_path, "a", encoding="utf-8") as f:
            f.write(f"Trying to show message: {message} at {time.ctime()}\n")
        
        # Use the command-based approach
        success = create_message_box_command(message)
        
        # Log result
        with open(debug_path, "a", encoding="utf-8") as f:
            f.write(f"Command creation result: {success} at {time.ctime()}\n")
        
        return success
    except Exception as e:
        # Log failure
        with open(debug_path, "a", encoding="utf-8") as f:
            f.write(f"Error showing message box: {str(e)} at {time.ctime()}\n")
        return False

# Add a Command Handler for showing message boxes
class MessageBoxCommandExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self, message):
        super().__init__()
        self.message = message
    
    def notify(self, args):
        try:
            # Display the message
            debug_file = comm_file("message_command_debug.txt")
            with open(debug_file, "a", encoding="utf-8") as f:
                f.write(f"MessageBoxCommand executing for: {self.message} at {time.ctime()}\n")
            
            # Show the message box in the UI thread
            ui.messageBox(self.message, "Fusion MCP Message")
            
            with open(debug_file, "a", encoding="utf-8") as f:
                f.write(f"Message box displayed successfully at {time.ctime()}\n")
        except Exception as e:
            with open(debug_file, "a", encoding="utf-8") as f:
                f.write(f"Error in command handler: {str(e)} at {time.ctime()}\n")
                f.write(traceback.format_exc())

class MessageBoxCommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self, message):
        super().__init__()
        self.message = message
    
    def notify(self, args):
        try:
            debug_file = comm_file("message_command_debug.txt")
            with open(debug_file, "a", encoding="utf-8") as f:
                f.write(f"MessageBoxCommand created for: {self.message} at {time.ctime()}\n")
            
            # Get the command
            cmd = args.command
            
            # Connect to the execute event
            onExecute = MessageBoxCommandExecuteHandler(self.message)
            cmd.execute.add(onExecute)
            message_command_handlers.append(onExecute)
            
            # Set command properties
            cmd.isEnabled = True
            cmd.isVisible = False
            
            with open(debug_file, "a", encoding="utf-8") as f:
                f.write(f"Command handlers set up at {time.ctime()}\n")
        except Exception as e:
            with open(debug_file, "a", encoding="utf-8") as f:
                f.write(f"Error in command created handler: {str(e)} at {time.ctime()}\n")
                f.write(traceback.format_exc()) 
