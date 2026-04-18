# Welcome to Cloud Functions for Firebase for Python!
# To get started, simply uncomment the below code or create your own.
# Deploy with `firebase deploy`

from firebase_functions import https_fn
from firebase_functions.options import set_global_options
from firebase_admin import firestore, initialize_app, storage
from google.cloud.firestore_v1.base_query import FieldFilter
import json
import hmac
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path


from export_excel import build_export_xlsx

from authentication import set_all_users_active, set_team_users_status

# For cost control, you can set the maximum number of containers that can be
# running at the same time. This helps mitigate the impact of unexpected
# traffic spikes by instead downgrading performance. This limit is a per-function
# limit. You can override the limit for each function using the max_instances
# parameter in the decorator, e.g. @https_fn.on_request(max_instances=5).
set_global_options(max_instances=10)

initialize_app()

logger = logging.getLogger(__name__)


def _resolve_local_export_dir():
    """
    Helyi .xlsx mentés mappája (emulátor / gépen futtatás).
    - EXPORT_LOCAL_DIR: explicit útvonal
    - Egyébként: functions/local_exports, ha emulátor vagy nincs Cloud Run K_SERVICE
    """
    explicit = os.environ.get("EXPORT_LOCAL_DIR", "").strip()
    if explicit:
        return explicit
    emu = os.environ.get("FUNCTIONS_EMULATOR", "").lower()
    if emu in ("true", "1", "yes"):
        return str(Path(__file__).resolve().parent / "local_exports")
    if os.environ.get("K_SERVICE"):
        return None
    return str(Path(__file__).resolve().parent / "local_exports")


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


def _build_wage_type_for_export(db, worklog_items: list, wage_type_doc_id: str) -> dict:
    """
    workspaces/{workspaceId}/wageTypes/{wageTypesDocId} defaultValue + customValue algyűjtemény (uid, value).
    Az export_excel a wageTypes.byWorkspace[workspaceId] struktúrát használja.
    """
    workspace_ids = {
        str(item["workspaceId"]).strip()
        for item in worklog_items
        if item.get("workspaceId")
    }
    by_workspace: dict = {}
    for wid in workspace_ids:
        wt_ref = (
            db.collection("workspaces")
            .document(wid)
            .collection("wageTypes")
            .document(wage_type_doc_id)
        )
        snap = wt_ref.get()
        if not snap.exists:
            continue
        data = snap.to_dict() or {}
        default_val = data.get("defaultValue")
        if default_val is None:
            default_val = data.get("default_value")
        custom_by_uid: dict = {}
        for cv_doc in wt_ref.collection("customValue").stream():
            cd = cv_doc.to_dict() or {}
            uid = cd.get("uid")
            if uid is None:
                continue
            val = cd.get("value")
            if val is None:
                val = cd.get("customValue")
            custom_by_uid[str(uid)] = val
        by_workspace[wid] = {
            "defaultValue": default_val,
            "customByUid": custom_by_uid,
        }
    return {"byWorkspace": by_workspace}


def _is_authorized_invite_request(req: https_fn.Request) -> bool:
    expected_token = os.environ.get("INVITE_API_TOKEN")
    auth_header = req.headers.get("Authorization", "")
    if not expected_token:
        return False
    if not auth_header.startswith("Bearer "):
        return False
    provided_token = auth_header.removeprefix("Bearer ").strip()
    return hmac.compare_digest(provided_token, expected_token)


