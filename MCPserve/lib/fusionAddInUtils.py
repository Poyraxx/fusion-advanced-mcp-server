import adsk.core
import traceback


_handlers = []


def add_handler(handler):
    _handlers.append(handler)
    return handler


def clear_handlers():
    _handlers.clear()


def handle_error(context):
    message = f"{context} failed:\n{traceback.format_exc()}"
    print(message)

    app = adsk.core.Application.get()
    ui = app.userInterface if app else None
    if ui:
        try:
            ui.messageBox(message)
        except Exception:
            pass
