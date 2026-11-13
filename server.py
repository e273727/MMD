#!/usr/bin/env python3
"""Local, dependency-free document register for e-file MMD."""
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import json, mimetypes, os, re, secrets, sqlite3, threading, subprocess, urllib.parse, datetime
import socket, sys
from email.parser import BytesParser
from email.policy import default

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
if getattr(sys, "frozen", False) and os.name == "nt":
    DATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "EFileMMD"
else:
    DATA = ROOT / "data"
FILES = DATA / "documents"
DB = DATA / "efile.sqlite3"
SETTINGS = DATA / "settings.json"
ALLOWED = {".pdf", ".doc", ".docx", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
LOCK = threading.Lock()
DB_LOCK = threading.RLock()
MASTER_FIELDS = {"documentType":"Document Type", "financialYear":"Financial Year"}
MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"]
STANDARD_DOC_TYPES = [
    "Purchase Order", "Quotation", "Financial Approval", "Technical Specification",
    "Comparative Statement", "Work Order", "GRN (Goods Received Note)", "Invoice",
    "Delivery Challan", "Inspection Report", "Condemnation", "Other"
]
STANDARD_FINANCIAL_YEARS = [
    "2024-25", "2025-26", "2026-27", "2027-28", "2028-29", "2029-30"
]
DEFAULT_MASTERS = {"documentType": STANDARD_DOC_TYPES, "financialYear": STANDARD_FINANCIAL_YEARS}
SESSION_LOCK = threading.RLock()
SESSIONS = {}
SESSION_TTL = 12 * 60 * 60
MAX_BYTES = 250 * 1024 * 1024
UPLOAD_SLOT = threading.Lock()

def settings():
    try: cfg=json.loads(SETTINGS.read_text())
    except (OSError, ValueError): cfg={"database": {"backend":"sqlite"}, "files": {"backend":"local"}}
    if not isinstance(cfg,dict): cfg={"database": {"backend":"sqlite"}, "files": {"backend":"local"}}
    existing=cfg.get("masters",{})
    if not isinstance(existing,dict): existing={}
    masters = {}
    doc_vals = existing.get("documentType", [])
    merged_docs = list(doc_vals) if isinstance(doc_vals, list) else []
    for dt in STANDARD_DOC_TYPES:
        if dt not in merged_docs:
            merged_docs.append(dt)
    masters["documentType"] = merged_docs or STANDARD_DOC_TYPES
    fy_vals = existing.get("financialYear", [])
    merged_fy = list(fy_vals) if isinstance(fy_vals, list) else []
    for fy in STANDARD_FINANCIAL_YEARS:
        if fy not in merged_fy:
            merged_fy.append(fy)
    masters["financialYear"] = merged_fy or STANDARD_FINANCIAL_YEARS
    cfg["masters"] = masters
    return cfg

def normalize_month(val):
    if isinstance(val, int) and 1 <= val <= 12:
        return val, MONTHS[val - 1]
    s = str(val).strip()
    if s.isdigit():
        num = int(s)
        if 1 <= num <= 12:
            return num, MONTHS[num - 1]
    for idx, m in enumerate(MONTHS, 1):
        if m.lower() == s.lower():
            return idx, m
    raise ValueError(f"Invalid month '{val}'. Must be between 1 and 12 or a valid month name.")

def save_settings(cfg):
    DATA.mkdir(parents=True,exist_ok=True)
    tmp=SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg,indent=2))
    os.replace(tmp,SETTINGS)

def sys_platform_is_mac():
    import sys
    return sys.platform == "darwin"

def smb_mount_path(cfg):
    share=str(cfg.get("share","")).strip()
    host=str(cfg.get("host","")).strip()
    if not share: return None
    if os.name == "nt":
        if not host: return None
        candidate=Path(f"//{host}/{share}")
        return candidate if candidate.is_dir() else None
    if not sys_platform_is_mac(): return None
    vols=Path("/Volumes")
    candidates=[vols/share]
    try: candidates += [p for p in vols.iterdir() if p.name.casefold()==share.casefold()]
    except OSError: pass
    return next((p for p in candidates if p.is_dir()),None)

def validate_postgres(cfg,password):
    try: import psycopg
    except ImportError as e: raise ValueError("Install the PostgreSQL extras first; see README.md.") from e
    try:
        with psycopg.connect(host=cfg["host"],port=cfg["port"],dbname=cfg["name"],user=cfg["user"],password=password,
                             sslmode=cfg.get("sslmode","prefer"),connect_timeout=5) as c: c.execute("SELECT 1")
    except Exception as e: raise ValueError(f"PostgreSQL connection failed: {type(e).__name__}: {e}")

def connect():
    backend=settings().get("database", {}).get("backend", "sqlite")
    if backend == "sqlite":
        c = sqlite3.connect(DB, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        return c
    try:
        import keyring
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as e:
        raise RuntimeError("PostgreSQL support needs the optional dependencies. Follow the network setup in README.md.") from e
    cfg=settings().get("database", {})
    password=keyring.get_password(secret_key(), cfg.get("user", ""))
    if password is None: raise RuntimeError("PostgreSQL password is not available in the system credential store. Re-enter it in Storage settings.")
    conn=psycopg.connect(host=cfg.get("host"), port=int(cfg.get("port",5432)), dbname=cfg.get("name"), user=cfg.get("user"), password=password,
                         sslmode=cfg.get("sslmode","prefer"), connect_timeout=5, row_factory=dict_row)
    return PgConnection(conn)

class PgRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int): return list(self.values())[key]
        return super().__getitem__(key)

class PgCursor:
    def __init__(self, cursor): self.cursor=cursor
    def fetchone(self):
        row=self.cursor.fetchone()
        return PgRow(row) if row is not None else None
    def fetchall(self): return [PgRow(row) for row in self.cursor.fetchall()]

class PgConnection:
    def __init__(self, conn): self.conn=conn
    def __enter__(self): self.conn.__enter__(); return self
    def __exit__(self, *args): return self.conn.__exit__(*args)
    def execute(self, sql, params=()): return PgCursor(self.conn.execute(sql.replace("?", "%s"), params))

def local_connect():
    c=sqlite3.connect(DB, timeout=15); c.row_factory=sqlite3.Row; c.execute("PRAGMA foreign_keys=ON"); return c

def new_serial_key():
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"MMD-{stamp}-{secrets.token_hex(4).upper()}"

def make_password_hash(password):
    import hashlib
    salt=secrets.token_bytes(16)
    digest=hashlib.pbkdf2_hmac("sha256",password.encode("utf-8"),salt,310_000)
    return f"pbkdf2_sha256$310000${salt.hex()}${digest.hex()}"

def verify_password(password, encoded):
    import hashlib, hmac
    try:
        method,rounds,salt,digest=encoded.split("$")
        if method!="pbkdf2_sha256": return False
        candidate=hashlib.pbkdf2_hmac("sha256",password.encode("utf-8"),bytes.fromhex(salt),int(rounds)).hex()
        return hmac.compare_digest(candidate,digest)
    except (ValueError,TypeError): return False

def create_session(user_id):
    token=secrets.token_urlsafe(32)
    with SESSION_LOCK: SESSIONS[token]=(user_id,datetime.datetime.now(datetime.timezone.utc).timestamp()+SESSION_TTL)
    return token

def authenticated_user(handler):
    from http.cookies import SimpleCookie
    cookie=SimpleCookie(); cookie.load(handler.headers.get("Cookie",""))
    morsel=cookie.get("efile_session")
    if not morsel: return None
    token=morsel.value
    with SESSION_LOCK:
        session=SESSIONS.get(token)
        if not session: return None
        if session[1] < datetime.datetime.now(datetime.timezone.utc).timestamp():
            SESSIONS.pop(token,None); return None
    with local_connect() as db: row=db.execute("SELECT id,username,role FROM app_users WHERE id=?",(session[0],)).fetchone()
    return dict(row) if row else None

def set_session_cookie(handler,token,max_age=SESSION_TTL):
    secure="; Secure" if handler.headers.get("X-Forwarded-Proto","http").casefold()=="https" else ""
    handler.send_header("Set-Cookie",f"efile_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}")

def validate_account(username,password):
    username=re.sub(r"\s+"," ",str(username)).strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{3,40}",username): raise ValueError("Username must be 3–40 characters using letters, numbers, dot, underscore, or hyphen.")
    if not isinstance(password,str) or len(password)<12 or len(password)>256: raise ValueError("Password must be at least 12 characters.")
    return username

