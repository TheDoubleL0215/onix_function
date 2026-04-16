# Welcome to Cloud Functions for Firebase for Python!
# To get started, simply uncomment the below code or create your own.
# Deploy with `firebase deploy`

from firebase_functions import https_fn
from firebase_functions.options import set_global_options
from firebase_admin import firestore, initialize_app
import json

# For cost control, you can set the maximum number of containers that can be
# running at the same time. This helps mitigate the impact of unexpected
# traffic spikes by instead downgrading performance. This limit is a per-function
# limit. You can override the limit for each function using the max_instances
# parameter in the decorator, e.g. @https_fn.on_request(max_instances=5).
set_global_options(max_instances=10)

initialize_app()


def _serialize_value(v):
    """Firestore to_dict() értékek JSON-barát formára (datetime, ref stb.)."""
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if hasattr(v, "path"):  # DocumentReference
        return v.path
    if isinstance(v, dict):
        return _serialize_dict(v)
    if isinstance(v, list):
        return [_serialize_value(x) for x in v]
    return v


def _serialize_dict(d):
    if d is None:
        return None
    return {k: _serialize_value(v) for k, v in d.items()}


@https_fn.on_request()
def listProjects(req: https_fn.Request) -> https_fn.Response:
    if req.method != "GET":
        return https_fn.Response("Method Not Allowed", status=405)

    db = firestore.client()
    docs = db.collection("projects").stream()
    projects = []

    for doc in docs:
        project_data = _serialize_dict(doc.to_dict() or {})
        project_data["id"] = doc.id
        projects.append(project_data)

    return https_fn.Response(
        json.dumps(projects),
        status=200,
        content_type="application/json",
    )
