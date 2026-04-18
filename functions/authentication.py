from firebase_admin import firestore, get_app, initialize_app
from google.cloud.firestore import FieldFilter


def _ensure_firebase_app() -> None:
    try:
        get_app()
    except ValueError:
        initialize_app()


def set_all_users_active() -> int:
    """
    Beállítja az összes users dokumentumnál az `active: True` mezőt.
    Visszatér az érintett (frissített) dokumentumok számával.
    """
    _ensure_firebase_app()
    db = firestore.client()
    users_ref = db.collection("users")
    docs = list(users_ref.stream())

    if not docs:
        return 0

    updated_count = 0
    batch = db.batch()
    ops_in_batch = 0

    for doc in docs:
        # merge=True viselkedést érünk el update-tel úgy, hogy csak ezt a mezőt írjuk.
        batch.update(doc.reference, {"active": True})
        updated_count += 1
        ops_in_batch += 1

        # Firestore batch limit: 500 művelet / commit.
        if ops_in_batch == 500:
            batch.commit()
            batch = db.batch()
            ops_in_batch = 0

    if ops_in_batch > 0:
        batch.commit()

    return updated_count


def set_team_users_status(team_id: str, enabled: bool) -> int:
    """
    A megadott teamhez tartozó usereknél beállítja a `status` mezőt.
    enabled=True  -> status: True
    enabled=False -> status: False
    """
    _ensure_firebase_app()
    db = firestore.client()
    users_query = db.collection("users").where(
        filter=FieldFilter("teamId", "==", team_id))
    docs = list(users_query.stream())

    if not docs:
        return 0

    updated_count = 0
    batch = db.batch()
    ops_in_batch = 0

    for doc in docs:
        batch.update(doc.reference, {"active": enabled})
        updated_count += 1
        ops_in_batch += 1

        if ops_in_batch == 500:
            batch.commit()
            batch = db.batch()
            ops_in_batch = 0

    if ops_in_batch > 0:
        batch.commit()

    return updated_count