def initialize():
    FILES.mkdir(parents=True, exist_ok=True)
    with local_connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, name TEXT NOT NULL, stored_name TEXT NOT NULL UNIQUE,
          size INTEGER NOT NULL, media_type TEXT NOT NULL, uploaded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          storage_path TEXT, unit TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '', subcategory TEXT NOT NULL DEFAULT '',
          document_type TEXT NOT NULL DEFAULT '', item_category TEXT NOT NULL DEFAULT '', department TEXT NOT NULL DEFAULT '', financial_year TEXT NOT NULL DEFAULT '',
          month TEXT NOT NULL DEFAULT '', item_text TEXT NOT NULL DEFAULT '', vendor_text TEXT NOT NULL DEFAULT '', serial_key TEXT);
        CREATE TABLE IF NOT EXISTS tags(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE);
        CREATE TABLE IF NOT EXISTS document_tags(document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
          tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE, PRIMARY KEY(document_id,tag_id));
        CREATE INDEX IF NOT EXISTS idx_doc_tags_tag ON document_tags(tag_id);
        CREATE TABLE IF NOT EXISTS app_logs(id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          level TEXT NOT NULL, action TEXT NOT NULL, message TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_app_logs_time ON app_logs(occurred_at DESC);
        CREATE TABLE IF NOT EXISTS app_users(id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
          password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','upload')),
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
        """)
        c.executescript("""
        CREATE TABLE IF NOT EXISTS purchase_orders(
            id TEXT PRIMARY KEY,
            po_id TEXT NOT NULL UNIQUE COLLATE NOCASE,
            financial_year TEXT NOT NULL,
            month TEXT NOT NULL,
            remarks TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            created_by TEXT NOT NULL DEFAULT ''
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_po_po_id_uniq ON purchase_orders(po_id COLLATE NOCASE);

        CREATE TABLE IF NOT EXISTS po_items(
            id TEXT PRIMARY KEY,
            purchase_order_id TEXT NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
            item_code TEXT NOT NULL DEFAULT '',
            item_name TEXT NOT NULL DEFAULT '',
            vendor_code TEXT NOT NULL DEFAULT '',
            vendor_name TEXT NOT NULL DEFAULT '',
            remarks TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_po_items_po ON po_items(purchase_order_id);

        CREATE TABLE IF NOT EXISTS item_documents(
            id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL REFERENCES po_items(id) ON DELETE CASCADE,
            document_id TEXT REFERENCES documents(id) ON DELETE SET NULL,
            document_name TEXT NOT NULL,
            document_type TEXT NOT NULL,
            file_path TEXT NOT NULL,
            uploaded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            uploaded_by TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_item_docs_item ON item_documents(item_id);
        """)

        cols={r[1] for r in c.execute("PRAGMA table_info(documents)").fetchall()}
        for column in ("storage_path TEXT", "unit TEXT NOT NULL DEFAULT ''", "category TEXT NOT NULL DEFAULT ''", "subcategory TEXT NOT NULL DEFAULT ''", "document_type TEXT NOT NULL DEFAULT ''", "item_category TEXT NOT NULL DEFAULT ''", "department TEXT NOT NULL DEFAULT ''", "financial_year TEXT NOT NULL DEFAULT ''", "month TEXT NOT NULL DEFAULT ''", "item_text TEXT NOT NULL DEFAULT ''", "vendor_text TEXT NOT NULL DEFAULT ''", "serial_key TEXT", "purchase_id TEXT NOT NULL DEFAULT ''", "po_id TEXT NOT NULL DEFAULT ''", "item_code TEXT NOT NULL DEFAULT ''", "vendor_code TEXT NOT NULL DEFAULT ''", "remark TEXT NOT NULL DEFAULT ''"):
            name=column.split()[0]
            if name not in cols: c.execute(f"ALTER TABLE documents ADD COLUMN {column}")
        
        po_item_cols = {r[1] for r in c.execute("PRAGMA table_info(po_items)").fetchall()}
        if "item_name" not in po_item_cols:
            c.execute("ALTER TABLE po_items ADD COLUMN item_name TEXT NOT NULL DEFAULT ''")
        if "vendor_name" not in po_item_cols:
            c.execute("ALTER TABLE po_items ADD COLUMN vendor_name TEXT NOT NULL DEFAULT ''")

        item_doc_cols = {r[1] for r in c.execute("PRAGMA table_info(item_documents)").fetchall()}
        if "document_id" not in item_doc_cols:
            c.execute("ALTER TABLE item_documents ADD COLUMN document_id TEXT")
        rows=c.execute("SELECT id,uploaded_at,item_category FROM documents WHERE serial_key IS NULL OR serial_key='' ").fetchall()
        for row in rows:
            c.execute("UPDATE documents SET serial_key=?, item_text=CASE WHEN item_text='' THEN ? ELSE item_text END WHERE id=?",(new_serial_key(),row["item_category"] or "",row["id"]))
        c.execute("UPDATE documents SET item_text=item_category WHERE item_text='' AND item_category<>''")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_serial_key ON documents(serial_key)")
        
        # Safe migration / backfill for legacy document PO IDs and item codes
        try:
            has_purchases = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='purchases'").fetchone()
            if has_purchases:
                c.execute("""
                    UPDATE documents
                    SET po_id = (SELECT purchase_no FROM purchases WHERE purchases.id = documents.purchase_id),
                        item_code = CASE WHEN (item_code IS NULL OR item_code = '') THEN (SELECT COALESCE(item_code, '') FROM purchases WHERE purchases.id = documents.purchase_id) ELSE item_code END
                    WHERE (po_id IS NULL OR po_id = '') AND purchase_id IS NOT NULL AND purchase_id != ''
                """)
        except Exception:
            pass

        # Backfill orphan item_documents into documents table so all PO documents are visible
        try:
            orphans = c.execute("""
                SELECT id, item_id, document_id, document_name, document_type, file_path, uploaded_at, uploaded_by
                FROM item_documents
                WHERE document_id IS NULL OR document_id = '' OR document_id NOT IN (SELECT id FROM documents)
            """).fetchall()
            for od in orphans:
                fpath = od["file_path"] or ""
                fname = os.path.basename(fpath) if fpath else od["document_name"]
                pitem = c.execute("""
                    SELECT pi.item_code, pi.item_name, pi.vendor_code, pi.vendor_name,
                           po.po_id, po.financial_year, po.month
                    FROM po_items pi
                    JOIN purchase_orders po ON pi.purchase_order_id = po.id
                    WHERE pi.id = ?
                """, (od["item_id"],)).fetchone()
                po_val = pitem["po_id"] if pitem else ""
                fy_val = pitem["financial_year"] if pitem else ""
                mo_val = pitem["month"] if pitem else ""
                it_code = pitem["item_code"] if pitem else ""
                it_name = pitem["item_name"] if pitem else ""
                vd_code = pitem["vendor_code"] if pitem else ""
                vd_name = pitem["vendor_name"] if pitem else ""
                doc_id = new_id()
                skey = new_serial_key()
                fsize = 0
                if fpath and os.path.exists(fpath):
                    fsize = os.path.getsize(fpath)
                elif (FILES / fname).exists():
                    fsize = (FILES / fname).stat().st_size
                c.execute("""
                    INSERT INTO documents (
                        id, name, stored_name, size, media_type, uploaded_at, storage_path,
                        document_type, financial_year, month, item_text, vendor_text,
                        serial_key, po_id, item_code, vendor_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    doc_id, od["document_name"], fname, fsize, "application/pdf",
                    od["uploaded_at"] or now_iso(), fpath, od["document_type"] or "Purchase Order",
                    fy_val, mo_val, it_name, vd_name, skey, po_val, it_code, vd_code
                ))
                c.execute("UPDATE item_documents SET document_id = ? WHERE id = ?", (doc_id, od["id"]))
        except Exception:
            pass
    if settings().get("database", {}).get("backend") == "postgres":
        try: ensure_postgres_schema()
        except Exception as e:
            # Keep local login/setup available so the user can fix PostgreSQL settings.
            write_log("ERROR","PostgreSQL startup",f"{type(e).__name__}: {e}")
            print("PostgreSQL is not ready; the app will stay open so Storage settings can be corrected.",flush=True)

def write_log(level, action, message):
    # Keep a bounded local history; never log document contents or tag values.
    with local_connect() as c:
        c.execute("INSERT INTO app_logs(level,action,message) VALUES(?,?,?)", (level[:12], action[:40], message[:500]))
        c.execute("DELETE FROM app_logs WHERE id NOT IN (SELECT id FROM app_logs ORDER BY id DESC LIMIT 500)")

def ensure_postgres_schema():
    with connect() as c:
        for sql in [
            "CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, name TEXT NOT NULL, stored_name TEXT NOT NULL UNIQUE, size BIGINT NOT NULL, media_type TEXT NOT NULL, uploaded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, storage_path TEXT, unit TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '', subcategory TEXT NOT NULL DEFAULT '', document_type TEXT NOT NULL DEFAULT '', item_category TEXT NOT NULL DEFAULT '', department TEXT NOT NULL DEFAULT '', financial_year TEXT NOT NULL DEFAULT '', month TEXT NOT NULL DEFAULT '', item_text TEXT NOT NULL DEFAULT '', vendor_text TEXT NOT NULL DEFAULT '', serial_key TEXT)",
            "CREATE TABLE IF NOT EXISTS tags(id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE)",
            "CREATE TABLE IF NOT EXISTS document_tags(document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE, tag_id BIGINT NOT NULL REFERENCES tags(id) ON DELETE CASCADE, PRIMARY KEY(document_id,tag_id))",
            "CREATE TABLE IF NOT EXISTS app_logs(id BIGSERIAL PRIMARY KEY, occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, level TEXT NOT NULL, action TEXT NOT NULL, message TEXT NOT NULL)",
            "CREATE INDEX IF NOT EXISTS idx_doc_tags_tag ON document_tags(tag_id)",
            "CREATE INDEX IF NOT EXISTS idx_app_logs_time ON app_logs(occurred_at DESC)",
        ]: c.execute(sql)
        for column in ("unit", "category", "subcategory", "document_type", "item_category", "department", "financial_year", "month", "item_text", "vendor_text", "serial_key"):
            c.execute(f"ALTER TABLE documents ADD COLUMN IF NOT EXISTS {column} TEXT NOT NULL DEFAULT ''" if column!="serial_key" else "ALTER TABLE documents ADD COLUMN IF NOT EXISTS serial_key TEXT")
        rows=c.execute("SELECT id,item_category FROM documents WHERE serial_key IS NULL OR serial_key='' ").fetchall()
        for row in rows:
            c.execute("UPDATE documents SET serial_key=?, item_text=CASE WHEN item_text='' THEN ? ELSE item_text END WHERE id=?",(new_serial_key(),row["item_category"] or "",row["id"]))
        c.execute("UPDATE documents SET item_text=item_category WHERE item_text='' AND item_category<>''")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_serial_key ON documents(serial_key)")
    # Import old local records into PostgreSQL by stable document ID.
    with local_connect() as local:
        docs=local.execute("SELECT id,name,stored_name,size,media_type,uploaded_at,storage_path,unit,category,subcategory,document_type,item_category,department,financial_year,month,item_text,vendor_text,serial_key FROM documents").fetchall()
        tags=local.execute("SELECT id,name FROM tags").fetchall()
        links=local.execute("SELECT document_id,tag_id FROM document_tags").fetchall()
    with connect() as remote:
        for d in docs:
            remote.execute("INSERT INTO documents(id,name,stored_name,size,media_type,uploaded_at,storage_path,unit,category,subcategory,document_type,item_category,department,financial_year,month,item_text,vendor_text,serial_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                           (d["id"],d["name"],d["stored_name"],d["size"],d["media_type"],d["uploaded_at"],d["storage_path"],d["unit"],d["category"],d["subcategory"],d["document_type"],d["item_category"],d["department"],d["financial_year"],d["month"],d["item_text"],d["vendor_text"],d["serial_key"]))
        for t in tags: remote.execute("INSERT INTO tags(name) VALUES(?) ON CONFLICT(name) DO NOTHING", (t["name"],))
        for link in links:
            tag=remote.execute("SELECT id FROM tags WHERE name=?",(local.execute("SELECT name FROM tags WHERE id=?",(link["tag_id"],)).fetchone()[0],)).fetchone()
            if tag: remote.execute("INSERT INTO document_tags(document_id,tag_id) VALUES(?,?) ON CONFLICT DO NOTHING",(link["document_id"],tag["id"]))

def copy_postgres_to_local():
    with connect() as remote:
        docs=remote.execute("SELECT id,name,stored_name,size,media_type,uploaded_at,storage_path,unit,category,subcategory,document_type,item_category,department,financial_year,month,item_text,vendor_text,serial_key FROM documents").fetchall()
        tags=remote.execute("SELECT id,name FROM tags").fetchall()
        links=remote.execute("SELECT document_id,tag_id FROM document_tags").fetchall()
    tag_names={r["id"]:r["name"] for r in tags}
    with local_connect() as local:
        for d in docs:
            local.execute("INSERT OR IGNORE INTO documents(id,name,stored_name,size,media_type,uploaded_at,storage_path,unit,category,subcategory,document_type,item_category,department,financial_year,month,item_text,vendor_text,serial_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (d["id"],d["name"],d["stored_name"],d["size"],d["media_type"],d["uploaded_at"].isoformat() if hasattr(d["uploaded_at"],"isoformat") else d["uploaded_at"],d["storage_path"],d["unit"],d["category"],d["subcategory"],d["document_type"],d["item_category"],d["department"],d["financial_year"],d["month"],d["item_text"],d["vendor_text"],d["serial_key"]))
        for t in tags: local.execute("INSERT OR IGNORE INTO tags(name) VALUES(?)",(t["name"],))
        for link in links:
            name=tag_names.get(link["tag_id"])
            if name:
                local_tag=local.execute("SELECT id FROM tags WHERE name=? COLLATE NOCASE",(name,)).fetchone()
                if local_tag: local.execute("INSERT OR IGNORE INTO document_tags(document_id,tag_id) VALUES(?,?)",(link["document_id"],local_tag["id"]))

def clean_name(name):
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r"[^\w.()\- ]", "_", name, flags=re.UNICODE).strip(" .")
    return name[:180] or "document"

def request_origin_matches(handler):
    origin = handler.headers.get("Origin")
    if origin is None: return True
    parsed = urlparse(origin)
    if parsed.scheme not in ("http", "https"): return False
    h_host = handler.headers.get("Host", "").casefold()
    o_host = parsed.netloc.casefold()
    if o_host == h_host: return True
    def norm(h): return h.replace("localhost", "127.0.0.1")
    return norm(o_host) == norm(h_host)

def safe_tags(raw):
    try: values = json.loads(raw)
    except Exception: raise ValueError("Tags must be a valid list.")
    if not isinstance(values, list) or len(values) > 20: raise ValueError("Add up to 20 tags.")
    result, seen = [], set()
    for item in values:
        if not isinstance(item, str): raise ValueError("Each tag must be text.")
        tag = re.sub(r"\s+", " ", item).strip()
        if not tag or len(tag) > 40: raise ValueError("Tags must contain 1–40 characters.")
        if tag.casefold() not in seen: result.append(tag); seen.add(tag.casefold())
    return result

def make_xlsx(rows):
    from io import BytesIO
    from zipfile import ZipFile, ZIP_DEFLATED
    from xml.sax.saxutils import escape
    headers=["PO ID","Item Code","Item Name","Vendor Code","Vendor Name","Document Type","Financial Year","Month","Remarks","Unique Key","Document Name","Uploaded At"]
    columns=["A","B","C","D","E","F","G","H","I","J","K","L"]
    def xml_cell(ref,value):
        value="" if value is None else str(value)
        value="".join(ch for ch in value if ch in "\t\n\r" or ord(ch)>=32)
        return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(value)}</t></is></c>'
    xml_rows=[]
    xml_rows.append('<row r="1">'+''.join(xml_cell(f"{col}1",title) for col,title in zip(columns,headers))+'</row>')
    for index,row in enumerate(rows,2):
        values=[
            row["po_id"] if "po_id" in row.keys() else (row["purchase_id"] if "purchase_id" in row.keys() else ""),
            row["item_code"] if "item_code" in row.keys() else "",
            row["item_text"],
            row["vendor_code"] if "vendor_code" in row.keys() else "",
            row["vendor_text"],
            row["document_type"],
            row["financial_year"],
            row["month"],
            row["remark"] if "remark" in row.keys() else "",
            row["serial_key"],
            row["name"],
            row["uploaded_at"].isoformat() if hasattr(row["uploaded_at"],"isoformat") else row["uploaded_at"]
        ]
        xml_rows.append(f'<row r="{index}">'+''.join(xml_cell(f"{col}{index}",value) for col,value in zip(columns,values))+'</row>')
    sheet='<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+''.join(xml_rows)+'</sheetData></worksheet>'
    files={"[Content_Types].xml":'<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
      "_rels/.rels":'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
      "xl/workbook.xml":'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Documents" sheetId="1" r:id="rId1"/></sheets></workbook>',
      "xl/_rels/workbook.xml.rels":'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
      "xl/worksheets/sheet1.xml":sheet}
    output=BytesIO()
    with ZipFile(output,"w",ZIP_DEFLATED) as book:
        for name,content in files.items(): book.writestr(name,content)
    return output.getvalue()

class Handler(SimpleHTTPRequestHandler):
    server_version = "EFileLocal/1.0"
    current_user = None
    _upload_slot = False

    def __init__(self, *args, **kwargs):
        self.current_user = None
        self._upload_slot = False
        super().__init__(*args, directory=str(ROOT), **kwargs)
    def log_message(self, fmt, *args): pass
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Content-Security-Policy", "default-src 'self' blob:; img-src 'self' blob: data:; frame-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'")
        super().end_headers()
    def send_json(self, value, code=200, headers=None):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
        for name,value in (headers or {}).items(): self.send_header(name,value)
        self.send_header("X-Content-Type-Options", "nosniff"); self.end_headers(); self.wfile.write(body)
    def read_json(self, limit=16384):
        length=int(self.headers.get("Content-Length","0"))
        if length<=0 or length>limit: raise ValueError("Request is empty or too large.")
        value=json.loads(self.rfile.read(length))
        if not isinstance(value,dict): raise ValueError("Request data must be an object.")
        return value
    def export_documents(self):
        with connect() as db: rows=db.execute("SELECT * FROM documents ORDER BY uploaded_at DESC").fetchall()
        body=make_xlsx(rows)
        self.send_response(200); self.send_header("Content-Type","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.send_header("Content-Disposition","attachment; filename=EFileMMD-Documents.xlsx"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
    def bootstrap_admin(self):
        try:
            data=self.read_json(8192)
            if data.get("password")!=data.get("confirmPassword"): raise ValueError("Passwords do not match.")
            username=validate_account(data.get("username",""),data.get("password",""))
            with DB_LOCK,local_connect() as db:
                if db.execute("SELECT count(*) FROM app_users").fetchone()[0]: return self.send_json({"error":"Admin setup is already complete. Sign in instead."},409)
                user_id=secrets.token_urlsafe(18)
                db.execute("INSERT INTO app_users(id,username,password_hash,role) VALUES(?,?,?,'admin')",(user_id,username,make_password_hash(data["password"])))
            token=create_session(user_id)
            return self.send_json({"user":{"id":user_id,"username":username,"role":"admin"}},201,{"Set-Cookie":f"efile_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}"})
        except ValueError as e: return self.send_json({"error":str(e)},400)
        except Exception as e:
            write_log("ERROR","Admin setup",f"{type(e).__name__}: {e}"); return self.send_json({"error":"Could not create the initial administrator."},500)
    def login_user(self):
        try:
            data=self.read_json(8192); username=str(data.get("username"," ")).strip().lower(); password=data.get("password","")
            with local_connect() as db: row=db.execute("SELECT id,username,password_hash,role FROM app_users WHERE username=? COLLATE NOCASE",(username,)).fetchone()
            if not row or not verify_password(password,row["password_hash"]): return self.send_json({"error":"Username or password is incorrect."},401)
            token=create_session(row["id"])
            return self.send_json({"user":{"id":row["id"],"username":row["username"],"role":row["role"]}},headers={"Set-Cookie":f"efile_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}"})
        except ValueError as e: return self.send_json({"error":str(e)},400)
        except Exception as e:
            write_log("ERROR","Sign in",f"{type(e).__name__}: {e}"); return self.send_json({"error":"Could not sign in. Try again."},500)

    def signup_user(self):
        try:
            data = self.read_json(8192)
            username = validate_account(data.get("username", ""), data.get("password", ""))
            pwd = str(data.get("password", ""))
            confirm = str(data.get("confirmPassword", ""))
            if confirm and pwd != confirm:
                raise ValueError("Passwords do not match.")
            with DB_LOCK, local_connect() as db:
                existing = db.execute("SELECT id FROM app_users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
                if existing:
                    return self.send_json({"error": "That username already exists. Please choose a different username or sign in."}, 409)
                admin_count = db.execute("SELECT count(*) FROM app_users WHERE role = 'admin'").fetchone()[0]
                role = "admin" if admin_count == 0 else "upload"
                user_id = secrets.token_urlsafe(18)
                db.execute("INSERT INTO app_users(id, username, password_hash, role) VALUES(?,?,?,?)",
                           (user_id, username, make_password_hash(pwd), role))
            token = create_session(user_id)
            return self.send_json(
                {"user": {"id": user_id, "username": username, "role": role}},
                201,
                {"Set-Cookie": f"efile_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}"}
            )
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        except Exception as e:
            write_log("ERROR", "Sign up", f"{type(e).__name__}: {e}")
            return self.send_json({"error": "Could not sign up. Try again."}, 500)

    def upload_purchase_order(self):
        action = "po_upload"
        if not request_origin_matches(self):
            write_log("ERROR", action, "PO upload rejected because origin did not match.")
            return self.send_json({"error": "Request origin rejected."}, 403)
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BYTES + 100_000:
            return self.send_json({"error": "Upload exceeds the 250 MB limit."}, 413)
        if not UPLOAD_SLOT.acquire(blocking=False):
            return self.send_json({"error": "Another upload is in progress. Please try again shortly."}, 429)
        self._upload_slot = True
        body = self.rfile.read(length)
        written_files = []
        try:
            msg = BytesParser(policy=default).parsebytes(
                (f"Content-Type: {self.headers.get('Content-Type')}\r\nMIME-Version: 1.0\r\n\r\n").encode() + body
            )
            po_data_str = None
            file_parts = {}
            for part in msg.iter_parts():
                key = part.get_param("name", header="content-disposition")
                if key == "po_data":
                    po_data_str = (part.get_payload(decode=True) or b"").decode("utf-8")
                elif key and key.startswith("file_"):
                    file_parts[key] = (part.get_filename() or "document", part.get_payload(decode=True) or b"")

            if not po_data_str:
                raise ValueError("Purchase Order data is missing.")

            po_data = json.loads(po_data_str)
            raw_po_id = str(po_data.get("po_id", "")).strip()
            if not raw_po_id:
                raise ValueError("Purchase Order (PO) ID is required.")

            items_input = po_data.get("items", [])
            if not items_input or not isinstance(items_input, list):
                raise ValueError("At least one item is required under the Purchase Order.")

            user = getattr(self, "current_user", None) or authenticated_user(self) or {"username": "anonymous"}
            self.current_user = user
            username = user.get("username", "") if isinstance(user, dict) else getattr(user, "username", "")

            file_cfg = settings().get("files", {"backend": "local"})
            if file_cfg.get("backend") == "smb":
                mount = smb_mount_path(file_cfg)
                if not mount:
                    raise ValueError("SMB share is disconnected.")
                destdir = mount / file_cfg.get("folder", "e-file MMD/documents")
            else:
                destdir = FILES
            destdir.mkdir(parents=True, exist_ok=True)

            with LOCK, DB_LOCK, connect() as c:
                existing_po = c.execute(
                    "SELECT * FROM purchase_orders WHERE po_id = ? COLLATE NOCASE", (raw_po_id,)
                ).fetchone()

                if existing_po:
                    po_record_id = existing_po["id"]
                    fin_year = existing_po["financial_year"]
                    raw_m = existing_po["month"]
                    month_num = raw_m if isinstance(raw_m, int) else (int(raw_m) if str(raw_m).isdigit() else normalize_month(raw_m)[0])
                    month_val = MONTHS[month_num - 1] if 1 <= month_num <= 12 else str(raw_m)
                    is_existing = True
                else:
                    fin_year = str(po_data.get("financial_year", "")).strip()
                    month_raw = str(po_data.get("month", "")).strip()
                    if not fin_year:
                        raise ValueError("Financial Year is required for a new Purchase Order.")
                    if not month_raw:
                        raise ValueError("Month is required for a new Purchase Order.")
                    month_num, month_val = normalize_month(month_raw)
                    po_record_id = secrets.token_urlsafe(18)
                    po_remarks = str(po_data.get("remarks", "")).strip()
                    c.execute(
                        "INSERT INTO purchase_orders(id, po_id, financial_year, month, remarks, created_by) VALUES(?,?,?,?,?,?)",
                        (po_record_id, raw_po_id, fin_year, month_num, po_remarks, username)
                    )
                    is_existing = False

                total_items_saved = 0
                total_docs_saved = 0

                for it in items_input:
                    item_code = str(it.get("item_code", "")).strip()
                    item_name = str(it.get("item_name", "")).strip()
                    vendor_code = str(it.get("vendor_code", "")).strip()
                    vendor_name = str(it.get("vendor_name", "")).strip()
                    item_remarks = str(it.get("remarks", "")).strip()
                    docs_info = it.get("documents", [])

                    if not item_code:
                        raise ValueError("Item Code is required for every item.")
                    if not item_name:
                        raise ValueError("Item Name is required for every item.")
                    if not vendor_code:
                        raise ValueError("Vendor Code is required for every item.")
                    if not vendor_name:
                        raise ValueError("Vendor Name is required for every item.")
                    if not docs_info or not isinstance(docs_info, list):
                        raise ValueError(f"At least one document is required for item '{item_name or item_code}'.")

                    item_record_id = secrets.token_urlsafe(18)
                    c.execute(
                        "INSERT INTO po_items(id, purchase_order_id, item_code, item_name, vendor_code, vendor_name, remarks) VALUES(?,?,?,?,?,?,?)",
                        (item_record_id, po_record_id, item_code, item_name, vendor_code, vendor_name, item_remarks)
                    )
                    total_items_saved += 1

                    for d_info in docs_info:
                        f_key = d_info.get("file_key")
                        doc_type = str(d_info.get("document_type", "")).strip()
                        if not doc_type:
                            raise ValueError(f"Document Type is required for all uploaded documents under '{item_name}'.")
                        if f_key not in file_parts:
                            raise ValueError(f"Uploaded file not found for document under '{item_name}'.")

                        orig_filename, content = file_parts[f_key]
                        if not content:
                            raise ValueError(f"File '{orig_filename}' is empty.")
                        if len(content) > MAX_BYTES:
                            raise ValueError(f"File '{orig_filename}' exceeds the 250 MB limit.")

                        clean_filename = clean_name(orig_filename)
                        ext = Path(clean_filename).suffix.lower()
                        if ext not in ALLOWED:
                            raise ValueError(f"File '{orig_filename}' has an unsupported file type.")

                        media = {
                            ".pdf": "application/pdf", ".doc": "application/msword",
                            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                            ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
                            ".tif": "image/tiff", ".tiff": "image/tiff"
                        }.get(ext, "application/octet-stream")

                        doc_id = secrets.token_urlsafe(18)
                        stored_name = doc_id + ext
                        dest = destdir / stored_name

                        with open(dest, "xb") as f:
                            f.write(content)
                        written_files.append(dest)

                        serial_key = new_serial_key()

                        c.execute(
                            """INSERT INTO documents(
                                id, name, stored_name, size, media_type, storage_path,
                                document_type, financial_year, month, item_text, vendor_text,
                                serial_key, purchase_id, po_id, item_code, vendor_code, remark
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                doc_id, clean_filename, stored_name, len(content), media,
                                str(dest) if file_cfg.get("backend") == "smb" else None,
                                doc_type, fin_year, month_val, item_name, vendor_name,
                                serial_key, None, raw_po_id, item_code, vendor_code, item_remarks
                            )
                        )

                        item_doc_id = secrets.token_urlsafe(18)
                        c.execute(
                            """INSERT INTO item_documents(
                                id, item_id, document_id, document_name, document_type, file_path, uploaded_by
                            ) VALUES(?,?,?,?,?,?,?)""",
                            (
                                item_doc_id, item_record_id, doc_id, clean_filename, doc_type,
                                str(dest), username
                            )
                        )
                        total_docs_saved += 1

            return self.send_json({
                "success": True,
                "po_id": raw_po_id,
                "is_existing": is_existing,
                "items_count": total_items_saved,
                "docs_count": total_docs_saved,
                "message": f"Successfully saved {total_docs_saved} document(s) for {total_items_saved} item(s) under PO '{raw_po_id}'."
            }, 201)

        except ValueError as e:
            for pth in written_files:
                pth.unlink(missing_ok=True)
            write_log("ERROR", action, str(e))
            return self.send_json({"error": str(e)}, 400)
        except Exception as e:
            for pth in written_files:
                pth.unlink(missing_ok=True)
            write_log("ERROR", action, f"{type(e).__name__}: {e}")
            return self.send_json({"error": f"Failed to save PO upload: {e}"}, 500)

    def do_GET(self):
        try: return self._do_get()
        except Exception as e:
            if urlparse(self.path).path.startswith("/api/"):
                write_log("ERROR","API read",f"{type(e).__name__}: {e}")
                return self.send_json({"error":"Storage connection failed. Check Storage settings and Activity logs."},503)
            raise
    def _do_get(self):
        p = urlparse(self.path)
        if p.path == "/api/auth/status":
            with local_connect() as db: count=db.execute("SELECT count(*) FROM app_users").fetchone()[0]
            return self.send_json({"setupRequired":count==0,"user":authenticated_user(self)})
        if p.path.startswith("/api/"):
            self.current_user=authenticated_user(self)
            if not self.current_user: return self.send_json({"error":"Sign in to continue."},401)
            if p.path in ("/api/logs","/api/users") and self.current_user["role"]!="admin": return self.send_json({"error":"Administrator access is required."},403)
            if p.path=="/api/documents/export.xlsx" and self.current_user["role"]!="admin": return self.send_json({"error":"Administrator access is required."},403)
        if p.path == "/api/purchase-orders/check":
            po_id = parse_qs(p.query).get("po_id", [""])[0].strip()
            if not po_id:
                return self.send_json({"error": "Enter a PO ID to check."}, 400)
            with connect() as c:
                row = c.execute("SELECT * FROM purchase_orders WHERE po_id = ? COLLATE NOCASE", (po_id,)).fetchone()
                if row:
                    items_count = c.execute("SELECT count(*) FROM po_items WHERE purchase_order_id = ?", (row["id"],)).fetchone()[0]
                    raw_m = row["month"]
                    month_display = MONTHS[raw_m - 1] if isinstance(raw_m, int) and 1 <= raw_m <= 12 else str(raw_m)
                    return self.send_json({
                        "exists": True,
                        "po": {
                            "id": row["id"],
                            "po_id": row["po_id"],
                            "financial_year": row["financial_year"],
                            "month": month_display,
                            "remarks": row["remarks"],
                            "created_at": row["created_at"],
                            "created_by": row["created_by"],
                            "items_count": items_count
                        }
                    })
                return self.send_json({"exists": False})
        if p.path == "/api/masters":
            cfg=settings(); return self.send_json({"masters":cfg.get("masters",DEFAULT_MASTERS)})
        if p.path == "/api/storage/settings":
            cfg=settings()
            db=cfg.get("database",{"backend":"sqlite"}); fs=cfg.get("files",{"backend":"local"})
            return self.send_json({"database":{"backend":db.get("backend","sqlite"),"host":db.get("host",""),"port":db.get("port",5432),"name":db.get("name",""),"user":db.get("user",""),"sslmode":db.get("sslmode","prefer")},
                                  "files":{"backend":fs.get("backend","local"),"host":fs.get("host",""),"share":fs.get("share",""),"folder":fs.get("folder","e-file MMD/documents")},
                                  "smbMounted":bool(smb_mount_path(fs)) if fs.get("backend")=="smb" else False})
        if p.path == "/api/storage":
            with connect() as c:
                row = c.execute("SELECT count(*), coalesce(sum(size),0) FROM documents").fetchone()
            return self.send_json({"documents": int(row[0]), "usedBytes": int(row[1] or 0), "backend":settings().get("files",{}).get("backend","local")})
        if p.path == "/api/logs":
            with local_connect() as c:
                rows = c.execute("SELECT occurred_at,level,action,message FROM app_logs ORDER BY id DESC LIMIT 200").fetchall()
            return self.send_json({"logs": [dict(timestamp=r[0], level=r[1], action=r[2], message=r[3]) for r in rows]})
        if p.path == "/api/documents/export.xlsx": return self.export_documents()
        if p.path == "/api/users":
            with local_connect() as db: rows=db.execute("SELECT id,username,role,created_at FROM app_users ORDER BY username").fetchall()
            return self.send_json({"users":[dict(id=r[0],username=r[1],role=r[2],createdAt=r[3]) for r in rows]})
        if p.path == "/api/documents":
            q = [x.strip() for x in parse_qs(p.query).get("tag", []) if x.strip()]
            with connect() as c:
                rows = c.execute("SELECT * FROM documents ORDER BY uploaded_at DESC").fetchall()
                rel = c.execute("SELECT dt.document_id,t.name FROM document_tags dt JOIN tags t ON t.id=dt.tag_id ORDER BY lower(t.name)").fetchall()
                tags = [r[0] for r in c.execute("SELECT name FROM tags ORDER BY lower(name)").fetchall()]
                by_doc={}
                for r in rel: by_doc.setdefault(r["document_id"],[]).append(r["name"])
                docs = [dict(
                    id=r["id"], name=r["name"], size=r["size"], type=r["media_type"],
                    uploadedAt=r["uploaded_at"].isoformat() if hasattr(r["uploaded_at"],"isoformat") else r["uploaded_at"],
                    serialKey=r["serial_key"], tags=by_doc.get(r["id"],[]),
                    documentType=r["document_type"], financialYear=r["financial_year"],
                    month=r["month"], itemText=r["item_text"], vendorText=r["vendor_text"],
                    po_id=r["po_id"] if "po_id" in r.keys() else (r["purchase_id"] if "purchase_id" in r.keys() else ""),
                    item_code=r["item_code"] if "item_code" in r.keys() else "",
                    vendor_code=r["vendor_code"] if "vendor_code" in r.keys() else "",
                    remark=r["remark"] if "remark" in r.keys() else ""
                ) for r in rows]
                if q:
                    wanted={x.casefold() for x in q}; docs=[d for d in docs if wanted.issubset({x.casefold() for x in d["tags"]})]
            return self.send_json({"documents": docs, "tags": tags})
        if p.path.startswith("/api/file/") or (p.path.startswith("/api/documents/") and p.path.endswith("/file")):
            if p.path.startswith("/api/file/"):
                docid = p.path.rsplit("/", 1)[-1]
            else:
                parts = p.path.strip("/").split("/")
                docid = parts[2] if len(parts) >= 4 else ""
            with connect() as c:
                row = c.execute("SELECT * FROM documents WHERE id=?", (docid,)).fetchone()
            if not row:
                self.send_response(404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<!DOCTYPE html><html><body><h2>Document Record Not Found</h2><p>The requested document could not be found in the database register.</p></body></html>")
                return

            # Multi-path search for physical file
            candidates = []
            if row["storage_path"]:
                cand_sp = Path(row["storage_path"])
                candidates.append(cand_sp)
                candidates.append(FILES / cand_sp.name)
                candidates.append(DATA / "documents" / cand_sp.name)
                candidates.append(DATA / cand_sp.name)
            if row["stored_name"]:
                candidates.append(FILES / row["stored_name"])
                candidates.append(DATA / "documents" / row["stored_name"])
                candidates.append(DATA / row["stored_name"])
            candidates.append(FILES / f"{row['id']}.pdf")
            candidates.append(DATA / "documents" / f"{row['id']}.pdf")

            file_path = None
            for cand in candidates:
                if cand and cand.is_file():
                    file_path = cand
                    break

            if not file_path:
                write_log("WARNING", "file_not_found", f"Physical file for document {docid} ({row['name']}) is missing.")
                self.send_response(404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                doc_name = row['name'] or 'Unknown Document'
                serial_key = row['serial_key'] or 'N/A'
                po_val = row['po_id'] if 'po_id' in row.keys() and row['po_id'] else (row['purchase_id'] if 'purchase_id' in row.keys() else 'N/A')
                html_err = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>File Not Found - MMD Document Register</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f8fafc; color: #1e293b; padding: 40px; display: flex; justify-content: center; align-items: center; min-height: 80vh; margin: 0; }}
        .card {{ background: #fff; max-width: 520px; width: 100%; border-radius: 12px; border: 1px solid #e2e8f0; box-shadow: 0 10px 25px -5px rgba(0,0,0,0.05); padding: 32px; box-sizing: border-box; }}
        .icon {{ width: 48px; height: 48px; border-radius: 50%; background: #fee2e2; color: #ef4444; display: flex; align-items: center; justify-content: center; font-size: 24px; font-weight: bold; margin-bottom: 20px; }}
        h1 {{ font-size: 20px; margin: 0 0 12px; color: #0f172a; }}
        p {{ margin: 0 0 16px; color: #64748b; font-size: 14px; line-height: 1.5; }}
        .meta {{ background: #f1f5f9; border-radius: 8px; padding: 14px; margin: 16px 0; font-size: 13px; line-height: 1.6; color: #334155; }}
        .meta strong {{ color: #0f172a; }}
        .btn {{ display: inline-block; background: #2563eb; color: #fff; text-decoration: none; padding: 8px 16px; border-radius: 6px; font-size: 13px; font-weight: 500; cursor: pointer; border: none; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">!</div>
        <h1>Uploaded File Not Found</h1>
        <p>The document record is registered in the database, but the physical file is currently missing or inaccessible from the storage directory.</p>
        <div class="meta">
            <div><strong>Document Name:</strong> {doc_name}</div>
            <div><strong>Unique Key:</strong> {serial_key}</div>
            <div><strong>Purchase Order ID:</strong> {po_val}</div>
        </div>
        <p>Please contact your system administrator or re-upload the document through the Purchase Order upload section.</p>
        <button class="btn" onclick="window.close()">Close Tab</button>
    </div>
</body>
</html>"""
                self.wfile.write(html_err.encode("utf-8"))
                return

            data = file_path.read_bytes()
            mime = row["media_type"] or "application/pdf"
            disposition = "inline" if mime == "application/pdf" or mime.startswith("image/") else "attachment"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f"{disposition}; filename*=UTF-8''{__import__('urllib.parse').parse.quote(row['name'])}")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return
        if p.path == "/": self.path = "/index.html"
        return super().do_GET()
    def do_POST(self):
        try: return self._do_post()
        finally:
            if getattr(self,"_upload_slot",False): UPLOAD_SLOT.release()
    def _do_post(self):
        endpoint = urlparse(self.path).path
        if not request_origin_matches(self):
            write_log("ERROR","request", "Request rejected because the origin did not match this local app.")
            return self.send_json({"error":"Request origin rejected."},403)
        if endpoint == "/api/auth/setup": return self.bootstrap_admin()
        if endpoint == "/api/auth/login": return self.login_user()
        if endpoint == "/api/auth/signup": return self.signup_user()
        self.current_user = authenticated_user(self)
        if endpoint == "/api/purchase-orders/upload": return self.upload_purchase_order()
        if endpoint == "/api/auth/logout":
            from http.cookies import SimpleCookie
            cookie=SimpleCookie(); cookie.load(self.headers.get("Cookie","")); token=cookie.get("efile_session")
            if token:
                with SESSION_LOCK: SESSIONS.pop(token.value,None)
            return self.send_json({"signedOut":True},headers={"Set-Cookie":"efile_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})
        self.current_user=authenticated_user(self)
        if not self.current_user: return self.send_json({"error":"Sign in to continue."},401)
        if (endpoint.startswith(("/api/masters","/api/storage/settings","/api/storage/smb/","/api/users","/api/documents/")) or endpoint=="/api/logs") and self.current_user["role"]!="admin":
            return self.send_json({"error":"Administrator access is required."},403)
        if endpoint == "/api/users":
            try:
                data=self.read_json(8192); username=validate_account(data.get("username",""),data.get("password","")); role=data.get("role","upload")
                if role not in ("admin","upload"): raise ValueError("Choose Admin or Upload user.")
                user_id=secrets.token_urlsafe(18)
                with DB_LOCK,local_connect() as db: db.execute("INSERT INTO app_users(id,username,password_hash,role) VALUES(?,?,?,?)",(user_id,username,make_password_hash(data["password"]),role))
                return self.send_json({"user":{"id":user_id,"username":username,"role":role}},201)
            except ValueError as e: return self.send_json({"error":str(e)},400)
            except sqlite3.IntegrityError: return self.send_json({"error":"That username already exists."},409)
            except Exception as e: write_log("ERROR","User creation",f"{type(e).__name__}: {e}"); return self.send_json({"error":"Could not create the user."},500)
        if endpoint.startswith("/api/users/") and endpoint.endswith("/delete"):
            user_id=endpoint.split("/")[-2]
            if user_id==self.current_user["id"]: return self.send_json({"error":"You cannot remove your own account."},400)
            try:
                with DB_LOCK,local_connect() as db:
                    target=db.execute("SELECT role FROM app_users WHERE id=?",(user_id,)).fetchone()
                    if not target: return self.send_json({"error":"User not found."},404)
                    admins=db.execute("SELECT count(*) FROM app_users WHERE role='admin'").fetchone()[0]
                    if target["role"]=="admin" and admins<=1: return self.send_json({"error":"Keep at least one administrator account."},400)
                    db.execute("DELETE FROM app_users WHERE id=?",(user_id,))
                with SESSION_LOCK:
                    for token,session in list(SESSIONS.items()):
                        if session[0]==user_id: SESSIONS.pop(token,None)
                return self.send_json({"deleted":True})
            except Exception as e: write_log("ERROR","User removal",f"{type(e).__name__}: {e}"); return self.send_json({"error":"Could not remove this user."},500)
        if endpoint.startswith("/api/documents/") and (endpoint.endswith("/edit") or endpoint.endswith("/delete")):
            parts=endpoint.strip("/").split("/"); doc_id=parts[2]
            if endpoint.endswith("/edit"):
                try:
                    data=self.read_json(16384)
                    values={key:re.sub(r"\s+"," ",str(data.get(key,""))).strip()[:250] for key in ("documentType","financialYear","month","itemText","vendorText","po_id","item_code","vendor_code")}
                    with connect() as db: cursor=db.execute("UPDATE documents SET document_type=?,financial_year=?,month=?,item_text=?,vendor_text=?,po_id=?,item_code=?,vendor_code=? WHERE id=?",(values["documentType"],values["financialYear"],values["month"],values["itemText"],values["vendorText"],values["po_id"],values["item_code"],values["vendor_code"],doc_id))
                    count=cursor.rowcount if hasattr(cursor,"rowcount") else cursor.cursor.rowcount
                    if count==0: return self.send_json({"error":"Document not found."},404)
                    return self.send_json({"saved":True})
                except ValueError as e: return self.send_json({"error":str(e)},400)
                except Exception as e: write_log("ERROR","Document edit",f"{type(e).__name__}: {e}"); return self.send_json({"error":"Could not update the document."},500)
            try:
                with connect() as db:
                    row=db.execute("SELECT stored_name,storage_path FROM documents WHERE id=?",(doc_id,)).fetchone()
                    if not row: return self.send_json({"error":"Document not found."},404)
                    path=Path(row["storage_path"]) if row["storage_path"] else FILES / row["stored_name"]
                    db.execute("DELETE FROM documents WHERE id=?",(doc_id,))
                try: path.unlink(missing_ok=True)
                except OSError as e: write_log("ERROR","Document delete",f"Attachment cleanup failed: {type(e).__name__}: {e}")
                return self.send_json({"deleted":True})
            except Exception as e: write_log("ERROR","Document delete",f"{type(e).__name__}: {e}"); return self.send_json({"error":"Could not delete the document."},500)
        if endpoint == "/api/logs":
            if not request_origin_matches(self):
                return self.send_json({"error":"Request origin rejected."}, 403)
            try:
                data = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", "0")), 8192)))
                action = str(data.get("action", "client"))[:40]
                message = str(data.get("message", "Client-side failure"))[:500]
                write_log("ERROR", action, message)
                return self.send_json({"saved": True}, 201)
            except Exception: return self.send_json({"error":"Could not save diagnostic log."}, 400)
        if endpoint == "/api/masters":
            try:
                body=json.loads(self.rfile.read(min(int(self.headers.get("Content-Length","0")),1048576)))
                if not isinstance(body,dict): raise ValueError("Master values must be grouped by classification.")
                raw=body.get("masters",{})
                if not isinstance(raw,dict): raise ValueError("Master values must be grouped by classification.")
                masters={}
                for field in MASTER_FIELDS:
                    values=raw.get(field,[])
                    if not isinstance(values,list) or len(values)>500: raise ValueError(f"Enter up to 500 values for {MASTER_FIELDS[field]}.")
                    clean=[]; seen=set()
                    for value in values:
                        value=re.sub(r"\s+"," ",str(value)).strip()
                        if not value or len(value)>80: raise ValueError(f"Each {MASTER_FIELDS[field]} value must contain 1–80 characters.")
                        if value.casefold() not in seen: clean.append(value); seen.add(value.casefold())
                    masters[field]=clean
                cfg=settings(); cfg["masters"]=masters; save_settings(cfg)
                return self.send_json({"saved":True,"masters":masters})
            except ValueError as e: return self.send_json({"error":str(e)},400)
            except Exception as e:
                write_log("ERROR","Classification masters",f"{type(e).__name__}: {e}")
                return self.send_json({"error":"Could not save classification masters."},500)
        if endpoint == "/api/storage/smb/connect":
            try:
                body=json.loads(self.rfile.read(min(int(self.headers.get("Content-Length","0")),8192)))
                host=str(body.get("host","")).strip(); share=str(body.get("share","")).strip()
                if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}",host) or not re.fullmatch(r"[A-Za-z0-9._$ -]{1,80}",share): raise ValueError("Enter a valid SMB server IP/host and share name.")
                cfg=settings(); cfg.setdefault("files",{}).update({"backend":"smb","host":host,"share":share,"folder":"e-file MMD/documents"})
                save_settings(cfg)
                if os.name == "nt":
                    subprocess.Popen(["explorer",f"\\\\{host}\\{share}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                elif sys_platform_is_mac():
                    subprocess.Popen(["open",f"smb://{host}/{urllib.parse.quote(share)}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else: return self.send_json({"error":"Open the SMB share in your system file manager, sign in, then retry."}, 400)
                return self.send_json({"started":True,"mounted":bool(smb_mount_path(cfg["files"]))}, 202)
            except ValueError as e: return self.send_json({"error":str(e)},400)
            except Exception as e:
                write_log("ERROR","SMB connect",f"{type(e).__name__}: {e}");return self.send_json({"error":"Could not open the SMB share in the system file manager."},500)
        if endpoint == "/api/storage/settings":
            try:
                body=json.loads(self.rfile.read(min(int(self.headers.get("Content-Length","0")),16384)))
                db=body.get("database",{}); fs=body.get("files",{})
                database={"backend":db.get("backend","sqlite")}; files={"backend":fs.get("backend","local")}
                if database["backend"] not in ("sqlite","postgres"): raise ValueError("Choose SQLite or PostgreSQL.")
                if files["backend"] not in ("local","smb"): raise ValueError("Choose local files or an SMB share.")
                if database["backend"]=="postgres":
                    database.update({"host":str(db.get("host","")).strip(),"port":int(db.get("port",5432)),"name":str(db.get("name","")).strip(),"user":str(db.get("user","")).strip(),"sslmode":str(db.get("sslmode","prefer"))})
                    password=str(db.get("password",""))
                    if not all(database[k] for k in ("host","name","user")) or not (1<=database["port"]<=65535): raise ValueError("Enter PostgreSQL host, port, database, and username.")
                    previous=settings().get("database",{})
                    if previous.get("backend")=="postgres" and any(str(previous.get(k,""))!=str(database.get(k,"")) for k in ("host","port","name","user")):
                        raise ValueError("Switch to SQLite first before changing the PostgreSQL server. This safely copies records back to local storage before you reconnect.")
                    validate_postgres(database,password)
                    try:
                        import keyring
                        keyring.set_password(secret_key(),database["user"],password)
                    except ImportError: raise ValueError("Install the network extras before configuring PostgreSQL; see README.md.")
                    except Exception as e: raise ValueError("Could not store the database password in the system credential store: "+str(e))
                if files["backend"]=="smb":
                    files.update({"host":str(fs.get("host","")).strip(),"share":str(fs.get("share","")).strip(),"folder":"e-file MMD/documents"})
                    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}",files["host"]) or not re.fullmatch(r"[A-Za-z0-9._$ -]{1,80}",files["share"]): raise ValueError("Enter a valid SMB server IP/host and share name.")
                    if not smb_mount_path(files): raise ValueError("The SMB share is not connected. Open the share in your system file manager, sign in, then retry.")
                old=settings().get("database",{}).get("backend","sqlite")
                if old=="postgres" and database["backend"]=="sqlite":
                    try: copy_postgres_to_local()
                    except Exception as e: write_log("ERROR","PostgreSQL fallback",f"Could not copy remote records; using the last local copy. {type(e).__name__}: {e}")
                updated=settings(); updated.update({"database":database,"files":files}); save_settings(updated)
                if database["backend"]=="postgres": ensure_postgres_schema()
                return self.send_json({"saved":True})
            except ValueError as e: return self.send_json({"error":str(e)},400)
            except Exception as e:
                write_log("ERROR","Storage settings",f"{type(e).__name__}: {e}");return self.send_json({"error":"Storage settings could not be saved. See Activity logs."},500)
        if endpoint != "/api/documents": return self.send_error(404)
        action = "upload"
        if not request_origin_matches(self):
            write_log("ERROR", action, "Upload rejected because the request origin did not match the local app.")
            return self.send_json({"error":"Request origin rejected."}, 403)
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BYTES + 100_000:
            message = "Upload exceeds the 250 MB limit."; write_log("ERROR", action, message)
            return self.send_json({"error":message}, 413)
        if not UPLOAD_SLOT.acquire(blocking=False): return self.send_json({"error":"Another large upload is in progress. Please try again shortly."},429)
        self._upload_slot=True
        body = self.rfile.read(length)
        try:
            message = BytesParser(policy=default).parsebytes((f"Content-Type: {self.headers.get('Content-Type')}\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
            fields = {}
            for part in message.iter_parts():
                key = part.get_param("name", header="content-disposition")
                if key == "file": fields["file"] = (part.get_filename() or "document", part.get_payload(decode=True) or b"", part.get_content_type())
                elif key == "tags": fields["tags"] = (part.get_payload(decode=True) or b"").decode("utf-8")
                elif key == "metadata": fields["metadata"] = (part.get_payload(decode=True) or b"").decode("utf-8")
            if "file" not in fields: raise ValueError("Choose a document to upload.")
            original, content, claimed_type = fields["file"]
            name = clean_name(original); ext = Path(name).suffix.lower()
            if ext not in ALLOWED: raise ValueError("Choose a PDF, DOC, DOCX, or supported image file.")
            if not content: raise ValueError("The selected file is empty.")
            if len(content) > MAX_BYTES:
                message = "The document must be 250 MB or smaller."; write_log("ERROR", action, message)
                return self.send_json({"error":message}, 413)
            tags = safe_tags(fields.get("tags", "[]"))
            metadata=json.loads(fields.get("metadata", "{}"))
            if not isinstance(metadata,dict): raise ValueError("Document classification must be an object.")
            metadata={key:re.sub(r"\s+"," ",str(metadata.get(key,"")).strip())[:250] for key in ("documentType","financialYear","month","itemText","vendorText")}
            # Use a canonical content type based on extension; do not trust browser-supplied MIME.
            media = {".pdf":"application/pdf", ".doc":"application/msword", ".docx":"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                     ".png":"image/png", ".jpg":"image/jpeg", ".jpeg":"image/jpeg", ".gif":"image/gif", ".webp":"image/webp", ".bmp":"image/bmp", ".tif":"image/tiff", ".tiff":"image/tiff"}[ext]
            if ext == ".pdf" and not content.startswith(b"%PDF-"): raise ValueError("This file does not appear to be a valid PDF.")
            if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}:
                signatures={".png":content.startswith(b"\x89PNG\r\n\x1a\n"),".jpg":content.startswith(b"\xff\xd8\xff"),".jpeg":content.startswith(b"\xff\xd8\xff"),".gif":content.startswith((b"GIF87a",b"GIF89a")),".webp":content.startswith(b"RIFF") and content[8:12]==b"WEBP",".bmp":content.startswith(b"BM"),".tif":content.startswith((b"II*\x00",b"MM\x00*")),".tiff":content.startswith((b"II*\x00",b"MM\x00*"))}
                if not signatures[ext]: raise ValueError("The file content does not match its image extension.")
            docid = secrets.token_urlsafe(18); stored = docid + ext
            file_cfg=settings().get("files",{"backend":"local"})
            if file_cfg.get("backend")=="smb":
                mount=smb_mount_path(file_cfg)
                if not mount: raise ValueError("SMB share is disconnected. Reconnect it in your system file manager, then retry.")
                destdir=mount / file_cfg.get("folder","e-file MMD/documents")
            else: destdir=FILES
            destdir.mkdir(parents=True,exist_ok=True)
            with LOCK:
                dest = destdir / stored
                with open(dest, "xb") as f: f.write(content)
                try:
                    with connect() as c:
                        serial_key=new_serial_key()
                        c.execute("INSERT INTO documents(id,name,stored_name,size,media_type,storage_path,document_type,financial_year,month,item_text,vendor_text,serial_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (docid,name,stored,len(content),media,str(dest) if file_cfg.get("backend")=="smb" else None,metadata["documentType"],metadata["financialYear"],metadata["month"],metadata["itemText"],metadata["vendorText"],serial_key))
                        for tag in tags:
                            found=c.execute("SELECT id FROM tags WHERE lower(name)=lower(?)", (tag,)).fetchone()
                            if found: tid=found["id"]
                            else:
                                c.execute("INSERT INTO tags(name) VALUES(?)", (tag,))
                                tid=c.execute("SELECT id FROM tags WHERE lower(name)=lower(?)", (tag,)).fetchone()["id"]
                            c.execute("INSERT INTO document_tags(document_id,tag_id) VALUES(?,?)", (docid,tid))
                except Exception:
                    dest.unlink(missing_ok=True); raise
            return self.send_json({"id":docid}, 201)
        except ValueError as e:
            write_log("ERROR", action, str(e)); return self.send_json({"error":str(e)}, 400)
        except Exception as e:
            write_log("ERROR", action, f"{type(e).__name__}: {e}")
            return self.send_json({"error":"Could not save this document. Check local storage and try again."}, 500)

def main():
    initialize()
    import argparse, webbrowser
    parser=argparse.ArgumentParser(description="Run the e-file MMD document register")
    parser.add_argument("--port", type=int, default=8765); parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--network", action="store_true", default=os.environ.get("EFILE_NETWORK") == "1", help="Allow other devices on the same trusted network to connect")
    args=parser.parse_args()
    bind_host="0.0.0.0" if args.network else "127.0.0.1"
    try: server=ThreadingHTTPServer((bind_host,args.port),Handler)
    except OSError as first_error:
        if args.port != 8765: raise SystemExit(f"Could not start on port {args.port}: {first_error}")
        for candidate in range(args.port + 1, args.port + 21):
            try: server=ThreadingHTTPServer((bind_host,candidate),Handler); break
            except OSError: continue
        else: raise SystemExit(f"Could not find an available local port near {args.port}: {first_error}")
    url=f"http://127.0.0.1:{server.server_port}"
    network_url=None
    if args.network:
        try:
            probe=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); probe.connect(("192.0.2.1",80)); lan_ip=probe.getsockname()[0]; probe.close()
            network_url=f"http://{lan_ip}:{server.server_port}"
        except OSError: network_url=f"http://<this-Mac-IP>:{server.server_port}"
    if server.server_port != args.port:
        print(f"Port {args.port} is already in use. This app is using {server.server_port}; close the older app window if you were using it.",flush=True)
    access=(f"Local access: {url}" if not args.network else f"This computer: {url}\nSame-network users: {network_url}\nNetwork access is enabled; use only on a trusted private network.")
    print(f"e-file MMD is ready.\n{access}\nDocuments stay in {DATA}\nPress Ctrl-C to stop.", flush=True)
    if not args.no_open: threading.Timer(.5,lambda:webbrowser.open(url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: print("\nStopped.")
    finally: server.server_close()

if __name__ == "__main__": main()