@https_fn.on_request()
def createInvitation(req: https_fn.Request) -> https_fn.Response:
    if req.method not in ("GET", "POST"):
        return https_fn.Response(
            json.dumps({"error": "Method not allowed. Use GET or POST."}),
            status=405,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    body = {}
    if req.method == "POST" and req.is_json:
        body = req.get_json(silent=True) or {}
    email = body.get("email") or req.args.get("email")
    action = body.get("action") or req.args.get("action")
    try:
        if action == "invite":
            if req.method != "POST":
                return https_fn.Response(
                    json.dumps(
                        {"success": False, "error": "Method not allowed. Use POST for invite."}, ensure_ascii=False),
                    status=405,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                )
            if not _is_authorized_invite_request(req):
                return https_fn.Response(
                    json.dumps(
                        {"success": False, "error": "Unauthorized"}, ensure_ascii=False),
                    status=401,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                )
            email = str(email).strip().lower() if email else None
            if not email:
                return https_fn.Response(
                    json.dumps(
                        {"success": False, "error": "Missing required field: email"}, ensure_ascii=False),
                    status=400,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                )
            db = firestore.client()
            invitation_ref = db.collection("invitations").document()
            invitation_ref.set(
                {
                    "email": email,
                    "createdAt": firestore.SERVER_TIMESTAMP,
                    "status": "pending",
                }
            )
            return https_fn.Response(
                json.dumps(
                    {"success": True, "invitationId": invitation_ref.id}, ensure_ascii=False),
                status=200,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
        elif action == "validate":
            if not email:
                return https_fn.Response(
                    json.dumps(
                        {"success": False, "error": "Missing required field: email"}, ensure_ascii=False),
                    status=400,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                )
            email = str(email).strip().lower()
            db = firestore.client()
            invitation_ref = (
                db.collection("invitations")
                .where(filter=FieldFilter("email", "==", email))
                .where(filter=FieldFilter("status", "==", "pending"))
                .get(timeout=10)
            )
            if not invitation_ref:
                return https_fn.Response(
                    json.dumps({"exists": False}, ensure_ascii=False),
                    status=200,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                )
            return https_fn.Response(
                json.dumps({"exists": True, }, ensure_ascii=False),
                status=200,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )

        elif action == "verifyCode":
            code = body.get("code") or req.args.get("code")
            if not code:
                return https_fn.Response(
                    json.dumps(
                        {"success": False, "error": "Missing required field: code"}, ensure_ascii=False),
                    status=400,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                )
            db = firestore.client()
            verification_ref = db.collection("workspaces").where(
                filter=FieldFilter("teamId", "==", code)).get()
            if not verification_ref:
                return https_fn.Response(
                    json.dumps({"exists": False}, ensure_ascii=False),
                    status=200,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                )
            return https_fn.Response(
                json.dumps({"exists": True}, ensure_ascii=False),
                status=200,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
        return https_fn.Response(
            json.dumps(
                {"success": False, "error": "Invalid action. Use invite|validate|verifyCode"}, ensure_ascii=False),
            status=400,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
    except Exception as e:
        return https_fn.Response(
            json.dumps({"success": False, "error": str(e)},
                       ensure_ascii=False),
            status=500,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )


@https_fn.on_request()
def createWorkspace(req: https_fn.Request) -> https_fn.Response:
    if req.method != "POST":
        return https_fn.Response(
            json.dumps({"error": "Method not allowed. Use POST."},
                       ensure_ascii=False),
            status=405,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    body = req.get_json(silent=True)
    if not isinstance(body, dict):
        return https_fn.Response(
            json.dumps(
                {"error": "Invalid JSON body. Expected a JSON object."}, ensure_ascii=False),
            status=400,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
    email = body.get("email") or req.args.get("email")
    if not email:
        return https_fn.Response(
            json.dumps({"error": "Missing required parameter: email"},
                       ensure_ascii=False),
            status=400,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    try:
        db = firestore.client()
        authorized_docs = db.collection("invitations").where(
            filter=FieldFilter("email", "==", email)).get()
        if not authorized_docs:
            return https_fn.Response(
                json.dumps({"error": "Unauthorized email"},
                           ensure_ascii=False),
                status=401,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
        workspace_ref = db.collection("workspaces").document()
        workspace_ref.set(body)
        authorized_docs[0].reference.update(
            {"status": "done", "workspaceId": workspace_ref.id})

        return https_fn.Response(
            json.dumps(
                {"success": True, "workspaceId": workspace_ref.id}, ensure_ascii=False),
            status=200,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
    except Exception as e:
        return https_fn.Response(
            json.dumps({"success": False, "error": str(e)},
                       ensure_ascii=False),
            status=500,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )


@https_fn.on_request()
def changeWorkspaceStatus(req: https_fn.Request) -> https_fn.Response:
    if req.method not in ("POST", "GET"):
        return https_fn.Response(
            json.dumps({"error": "Method not allowed. Use GET or POST."}),
            status=405,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    body = req.get_json(silent=True) or {}
    team_id = req.args.get("teamId") or body.get("teamId")
    mode = req.args.get("mode") or body.get("mode")

    if not team_id:
        return https_fn.Response(
            json.dumps({"error": "Missing required parameter: teamId"}),
            status=400,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
    if not mode:
        return https_fn.Response(
            json.dumps(
                {"error": "Missing required parameter: mode (enable|disable)"}),
            status=400,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    mode_norm = str(mode).strip().lower()
    if mode_norm not in ("enable", "disable"):
        return https_fn.Response(
            json.dumps({"error": "Invalid mode. Use 'enable' or 'disable'."}),
            status=400,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    enabled = mode_norm == "enable"
    try:
        updated = set_team_users_status(team_id, enabled)
        return https_fn.Response(
            json.dumps(
                {
                    "success": True,
                    "teamId": team_id,
                    "mode": mode_norm,
                    "statusValue": enabled,
                    "updatedUsers": updated,
                },
                ensure_ascii=False,
            ),
            status=200,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
    except Exception as e:
        return https_fn.Response(
            json.dumps({"success": False, "error": str(e)},
                       ensure_ascii=False),
            status=500,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )


@https_fn.on_request()
def activateAllUsers(req: https_fn.Request) -> https_fn.Response:
    if req.method not in ("POST", "GET"):
        return https_fn.Response(
            json.dumps({"error": "Method not allowed. Use GET or POST."}),
            status=405,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    try:
        updated = set_all_users_active()
        return https_fn.Response(
            json.dumps({"success": True, "updatedUsers": updated},
                       ensure_ascii=False),
            status=200,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
    except Exception as e:
        return https_fn.Response(
            json.dumps({"success": False, "error": str(e)},
                       ensure_ascii=False),
            status=500,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )


@https_fn.on_request()
def projectExport(req: https_fn.Request) -> https_fn.Response:
    projectId = req.args.get("projectId")
    if not projectId:
        return https_fn.Response(
            "No projectId parameter provided",
            status=400,
        )

    db = firestore.client()
    project_ref = db.collection("projects").document(projectId)
    project_snapshot = project_ref.get()
    if not project_snapshot.exists:
        return https_fn.Response("Project not found", status=404)

    project_dict = project_snapshot.to_dict()
    team_id = project_dict.get("teamId")
    if not team_id:
        return https_fn.Response("Project has no teamId", status=400)

    worklog_query = db.collection_group("worklogs").where(
        filter=FieldFilter("assignedProjectId", "==", projectId)
    )
    material_query = db.collection_group("materials").where(
        filter=FieldFilter("projectId", "==", projectId)
    )
    users_query = db.collection("users").where(
        filter=FieldFilter("teamId", "==", team_id)
    )
    machines_query = db.collection("machines").where(
        filter=FieldFilter("teamId", "==", team_id)
    )
    machine_worklog_ref = db.collection("projects").document(
        projectId).collection("machineWorklog")

    worklog_items = []
    for doc in worklog_query.stream():
        data = doc.to_dict() or {}
        # Firestore != kihagyja a type mező nélküli dokumentumokat; itt kézzel: csak type == "machines" kiesik, hiányzó type benne marad.
        if data.get("type") == "machines":
            continue
        workspace_id = doc.reference.parent.parent.id if doc.reference.parent else None
        wl = dict(data)
        wl["id"] = doc.id
        # A path-ból számított workspace mindig felülírja a dokumentum mezőit (különben rossz/null workspaceId felülírhatja).
        wl["workspaceId"] = workspace_id
        worklog_items.append(wl)

    material_items = [{"id": doc.id, **doc.to_dict()}
                      for doc in material_query.stream()]
    users_items = [{"id": doc.id, **doc.to_dict()}
                   for doc in users_query.stream()]
    machines_items = [{"id": doc.id, **doc.to_dict()}
                      for doc in machines_query.stream()]
    machine_worklog_items = [
        {"id": doc.id, **doc.to_dict()}
        for doc in machine_worklog_ref.stream()
    ]

    wage_type_doc_id = (req.args.get("wageType") or "").strip() or None
    wage_type_for_export = (
        _build_wage_type_for_export(db, worklog_items, wage_type_doc_id)
        if wage_type_doc_id
        else {}
    )

    export_data = {
        "project": _serialize_dict(project_dict),
        "worklog": [_serialize_dict(x) for x in worklog_items],
        "material": [_serialize_dict(x) for x in material_items],
        "users": [_serialize_dict(x) for x in users_items],
        "machines": [_serialize_dict(x) for x in machines_items],
        "machineWorklog": [_serialize_dict(x) for x in machine_worklog_items],
        "wageType": wage_type_for_export,
    }

    print(json.dumps(export_data, indent=4))

    try:
        xlsx_bytes = build_export_xlsx(export_data)
    except Exception as e:
        return https_fn.Response(
            json.dumps({"error": "Excel export failed", "detail": str(e)}),
            status=500,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    # Egyedi kulcs minden exportra (csak nap → ugyanaz a path, Storage felülírja a blobot).
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    project_name = str(project_dict.get("projectName")
                       or "project").strip().lower()
    safe_project_name = "".join(
        ch if ch.isalnum() or ch in ("-", "_") else "_"
        for ch in project_name
    ).strip("_")
    if not safe_project_name:
        safe_project_name = "project"
    file_name = f"{safe_project_name}_{stamp}.xlsx"
    storage_path = f"exports/{safe_project_name}/{file_name}"

    local_path_written = None
    local_save_error = None
    local_dir = _resolve_local_export_dir()
    if local_dir:
        try:
            os.makedirs(local_dir, exist_ok=True)
            local_path = os.path.join(local_dir, file_name)
            with open(local_path, "wb") as f:
                f.write(xlsx_bytes)
            local_path_written = os.path.abspath(local_path)
        except OSError as e:
            local_save_error = str(e)
            logger.warning(
                "Helyi Excel mentés sikertelen (%s): %s", local_dir, e)

    try:
        bucket = storage.bucket()
        blob = bucket.blob(storage_path)
        blob.upload_from_string(
            xlsx_bytes,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as e:
        return https_fn.Response(
            json.dumps({"error": "Storage upload failed", "detail": str(e)}),
            status=500,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    download_url = None
    try:
        download_url = blob.generate_signed_url(
            expiration=timedelta(hours=1),
            method="GET",
        )
    except Exception:
        # Lokális/emulator: nincs privát kulcs a credentialban, signed URL nem lehet.
        # A kliens a storagePath-tal a Firebase Storage SDK getDownloadURL() használatával lekérheti az URL-t.
        pass

    payload = {
        "fileName": file_name,
        "storagePath": storage_path,
    }
    if wage_type_doc_id:
        payload["wageType"] = wage_type_doc_id
    if download_url:
        payload["downloadUrl"] = download_url
    if local_path_written:
        payload["localPath"] = local_path_written
    if local_save_error:
        payload["localSaveError"] = local_save_error

    return https_fn.Response(
        json.dumps(payload, ensure_ascii=False),
        status=200,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
