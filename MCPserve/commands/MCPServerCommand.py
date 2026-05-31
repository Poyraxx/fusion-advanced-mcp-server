#!/usr/bin/env python3

import adsk.core
import adsk.drawing
import adsk.fusion
import os
import sys
import traceback
import threading
import time
import json
import asyncio
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
                create_message_box_command(test_message)

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
        @fusion_mcp.resource("fusion://active-document-info")
        def get_active_document_info():
            """Get information about the active document in Fusion 360."""
            return get_active_document_info_resource()

        @fusion_mcp.resource("fusion://design-structure")
        def get_design_structure():
            """Get the structure of the active design in Fusion 360."""
            return get_design_structure_resource()

        @fusion_mcp.resource("fusion://parameters")
        def get_parameters():
            """Get the parameters of the active design in Fusion 360."""
            return get_parameters_resource()

        @fusion_mcp.resource("fusion://components")
        def get_components():
            """Get the components in the active design."""
            return get_components_resource()

        @fusion_mcp.resource("fusion://sketches")
        def get_sketches():
            """Get the sketches in the active design."""
            return get_sketches_resource()

        @fusion_mcp.resource("fusion://bodies")
        def get_bodies():
            """Get the bodies in the active design."""
            return get_bodies_resource()

        print("Registering tools...")

        @fusion_mcp.tool()
        def message_box(message: str) -> str:
            """Display a message box in Fusion 360."""
            return message_box_impl(message)

        @fusion_mcp.tool()
        def create_new_sketch(plane_name: str, component_name: str = "", sketch_name: str = "") -> str:
            """Create a new sketch on the specified plane."""
            return create_new_sketch_impl(plane_name, component_name, sketch_name)

        @fusion_mcp.tool()
        def create_parameter(name: str, expression: str, unit: str, comment: str = "") -> str:
            """Create or update a parameter in the active design."""
            return create_parameter_impl(name, expression, unit, comment)

        @fusion_mcp.tool()
        def create_component(name: str, reuse_existing: bool = True) -> dict:
            """Create a new component in the active design."""
            return create_component_impl(name, reuse_existing)

        @fusion_mcp.tool()
        def create_offset_plane(base_plane_name: str, offset: float | str, component_name: str = "", plane_name: str = "") -> dict:
            """Create a construction plane offset from an existing plane."""
            return create_offset_plane_impl(base_plane_name, offset, component_name, plane_name)

        @fusion_mcp.tool()
        def list_sketch_entities(sketch_name: str, component_name: str = "") -> dict:
            """List sketch points and curves, including entity tokens."""
            return list_sketch_entities_impl(sketch_name, component_name)

        @fusion_mcp.tool()
        def list_sketch_profiles(sketch_name: str, component_name: str = "") -> dict:
            """List sketch profiles for a sketch."""
            return list_sketch_profiles_impl(sketch_name, component_name)

        @fusion_mcp.tool()
        def create_sketch_point(sketch_name: str, x: float | str, y: float | str, z: float | str = 0.0, component_name: str = "") -> dict:
            """Create a sketch point using sketch-space coordinates."""
            return create_sketch_point_impl(sketch_name, x, y, z, component_name)

        @fusion_mcp.tool()
        def create_sketch_line(sketch_name: str, start_x: float | str, start_y: float | str, end_x: float | str, end_y: float | str, start_z: float | str = 0.0, end_z: float | str = 0.0, component_name: str = "") -> dict:
            """Create a sketch line between two sketch-space points."""
            return create_sketch_line_impl(sketch_name, start_x, start_y, end_x, end_y, start_z, end_z, component_name)

        @fusion_mcp.tool()
        def create_sketch_lines(sketch_name: str, points: list[dict], component_name: str = "") -> dict:
            """Create a polyline from multiple sketch-space points."""
            return create_sketch_lines_impl(sketch_name, points, component_name)

        @fusion_mcp.tool()
        def create_sketch_circle(sketch_name: str, center_x: float | str, center_y: float | str, radius: float | str, center_z: float | str = 0.0, component_name: str = "") -> dict:
            """Create a sketch circle from a center point and radius."""
            return create_sketch_circle_impl(sketch_name, center_x, center_y, radius, center_z, component_name)

        @fusion_mcp.tool()
        def create_sketch_rectangle(sketch_name: str, x1: float | str, y1: float | str, x2: float | str, y2: float | str, z1: float | str = 0.0, z2: float | str = 0.0, component_name: str = "") -> dict:
            """Create a two-point sketch rectangle."""
            return create_sketch_rectangle_impl(sketch_name, x1, y1, x2, y2, z1, z2, component_name)

        @fusion_mcp.tool()
        def create_sketch_center_rectangle(sketch_name: str, center_x: float | str, center_y: float | str, corner_x: float | str, corner_y: float | str, center_z: float | str = 0.0, corner_z: float | str = 0.0, component_name: str = "") -> dict:
            """Create a center-point sketch rectangle."""
            return create_sketch_center_rectangle_impl(sketch_name, center_x, center_y, corner_x, corner_y, center_z, corner_z, component_name)

        @fusion_mcp.tool()
        def create_sketch_arc(sketch_name: str, center_x: float | str, center_y: float | str, start_x: float | str, start_y: float | str, sweep_angle: float | str, center_z: float | str = 0.0, start_z: float | str = 0.0, component_name: str = "") -> dict:
            """Create a sketch arc from center, start point, and sweep angle."""
            return create_sketch_arc_impl(sketch_name, center_x, center_y, start_x, start_y, sweep_angle, center_z, start_z, component_name)

        @fusion_mcp.tool()
        def create_sketch_spline(sketch_name: str, points: list[dict], component_name: str = "") -> dict:
            """Create a fitted spline through multiple sketch-space points."""
            return create_sketch_spline_impl(sketch_name, points, component_name)

        @fusion_mcp.tool()
        def add_sketch_constraint(sketch_name: str, constraint_type: str, entity_one_token: str = "", entity_two_token: str = "", entity_three_token: str = "", component_name: str = "") -> dict:
            """Apply a geometric constraint using sketch entity tokens."""
            return add_sketch_constraint_impl(sketch_name, constraint_type, entity_one_token, entity_two_token, entity_three_token, component_name)

        @fusion_mcp.tool()
        def add_sketch_dimension(sketch_name: str, dimension_type: str, entity_one_token: str, entity_two_token: str = "", text_x: float | str = 0.0, text_y: float | str = 0.0, text_z: float | str = 0.0, orientation: str = "aligned", expression: str = "", component_name: str = "") -> dict:
            """Add a driving sketch dimension using sketch entity tokens."""
            return add_sketch_dimension_impl(sketch_name, dimension_type, entity_one_token, entity_two_token, text_x, text_y, text_z, orientation, expression, component_name)

        @fusion_mcp.tool()
        def create_extrude(sketch_name: str = "", distance: float | str = "10 mm", profile_index: int = 0, operation: str = "new_body", component_name: str = "", feature_name: str = "", body_name: str = "", direction: str = "positive", profile_token: str = "") -> dict:
            """Extrude a sketch profile into a 3D feature."""
            return create_extrude_impl(sketch_name, distance, profile_index, operation, component_name, feature_name, body_name, direction, profile_token)

        @fusion_mcp.tool()
        def create_revolve(sketch_name: str = "", axis_token: str = "", angle: float | str = "360 deg", profile_index: int = 0, operation: str = "new_body", component_name: str = "", feature_name: str = "", body_name: str = "", profile_token: str = "", axis_name: str = "") -> dict:
            """Revolve a sketch profile around an axis entity token."""
            return create_revolve_impl(sketch_name, axis_token, angle, profile_index, operation, component_name, feature_name, body_name, profile_token, axis_name)

        @fusion_mcp.tool()
        def delete_body(body_name: str, component_name: str = "", allow_partial_match: bool = False, delete_all_matches: bool = False) -> dict:
            """Delete one or more bodies from the active design."""
            return delete_body_impl(body_name, component_name, allow_partial_match, delete_all_matches)

        @fusion_mcp.tool()
        def export_sketch_dxf(sketch_name: str, filename: str = "", component_name: str = "") -> dict:
            """Export a sketch to a DXF file."""
            return export_sketch_dxf_impl(sketch_name, filename, component_name)

        @fusion_mcp.tool()
        def export_design_file(format: str, filename: str = "", component_name: str = "", body_name: str = "") -> dict:
            """Export the active design to STEP, IGES, SAT, STL, 3MF, or OBJ."""
            return export_design_file_impl(format, filename, component_name, body_name)

        @fusion_mcp.tool()
        def export_active_drawing_pdf(filename: str = "") -> dict:
            """Export the active drawing document to PDF."""
            return export_active_drawing_pdf_impl(filename)

        print("Registering prompts...")

        @fusion_mcp.prompt()
        def create_sketch_prompt(description: str) -> dict:
            """Create a prompt for creating a sketch based on a description."""
            return create_sketch_prompt_impl(description)

        @fusion_mcp.prompt()
        def parameter_setup_prompt(description: str) -> dict:
            """Create a prompt for setting up parameters based on a description."""
            return parameter_setup_prompt_impl(description)

        @fusion_mcp.prompt()
        def feature_strategy_prompt(description: str) -> dict:
            """Create a prompt for planning sketches and features for a Fusion model."""
            return feature_strategy_prompt_impl(description)

        resource_dispatch = {
            "fusion://active-document-info": get_active_document_info_resource,
            "fusion://design-structure": get_design_structure_resource,
            "fusion://parameters": get_parameters_resource,
            "fusion://components": get_components_resource,
            "fusion://sketches": get_sketches_resource,
            "fusion://bodies": get_bodies_resource,
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
                                        create_message_box_command(message)
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
                                        command_id = file.split("_")[1].split(".")[0]
                                        
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
        return
    
    # Set server running flag to stop the server loop
    server_running = False
    
    # Wait for the thread to finish
    if server_thread and server_thread.is_alive():
        server_thread.join(timeout=2.0)
    
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
