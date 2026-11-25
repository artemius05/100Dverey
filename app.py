from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"
IMPORTS_DIR = DATA_DIR / "imports"
DB_PATH = Path(os.environ.get("DOOR_CATALOG_DB", str(DATA_DIR / "catalog.sqlite3")))
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
ALLOWED_EXTENSIONS = {".xlsx", ".xls", ".csv", ".tsv", ".json", ".pdf", ".docx"}
PRICE_FIELDS = {"leafPrice", "blockPrice", "boxPrice", "archPrice", "extPrice"}
MODEL_FIELDS = {"colors", "glass", "feature", *PRICE_FIELDS}
API_BASE = os.environ.get("DOOR_API_BASE", "/api").rstrip("/") or "/api"
ACCESSORY_RE = re.compile(
    r"(?:^|[\s(])(?:добір|добор|короб(?:ка)?|лиштва|наличник|плінтус|плинтус|погонаж|"
    r"притворна\s+планка|з.?єднувальна\s+планка)(?=$|[\s:*0-9])",
    re.IGNORECASE,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return re.sub(r"[^\w]+", "", value, flags=re.UNICODE)


def clean_colors(value: Any) -> list[str]:
    source = value if isinstance(value, list) else [value]
    result: list[str] = []
    positions: dict[str, int] = {}
    for item in source:
        for part in re.split(r"[,;\r\n]+", str(item or "")):
            color = re.sub(r"\s+", " ", part).strip(" \t,;")
            if not color:
                continue
            key = normalize(color.rstrip("+"))
            if not key:
                continue
            if key in positions:
                if color.endswith("+"):
                    result[positions[key]] = result[positions[key]].rstrip("+") + "+"
                continue
            positions[key] = len(result)
            result.append(color[:120])
    return result[:100]


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def create_schema() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS manufacturers (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL CHECK(kind IN ('interior', 'entrance')),
                name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(kind, normalized_name)
            );
            CREATE TABLE IF NOT EXISTS import_batches (
                id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                kind TEXT NOT NULL,
                manufacturer_name TEXT NOT NULL,
                original_filename TEXT,
                stored_filename TEXT,
                sha256 TEXT,
                row_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                snapshot_json TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS models (
                id INTEGER PRIMARY KEY,
                manufacturer_id INTEGER NOT NULL REFERENCES manufacturers(id),
                name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                fields_json TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(manufacturer_id, normalized_name)
            );
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY,
                model_id INTEGER NOT NULL REFERENCES models(id),
                import_id TEXT REFERENCES import_batches(id),
                prices_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS import_rows (
                id INTEGER PRIMARY KEY,
                import_id TEXT NOT NULL REFERENCES import_batches(id),
                model_id INTEGER NOT NULL REFERENCES models(id),
                model_name TEXT NOT NULL,
                action TEXT NOT NULL,
                previous_json TEXT,
                incoming_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_models_manufacturer
                ON models(manufacturer_id, active, normalized_name);
            CREATE INDEX IF NOT EXISTS idx_prices_model_latest
                ON price_history(model_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_imports_created
                ON import_batches(created_at DESC);
            """
        )


def read_seed_catalog() -> tuple[dict[str, Any], str]:
    html_path = STATIC_DIR / "index.html"
    html = html_path.read_text(encoding="utf-8")
    match = re.search(
        r'<script id="embedded-door-db"[^>]*data-version="([^"]+)"[^>]*>(.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    if not match:
        raise RuntimeError("В index.html не знайдено вбудований каталог для першого запуску.")
    return json.loads(match.group(2)), match.group(1)


def split_prices(record: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    fields = {key: value for key, value in record.items() if key not in PRICE_FIELDS}
    prices = {
        key: value
        for key, value in record.items()
        if key in PRICE_FIELDS and isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    return fields, prices


def seed_if_empty() -> None:
    with connect() as conn:
        seeded = conn.execute("SELECT value FROM app_meta WHERE key='seed_version'").fetchone()
        if seeded:
            return
        catalog, version = read_seed_catalog()
        existing = conn.execute("SELECT COUNT(*) AS n FROM manufacturers").fetchone()["n"]
        if existing:
            conn.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES('seed_version',?)", (version,))
            return
        stamp = now_iso()
        for kind in ("interior", "entrance"):
            for manufacturer_name, models in (catalog.get(kind) or {}).items():
                cur = conn.execute(
                    "INSERT INTO manufacturers(kind,name,normalized_name,active,created_at,updated_at) "
                    "VALUES(?,?,?,1,?,?)",
                    (kind, manufacturer_name, normalize(manufacturer_name), stamp, stamp),
                )
                manufacturer_id = cur.lastrowid
                for model_name, record in (models or {}).items():
                    if ACCESSORY_RE.search(str(model_name)):
                        continue
                    fields, prices = split_prices(record or {})
                    model_cur = conn.execute(
                        "INSERT INTO models(manufacturer_id,name,normalized_name,fields_json,active,created_at,updated_at) "
                        "VALUES(?,?,?,?,1,?,?)",
                        (manufacturer_id, model_name, normalize(model_name), json.dumps(fields, ensure_ascii=False), stamp, stamp),
                    )
                    if prices:
                        conn.execute(
                            "INSERT INTO price_history(model_id,import_id,prices_json,created_at) VALUES(?,NULL,?,?)",
                            (model_cur.lastrowid, json.dumps(prices), stamp),
                        )
        conn.execute("INSERT INTO app_meta(key,value) VALUES('seed_version',?)", (version,))


def catalog_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    result: dict[str, Any] = {"interior": {}, "entrance": {}}
    rows = conn.execute(
        "SELECT mf.kind,mf.name AS manufacturer,m.id AS model_id,m.name AS model,m.fields_json "
        "FROM manufacturers mf JOIN models m ON m.manufacturer_id=mf.id "
        "WHERE mf.active=1 AND m.active=1 ORDER BY mf.name,m.name"
    ).fetchall()
    for row in rows:
        manufacturer = result[row["kind"]].setdefault(row["manufacturer"], {})
        fields = json.loads(row["fields_json"])
        if "colors" in fields:
            fields["colors"] = clean_colors(fields.get("colors"))
        price_row = conn.execute(
            "SELECT prices_json FROM price_history WHERE model_id=? ORDER BY id DESC LIMIT 1",
            (row["model_id"],),
        ).fetchone()
        if price_row:
            fields.update(json.loads(price_row["prices_json"]))
        manufacturer[row["model"]] = fields
    return result


def public_catalog() -> dict[str, Any]:
    with connect() as conn:
        return catalog_payload(conn)


def check_admin(password: str | None) -> None:
    expected = os.environ.get("DOOR_ADMIN_PASSWORD", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Пароль адміністратора не налаштований. Перезапустіть сервер.")
    if not password or not secrets.compare_digest(password, expected):
        raise HTTPException(status_code=401, detail="Неправильний пароль адміністратора.")


def valid_import(payload_text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Не вдалося прочитати дані імпорту.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Невірний формат імпорту.")
    kind = payload.get("kind")
    mode = payload.get("mode")
    maker = str(payload.get("maker") or "").strip()
    items = payload.get("models")
    if kind not in {"interior", "entrance"} or mode not in {"add", "update"}:
        raise HTTPException(status_code=400, detail="Невідомий тип або режим імпорту.")
    if not maker or len(maker) > 120:
        raise HTTPException(status_code=400, detail="Перевірте назву виробника.")
    if not isinstance(items, list) or not 1 <= len(items) <= 1000:
        raise HTTPException(status_code=400, detail="У прайсі має бути від 1 до 1000 моделей.")
    clean: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        name = re.sub(r"\s+", " ", str(item.get("model") or "")).strip()
        key = normalize(name)
        fields = item.get("fields")
        if not name or not key or len(name) > 180 or ACCESSORY_RE.search(name):
            continue
        if not isinstance(fields, dict):
            continue
        if key in seen:
            raise HTTPException(status_code=400, detail=f"У прайсі повторюється модель «{name}».")
        seen.add(key)
        normalized_fields: dict[str, Any] = {}
        for field, value in fields.items():
            if field not in MODEL_FIELDS or value is None or value == "":
                continue
            if field == "colors":
                normalized_fields[field] = clean_colors(value)
            elif field in PRICE_FIELDS:
                if isinstance(value, bool):
                    continue
                try:
                    amount = float(value)
                except (TypeError, ValueError):
                    continue
                if amount > 0 and amount < 100_000_000:
                    normalized_fields[field] = int(amount) if amount.is_integer() else amount
            else:
                normalized_fields[field] = re.sub(r"\s+", " ", str(value)).strip()[:1500]
        if not PRICE_FIELDS.intersection(normalized_fields):
            continue
        clean.append({"model": name, "key": key, "fields": normalized_fields})
    if not clean:
        raise HTTPException(status_code=400, detail="Не знайдено моделей із коректними цінами.")
    payload.update({"kind": kind, "mode": mode, "maker": maker})
    return payload, clean


def latest_prices(conn: sqlite3.Connection, model_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT prices_json FROM price_history WHERE model_id=? ORDER BY id DESC LIMIT 1", (model_id,)
    ).fetchone()
    return json.loads(row["prices_json"]) if row else {}


def manufacturer_snapshot(conn: sqlite3.Connection, manufacturer_id: int) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT id,name,normalized_name,fields_json,active FROM models WHERE manufacturer_id=?",
        (manufacturer_id,),
    ).fetchall()
    return {
        row["normalized_name"]: {
            "id": row["id"],
            "name": row["name"],
            "fields": json.loads(row["fields_json"]),
            "prices": latest_prices(conn, row["id"]),
            "active": row["active"],
        }
        for row in rows
    }


create_schema()
seed_if_empty()


def archive_legacy_leador_once() -> None:
    """Archive the old, incomplete LEADOR seed so it can be re-imported cleanly."""
    with connect() as conn:
        done = conn.execute("SELECT value FROM app_meta WHERE key='legacy_leador_archived'").fetchone()
        if done:
            return
        stamp = now_iso()
        rows = conn.execute(
            "SELECT id FROM manufacturers WHERE normalized_name=? AND active=1",
            (normalize("LEADOR"),),
        ).fetchall()
        for row in rows:
            conn.execute("UPDATE manufacturers SET active=0,updated_at=? WHERE id=?", (stamp, row["id"]))
            conn.execute("UPDATE models SET active=0,updated_at=? WHERE manufacturer_id=?", (stamp, row["id"]))
        conn.execute("INSERT INTO app_meta(key,value) VALUES('legacy_leador_archived','1')")


archive_legacy_leador_once()

app = FastAPI(title="Каталог дверей", docs_url=f"{API_BASE}/docs", redoc_url=None)


@app.get("/")
def home() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    api_config = f"<script>window.DOOR_API_BASE = {json.dumps(API_BASE)};</script>"
    html = html.replace("</head>", f"{api_config}\n</head>", 1)
    return HTMLResponse(html)


@app.get(f"{API_BASE}/catalog")
def get_catalog() -> dict[str, Any]:
    return public_catalog()


@app.post(f"{API_BASE}/imports")
async def import_price(
    file: UploadFile = File(...),
    payload: str = Form(...),
    x_admin_password: str | None = Header(default=None, alias="X-Admin-Password"),
) -> dict[str, Any]:
    check_admin(x_admin_password)
    data, models = valid_import(payload)
    original_name = Path(file.filename or "price").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Цей формат файлу не підтримується.")
    stored_name = f"{uuid4().hex}{suffix}"
    stored_path = IMPORTS_DIR / stored_name
    digest = hashlib.sha256()
    size = 0
    try:
        with stored_path.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Файл перевищує ліміт 100 МБ.")
                digest.update(chunk)
                output.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="Файл порожній.")
        import_id = uuid4().hex
        stamp = now_iso()
        added = updated = 0
        kind, maker, mode = data["kind"], data["maker"], data["mode"]
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            maker_key = normalize(maker)
            manufacturer = conn.execute(
                "SELECT * FROM manufacturers WHERE kind=? AND normalized_name=?",
                (kind, maker_key),
            ).fetchone()
            if mode == "update" and (not manufacturer or not manufacturer["active"]):
                raise HTTPException(status_code=404, detail="Виробника для оновлення не знайдено.")
            if mode == "add" and manufacturer and manufacturer["active"]:
                raise HTTPException(status_code=409, detail="Такий виробник уже є. Оберіть «Оновити прайс».")
            existed = bool(manufacturer and manufacturer["active"])
            snapshot = manufacturer_snapshot(conn, manufacturer["id"]) if existed else None
            conn.execute(
                "INSERT INTO import_batches(id,action,kind,manufacturer_name,original_filename,stored_filename,sha256,row_count,status,snapshot_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (import_id, mode, kind, maker, original_name, stored_name, digest.hexdigest(), len(models), "processing", json.dumps(snapshot, ensure_ascii=False) if snapshot is not None else None, stamp),
            )
            if manufacturer:
                manufacturer_id = manufacturer["id"]
                conn.execute("UPDATE manufacturers SET name=?,active=1,updated_at=? WHERE id=?", (maker, stamp, manufacturer_id))
            else:
                cur = conn.execute(
                    "INSERT INTO manufacturers(kind,name,normalized_name,active,created_at,updated_at) VALUES(?,?,?,1,?,?)",
                    (kind, maker, maker_key, stamp, stamp),
                )
                manufacturer_id = cur.lastrowid
            for incoming in models:
                name, model_key, fields = incoming["model"], incoming["key"], incoming["fields"]
                previous = conn.execute(
                    "SELECT * FROM models WHERE manufacturer_id=? AND normalized_name=?",
                    (manufacturer_id, model_key),
                ).fetchone()
                if previous:
                    old_fields = json.loads(previous["fields_json"])
                    old_record = {**old_fields, **latest_prices(conn, previous["id"])}
                    old_record["model"] = previous["name"]
                    merged = dict(old_fields)
                    for field, value in fields.items():
                        if field not in PRICE_FIELDS:
                            merged[field] = value
                    conn.execute(
                        "UPDATE models SET name=?,fields_json=?,active=1,updated_at=? WHERE id=?",
                        (name, json.dumps(merged, ensure_ascii=False), stamp, previous["id"]),
                    )
                    model_id = previous["id"]
                    updated += 1
                    action = "updated"
                else:
                    old_record = None
                    non_price = {key: value for key, value in fields.items() if key not in PRICE_FIELDS}
                    cur = conn.execute(
                        "INSERT INTO models(manufacturer_id,name,normalized_name,fields_json,active,created_at,updated_at) VALUES(?,?,?,?,1,?,?)",
                        (manufacturer_id, name, model_key, json.dumps(non_price, ensure_ascii=False), stamp, stamp),
                    )
                    model_id = cur.lastrowid
                    added += 1
                    action = "added"
                prices = {key: value for key, value in fields.items() if key in PRICE_FIELDS}
                if previous:
                    prices = {**latest_prices(conn, model_id), **prices}
                conn.execute(
                    "INSERT INTO price_history(model_id,import_id,prices_json,created_at) VALUES(?,?,?,?)",
                    (model_id, import_id, json.dumps(prices), stamp),
                )
                conn.execute(
                    "INSERT INTO import_rows(import_id,model_id,model_name,action,previous_json,incoming_json) VALUES(?,?,?,?,?,?)",
                    (import_id, model_id, name, action, json.dumps(old_record, ensure_ascii=False) if old_record is not None else None, json.dumps(incoming, ensure_ascii=False)),
                )
            conn.execute("UPDATE import_batches SET status='completed' WHERE id=?", (import_id,))
            catalog = catalog_payload(conn)
        return {
            "importId": import_id,
            "summary": {"added": added, "updated": updated, "keptExisting": True, "imported": len(models)},
            "catalog": catalog,
        }
    except HTTPException:
        stored_path.unlink(missing_ok=True)
        raise
    except sqlite3.IntegrityError as exc:
        stored_path.unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail="Не вдалося зберегти прайс через конфлікт даних.") from exc
    except Exception as exc:
        stored_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Помилка під час запису бази. Прайс не застосовано.") from exc
    finally:
        await file.close()


@app.post(f"{API_BASE}/manufacturers/archive")
def archive_manufacturer(
    payload: dict[str, Any],
    x_admin_password: str | None = Header(default=None, alias="X-Admin-Password"),
) -> dict[str, Any]:
    check_admin(x_admin_password)
    kind, maker = payload.get("kind"), str(payload.get("maker") or "").strip()
    if kind not in {"interior", "entrance"} or not maker:
        raise HTTPException(status_code=400, detail="Вкажіть тип дверей і виробника.")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id FROM manufacturers WHERE kind=? AND normalized_name=? AND active=1",
            (kind, normalize(maker)),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Активного виробника не знайдено.")
        conn.execute("UPDATE manufacturers SET active=0,updated_at=? WHERE id=?", (now_iso(), row["id"]))
        conn.execute("UPDATE models SET active=0,updated_at=? WHERE manufacturer_id=?", (now_iso(), row["id"]))
        conn.execute(
            "INSERT INTO import_batches(id,action,kind,manufacturer_name,status,created_at) VALUES(?,?,?,?,?,?)",
            (uuid4().hex, "archive", kind, maker, "completed", now_iso()),
        )
        catalog = catalog_payload(conn)
    return {"catalog": catalog, "archived": maker}


@app.get(f"{API_BASE}/imports")
def import_history(
    x_admin_password: str | None = Header(default=None, alias="X-Admin-Password"),
) -> list[dict[str, Any]]:
    check_admin(x_admin_password)
    with connect() as conn:
        rows = conn.execute(
            "SELECT id,action,kind,manufacturer_name,original_filename,row_count,status,created_at "
            "FROM import_batches ORDER BY created_at DESC,rowid DESC LIMIT 100"
        ).fetchall()
    return [dict(row) for row in rows]


@app.post(f"{API_BASE}/imports/{{import_id}}/rollback")
def rollback_import(
    import_id: str,
    x_admin_password: str | None = Header(default=None, alias="X-Admin-Password"),
) -> dict[str, Any]:
    check_admin(x_admin_password)
    rollback_id = uuid4().hex
    stamp = now_iso()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        batch = conn.execute(
            "SELECT * FROM import_batches WHERE id=? AND action IN ('add','update') AND status='completed'",
            (import_id,),
        ).fetchone()
        if not batch:
            raise HTTPException(status_code=404, detail="Імпорт не знайдений або його вже скасовано.")
        manufacturer = conn.execute(
            "SELECT * FROM manufacturers WHERE kind=? AND normalized_name=?",
            (batch["kind"], normalize(batch["manufacturer_name"])),
        ).fetchone()
        if not manufacturer:
            raise HTTPException(status_code=404, detail="Виробника імпорту більше немає в базі.")
        snapshot = json.loads(batch["snapshot_json"]) if batch["snapshot_json"] else None
        conn.execute(
            "INSERT INTO import_batches(id,action,kind,manufacturer_name,original_filename,row_count,status,snapshot_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (rollback_id, "rollback", batch["kind"], batch["manufacturer_name"], batch["original_filename"], batch["row_count"], "processing", None, stamp),
        )
        if snapshot is None:
            conn.execute("UPDATE manufacturers SET active=0,updated_at=? WHERE id=?", (stamp, manufacturer["id"]))
            conn.execute("UPDATE models SET active=0,updated_at=? WHERE manufacturer_id=?", (stamp, manufacturer["id"]))
        else:
            conn.execute("UPDATE manufacturers SET active=1,updated_at=? WHERE id=?", (stamp, manufacturer["id"]))
            conn.execute("UPDATE models SET active=0,updated_at=? WHERE manufacturer_id=?", (stamp, manufacturer["id"]))
            for item in snapshot.values():
                name = item["name"]
                key = normalize(name)
                row = conn.execute(
                    "SELECT id FROM models WHERE manufacturer_id=? AND normalized_name=?",
                    (manufacturer["id"], key),
                ).fetchone()
                fields = item.get("fields") or {}
                if row:
                    model_id = row["id"]
                    conn.execute(
                        "UPDATE models SET name=?,fields_json=?,active=?,updated_at=? WHERE id=?",
                        (name, json.dumps(fields, ensure_ascii=False), int(item.get("active", 1)), stamp, model_id),
                    )
                else:
                    cur = conn.execute(
                        "INSERT INTO models(manufacturer_id,name,normalized_name,fields_json,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (manufacturer["id"], name, key, json.dumps(fields, ensure_ascii=False), int(item.get("active", 1)), stamp, stamp),
                    )
                    model_id = cur.lastrowid
                old_prices = item.get("prices") or {}
                if old_prices:
                    conn.execute(
                        "INSERT INTO price_history(model_id,import_id,prices_json,created_at) VALUES(?,?,?,?)",
                        (model_id, rollback_id, json.dumps(old_prices), stamp),
                    )
        conn.execute("UPDATE import_batches SET status='completed' WHERE id=?", (rollback_id,))
        conn.execute("UPDATE import_batches SET status='rolled_back' WHERE id=?", (import_id,))
        catalog = catalog_payload(conn)
    return {"catalog": catalog, "rolledBack": import_id}


@app.get(f"{API_BASE}/health")
def health() -> dict[str, str]:
    return {"status": "ok", "database": DB_PATH.name}
