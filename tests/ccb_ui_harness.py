"""Isolated local harness for manually checking the CCB pages."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from werkzeug.security import generate_password_hash

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ccb_store import CCBTDeckStore
import webui


def seed(db_path: Path) -> None:
    webui._AUTH_DB_PATH = db_path
    webui._init_auth_db()
    conn = webui._db()
    try:
        user_id = int(conn.execute(
            "INSERT INTO users(username,full_name,email,password_hash,is_active,is_locked) VALUES (?,?,?,?,1,0)",
            ("alex", "Alex Musician", "alex@example.test", generate_password_hash("test-password")),
        ).lastrowid)
        group_id = int(conn.execute(
            "INSERT INTO groups(name,digico_allowed_auxes) VALUES (?,?)",
            ("Drums AUX", '["4"]'),
        ).lastrowid)
        conn.execute("INSERT INTO group_pages(group_id,page_key) VALUES (?,?)", (group_id, "page:digico_mixer"))
        conn.execute("INSERT INTO ccb_group_roles(group_id,role_key) VALUES (?,?)", (group_id, "drums"))
        conn.commit()
    finally:
        conn.close()

    store = CCBTDeckStore(db_path)
    store.cache_services([
        {"event_id": 801, "schedule_id": 50, "category_id": 7, "name": "8am Service", "schedule_name": "Sunday", "start": "2026-08-02T08:00:00+10:00", "service_plan_id": 9},
        {"event_id": 802, "schedule_id": 50, "category_id": 7, "name": "10am Service", "schedule_name": "Sunday", "start": "2026-08-02T10:00:00+10:00", "service_plan_id": 10},
    ])
    store.link_identity(external_id="11", user_id=user_id, display_name="Alex Musician", email="alex@example.test")
    roster = {
        "event_id": 801,
        "people": [
            {"id": 11, "name": "Alex Musician", "email": "alex@example.test", "raw": {}},
            {"id": 12, "name": "Jordan Singer", "email": "jordan@example.test", "raw": {}},
        ],
        "eligible_people": [{"id": 13, "name": "Taylor Vocalist", "email": "taylor@example.test", "pools": ["vocals"], "raw": {}}],
        "positions": [
            {"position_id": 1, "position_name": "Frontline", "team_name": "Worship", "assignments": [{"individual_id": 12, "name": "Jordan Singer", "status": "PENDING"}], "raw": {}},
            {"position_id": 2, "position_name": "Drums", "team_name": "Band", "assignments": [{"individual_id": 11, "name": "Alex Musician", "status": "ACCEPTED"}], "raw": {}},
            {"position_id": 3, "position_name": "Lighting Tech", "team_name": "Production", "assignments": [], "raw": {}},
        ],
        "warnings": [], "raw": {},
    }
    runsheet = {
        "plan_id": 9,
        "items": [
            {"id": 1, "order": 1, "item_type": "SECTION_HEADER", "name": "Pre-service", "description": "", "duration_seconds": 0, "start": "2026-08-02T07:55:00+10:00", "end": "2026-08-02T07:55:00+10:00", "links": [], "files": [], "raw": {}},
            {"id": 2, "order": 2, "item_type": "ITEM", "name": "Welcome", "description": "", "duration_seconds": 120, "start": "2026-08-02T07:55:00+10:00", "end": "2026-08-02T07:57:00+10:00", "links": [], "files": [], "raw": {}},
            {"id": 3, "order": 3, "item_type": "ITEM", "name": "Song 1", "description": "", "duration_seconds": 300, "start": "2026-08-02T07:57:00+10:00", "end": "2026-08-02T08:02:00+10:00", "links": [], "files": [], "raw": {}},
        ],
    }
    run_id = store.start_workflow_run(801, ["roster", "runsheet"], "harness")
    service = store.service(801)
    store.store_workflow_result(
        run_id,
        801,
        {"ok": True, "branches": {
            "roster": {"ok": True, "data": roster, "warnings": []},
            "runsheet": {"ok": True, "data": runsheet, "warnings": []},
        }},
        {"roster": service, "runsheet": service},
        actor_user_id=None,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5057)
    parser.add_argument("--data-dir")
    args = parser.parse_args()
    data_dir = Path(args.data_dir) if args.data_dir else Path(tempfile.mkdtemp(prefix="tdeck-ccb-ui-"))
    data_dir.mkdir(parents=True, exist_ok=True)
    seed(data_dir / "auth.db")
    auth_patch = patch.object(webui, "_auth_enabled", return_value=False)
    auth_patch.start()
    webui.app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)
