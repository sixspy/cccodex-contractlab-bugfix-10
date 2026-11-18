
import hashlib, json, secrets, sqlite3
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}


class PromotionError(Exception):
    """晋升事务内的可预期拒绝（携带 HTTP 状态码）。"""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _promotion_fault(stage: str) -> None:
    """晋升故障注入点：生产为空操作，测试可 monkeypatch 模拟事务中途失败。"""
    return None


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # 并发晋升时让后到者在写锁上排队，而不是立刻 database is locked
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from datetime import datetime
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "入站契约"
SKELETON = "Schema 契约版本约束入站校验，破坏性变更进缓冲或拒收，并与落库结果对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS contracts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT UNIQUE NOT NULL,
          subject TEXT NOT NULL,
          mode TEXT NOT NULL,
          track TEXT NOT NULL,
          status TEXT NOT NULL,
          required_fields TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ingress_batches(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          subject TEXT NOT NULL,
          payload_fields TEXT NOT NULL,
          contract_version TEXT,
          result TEXT NOT NULL,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS buffers(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_id INTEGER NOT NULL,
          reason TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS promotion_ledger(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          subject TEXT NOT NULL,
          shadow_version TEXT NOT NULL,
          formal_version TEXT NOT NULL,
          path TEXT NOT NULL,
          result TEXT NOT NULL,
          detail TEXT NOT NULL,
          operator TEXT NOT NULL,
          created_day TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS promotion_deviations(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          subject TEXT NOT NULL,
          kind TEXT NOT NULL,
          detail TEXT NOT NULL,
          operator TEXT NOT NULL,
          created_day TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS promotion_drafts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          subject TEXT NOT NULL,
          shadow_version TEXT NOT NULL,
          formal_version TEXT NOT NULL,
          snapshot TEXT NOT NULL,
          status TEXT NOT NULL,
          operator TEXT NOT NULL,
          created_day TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        if conn.execute("SELECT COUNT(*) c FROM contracts").fetchone()["c"] == 0:
            conn.execute("INSERT INTO contracts(version,subject,mode,track,status,required_fields) VALUES (?,?,?,?,?,?)",
                         ("v3","订单入站","严格","正式","启用","order_id,amount,currency"))
            conn.execute("INSERT INTO contracts(version,subject,mode,track,status,required_fields) VALUES (?,?,?,?,?,?)",
                         ("v4-shadow","订单入站","兼容","影子","试跑","order_id,amount,currency,channel"))
            conn.execute("INSERT INTO contracts(version,subject,mode,track,status,required_fields) VALUES (?,?,?,?,?,?)",
                         ("v2","库存入站","严格","正式","停用","sku,qty"))

@app.on_event("startup")
def _s():
    init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM contracts").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "contracts": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/contracts")
def contracts(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM contracts ORDER BY id").fetchall()]}

class IngressIn(BaseModel):
    subject: str
    payload_fields: str  # comma fields present

@app.post("/api/ingress/validate")
def validate(body: IngressIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    fields = {x.strip() for x in body.payload_fields.split(",") if x.strip()}
    with connect() as c:
        # 失败闭：同主题若存在半截指针（影子启用/正式双启用），禁止按任何半截口径放必填
        split = _detect_half_pointer(c, body.subject)
        if split is not None:
            _record_deviation(body.subject, "半截偏了",
                              "入站必填读取命中半截指针，已失败闭：" + json.dumps(split, ensure_ascii=False),
                              user)
            raise HTTPException(503, "晋升半截指针未愈合，入站必填失败闭（禁止读影子必填）")
        ctr = c.execute("SELECT * FROM contracts WHERE subject=? AND track='正式' AND status='启用' ORDER BY id DESC LIMIT 1", (body.subject,)).fetchone()
        if not ctr:
            raise HTTPException(404, "无启用正式契约")
        need = {x.strip() for x in ctr["required_fields"].split(",") if x.strip()}
        missing = sorted(need - fields)
        now = datetime.now().isoformat(timespec="seconds")
        if missing:
            if ctr["mode"] == "兼容":
                cur = c.execute("INSERT INTO ingress_batches(subject,payload_fields,contract_version,result,detail,created_at) VALUES (?,?,?,?,?,?)",
                                (body.subject, body.payload_fields, ctr["version"], "缓冲", "缺字段:"+ ",".join(missing), now))
                c.execute("INSERT INTO buffers(batch_id,reason) VALUES (?,?)", (cur.lastrowid, "缺字段进双写缓冲"))
                return {"result":"缓冲", "version": ctr["version"], "missing": missing, "batch_id": cur.lastrowid}
            cur = c.execute("INSERT INTO ingress_batches(subject,payload_fields,contract_version,result,detail,created_at) VALUES (?,?,?,?,?,?)",
                            (body.subject, body.payload_fields, ctr["version"], "拒收", "缺字段:"+ ",".join(missing), now))
            return {"result":"拒收", "version": ctr["version"], "missing": missing, "batch_id": cur.lastrowid}
        cur = c.execute("INSERT INTO ingress_batches(subject,payload_fields,contract_version,result,detail,created_at) VALUES (?,?,?,?,?,?)",
                        (body.subject, body.payload_fields, ctr["version"], "通过", "字段齐全", now))
        return {"result":"通过", "version": ctr["version"], "batch_id": cur.lastrowid}

@app.get("/api/ingress")
def ingress(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM ingress_batches ORDER BY id DESC").fetchall()]}

@app.get("/api/buffers")
def buffers(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""SELECT b.*, i.subject, i.payload_fields FROM buffers b JOIN ingress_batches i ON i.id=b.batch_id ORDER BY b.id DESC""").fetchall()
        return {"items":[dict(r) for r in rows]}

@app.get("/api/reconcile")
def reconcile(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        total = c.execute("SELECT COUNT(*) c FROM ingress_batches").fetchone()["c"]
        passed = c.execute("SELECT COUNT(*) c FROM ingress_batches WHERE result='通过'").fetchone()["c"]
        buffered = c.execute("SELECT COUNT(*) c FROM ingress_batches WHERE result='缓冲'").fetchone()["c"]
        rejected = c.execute("SELECT COUNT(*) c FROM ingress_batches WHERE result='拒收'").fetchone()["c"]
        buf_rows = c.execute("SELECT COUNT(*) c FROM buffers").fetchone()["c"]
    return {"ingress_total": total, "passed": passed, "buffered": buffered, "rejected": rejected, "buffer_rows": buf_rows, "aligned": buffered == buf_rows}


# ---------------- 影子晋升正式：整笔事务，禁止半截指针 ----------------

def _today() -> str:
    return datetime.now().date().isoformat()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _formal_version_of(shadow_version: str) -> str:
    return shadow_version[:-7] if shadow_version.endswith("-shadow") else shadow_version


def _detect_half_pointer(conn, subject: str) -> dict | None:
    """检测同主题是否存在半截指针：影子启用、或正式双启用。"""
    rows = conn.execute(
        "SELECT version,track,status FROM contracts WHERE subject=? AND status='启用'",
        (subject,)).fetchall()
    shadow_enabled = [r["version"] for r in rows if r["track"] == "影子"]
    formal_enabled = [r["version"] for r in rows if r["track"] == "正式"]
    if shadow_enabled or len(formal_enabled) > 1:
        return {"shadow_enabled": shadow_enabled, "formal_enabled": formal_enabled}
    return None


def _record_deviation(subject: str, kind: str, detail: str, operator: str) -> None:
    with connect() as c:
        c.execute("INSERT INTO promotion_deviations(subject,kind,detail,operator,created_day,created_at)"
                  " VALUES (?,?,?,?,?,?)", (subject, kind, detail, operator, _today(), _now()))
        c.commit()


def _record_ledger_fail(subject: str, shadow_version: str, formal_version: str,
                        path: str, detail: str, operator: str) -> None:
    """事务回滚后单独留失败闭册（不留任何指针改动）。"""
    with connect() as c:
        c.execute("INSERT INTO promotion_ledger(subject,shadow_version,formal_version,path,result,detail,"
                  "operator,created_day,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (subject, shadow_version, formal_version, path, "失败", detail,
                   operator, _today(), _now()))
        c.commit()


def _selected_path_today(conn, subject: str) -> str | None:
    # 只认成功晋升与待确认/已确认草稿：失败闭册不锁路径，允许按同一口径重试
    row = conn.execute(
        "SELECT path FROM promotion_ledger WHERE subject=? AND result='成功' AND created_day=?"
        " ORDER BY id LIMIT 1",
        (subject, _today())).fetchone()
    if row:
        return row["path"]
    drow = conn.execute(
        "SELECT id FROM promotion_drafts WHERE subject=? AND created_day=? AND status='待确认' LIMIT 1",
        (subject, _today())).fetchone()
    return "乙" if drow else None


def _promote_in_tx(conn, shadow_row, path: str, operator: str) -> dict:
    """晋升整笔事务：影子停用 + 正式启用 + 必填切换必须同成同败。

    甲路径：影子停用、正式启用与必填切换在同一事务内完成；
    乙路径：草稿快照确认后，仍由本函数原子切正式指针。
    两条路径共用同一个原子切换内核，杜绝混用半截支。
    """
    subject = shadow_row["subject"]
    shadow_version = shadow_row["version"]
    formal_version = _formal_version_of(shadow_version)

    # 同主题当天只允许一个成功晋升；后到者排队只记争用（先于版本冲突判定）
    exists = conn.execute(
        "SELECT 1 FROM promotion_ledger WHERE subject=? AND result='成功' AND created_day=?",
        (subject, _today())).fetchone()
    if exists:
        raise PromotionError(409, "同主题今日已有晋升成功，本次只记争用")

    if conn.execute("SELECT 1 FROM contracts WHERE version=?", (formal_version,)).fetchone():
        raise PromotionError(409, f"正式版本 {formal_version} 已存在，禁止重复晋升")

    # 同主题若存在半截指针，先由本事务整笔愈合，不允许在分裂态上再叠半截
    split = _detect_half_pointer(conn, subject)
    conn.execute("UPDATE contracts SET status='停用' WHERE subject=? AND track='影子' AND status='启用'",
                 (subject,))

    cur = conn.execute(
        "INSERT INTO contracts(version,subject,mode,track,status,required_fields)"
        " VALUES (?,?,?,?,?,?)",
        (formal_version, subject, shadow_row["mode"], "正式", "启用", shadow_row["required_fields"]))
    _promotion_fault("after_formal_insert")
    conn.execute("UPDATE contracts SET status='停用' WHERE subject=? AND track='正式' AND id<>?",
                 (subject, cur.lastrowid))
    _promotion_fault("after_shadow_disable")
    conn.execute("UPDATE contracts SET status='停用' WHERE version=?", (shadow_version,))

    detail = "晋升成功" if split is None else "晋升成功（事务内愈合半截指针）"
    conn.execute("INSERT INTO promotion_ledger(subject,shadow_version,formal_version,path,result,detail,"
                 "operator,created_day,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (subject, shadow_version, formal_version, path, "成功", detail,
                  operator, _today(), _now()))
    if split is not None:
        conn.execute("INSERT INTO promotion_deviations(subject,kind,detail,operator,created_day,created_at)"
                     " VALUES (?,?,?,?,?,?)",
                     (subject, "半截愈合", json.dumps(split, ensure_ascii=False),
                      operator, _today(), _now()))
    return {"subject": subject, "shadow_version": shadow_version,
            "formal_version": formal_version, "path": path}


class PromoteIn(BaseModel):
    version: str
    path: str = "甲"          # 甲=同事务直切；乙=草稿快照确认后原子切
    operator: str | None = None


class DraftIn(BaseModel):
    version: str
    operator: str | None = None


@app.post("/api/contracts/promote")
def promote(body: PromoteIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    operator = body.operator or user
    if body.path not in ("甲", "乙"):
        raise HTTPException(400, "晋升路径只允许甲或乙")
    conn = connect()
    subject = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        shadow_row = conn.execute("SELECT * FROM contracts WHERE version=? AND track='影子'",
                                  (body.version,)).fetchone()
        if not shadow_row:
            raise PromotionError(404, f"影子契约 {body.version} 不存在")
        subject = shadow_row["subject"]

        chosen = _selected_path_today(conn, subject)
        if chosen is not None and chosen != body.path:
            raise PromotionError(409, f"今日晋升路径已选定{chosen}，禁止换径（半截偏离）")

        out = _promote_in_tx(conn, shadow_row, body.path, operator)
        conn.commit()
        return {"result": "成功", **out}
    except PromotionError as e:
        conn.rollback()
        if e.status_code == 409 and ("争用" in str(e) or "已存在" in str(e)):
            _record_deviation(subject, "争用", str(e), operator)
        elif "半截偏离" in str(e):
            _record_deviation(subject, "半截偏离", str(e), operator)
        raise HTTPException(e.status_code, str(e))
    except Exception as e:  # 整笔同败：任何中途故障都全量回滚，只留失败闭册
        conn.rollback()
        detail = f"晋升事务失败已整笔回滚：{e}"
        _record_ledger_fail(subject or "未知主题", body.version,
                            _formal_version_of(body.version), body.path, detail, operator)
        raise HTTPException(500, detail)
    finally:
        conn.close()


@app.post("/api/contracts/promotion-drafts")
def create_draft(body: DraftIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    operator = body.operator or user
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        shadow_row = conn.execute("SELECT * FROM contracts WHERE version=? AND track='影子'",
                                  (body.version,)).fetchone()
        if not shadow_row:
            raise HTTPException(404, f"影子契约 {body.version} 不存在")
        subject = shadow_row["subject"]
        chosen = _selected_path_today(conn, subject)
        if chosen is not None and chosen != "乙":
            _record_deviation(subject, "半截偏离", f"今日路径已选定{chosen}，禁止改走乙径", operator)
            raise HTTPException(409, f"今日晋升路径已选定{chosen}，禁止换径（半截偏离）")
        dup = conn.execute("SELECT 1 FROM promotion_drafts WHERE subject=? AND status='待确认' AND created_day=?",
                           (subject, _today())).fetchone()
        if dup:
            _record_deviation(subject, "争用", "同主题已有待确认草稿", operator)
            raise HTTPException(409, "同主题今日已有晋升草稿，本次只记争用")
        formal_version = _formal_version_of(shadow_row["version"])
        cur = conn.execute(
            "INSERT INTO promotion_drafts(subject,shadow_version,formal_version,snapshot,status,operator,"
            "created_day,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (subject, shadow_row["version"], formal_version,
             json.dumps(dict(shadow_row), ensure_ascii=False), "待确认",
             operator, _today(), _now()))
        conn.commit()
        return {"draft_id": cur.lastrowid, "path": "乙",
                "shadow_version": shadow_row["version"], "formal_version": formal_version}


@app.post("/api/contracts/promotion-drafts/{draft_id}/confirm")
def confirm_draft(draft_id: int, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    conn = connect()
    draft = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        draft = conn.execute("SELECT * FROM promotion_drafts WHERE id=?", (draft_id,)).fetchone()
        if not draft:
            raise PromotionError(404, f"晋升草稿 {draft_id} 不存在")
        if draft["status"] != "待确认":
            raise PromotionError(409, f"草稿状态为{draft['status']}，禁止重复确认")
        shadow_row = conn.execute("SELECT * FROM contracts WHERE version=? AND track='影子'",
                                  (draft["shadow_version"],)).fetchone()
        if not shadow_row:
            raise PromotionError(404, f"影子契约 {draft['shadow_version']} 已不存在")
        chosen = _selected_path_today(conn, draft["subject"])
        if chosen is not None and chosen != "乙":
            raise PromotionError(409, "今日晋升路径已偏离乙径（半截偏离）")
        out = _promote_in_tx(conn, shadow_row, "乙", user)
        conn.execute("UPDATE promotion_drafts SET status='已确认' WHERE id=?", (draft_id,))
        conn.commit()
        return {"result": "成功", **out}
    except PromotionError as e:
        conn.rollback()
        if e.status_code == 409 and draft is not None:
            kind = "半截偏离" if "偏离" in str(e) else "争用"
            _record_deviation(draft["subject"], kind, str(e), user)
        raise HTTPException(e.status_code, str(e))
    except Exception as e:
        conn.rollback()
        subject = draft["subject"] if draft else "未知主题"
        shadow_v = draft["shadow_version"] if draft else "未知版本"
        formal_v = draft["formal_version"] if draft else "未知版本"
        _record_ledger_fail(subject, shadow_v, formal_v, "乙",
                            f"草稿确认原子切失败已整笔回滚：{e}", user)
        raise HTTPException(500, f"草稿确认原子切失败已整笔回滚：{e}")
    finally:
        conn.close()


@app.get("/api/promotion-ledger")
def promotion_ledger(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute(
            "SELECT * FROM promotion_ledger ORDER BY id DESC").fetchall()]}


@app.get("/api/promotion-deviations")
def promotion_deviations(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute(
            "SELECT * FROM promotion_deviations ORDER BY id DESC").fetchall()]}


@app.get("/api/promotion-policy")
def promotion_policy(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        subjects = [r["subject"] for r in c.execute(
            "SELECT DISTINCT subject FROM contracts").fetchall()]
        split = {s: _detect_half_pointer(c, s) for s in subjects}
        split = {s: v for s, v in split.items() if v}
        # 当日选定路径：取今天成功闭册或待确认草稿里时间最新的一条
        row = c.execute(
            "SELECT path FROM ("
            " SELECT path, created_at FROM promotion_ledger WHERE result='成功' AND created_day=?"
            " UNION ALL SELECT '乙' AS path, created_at FROM promotion_drafts"
            " WHERE status='待确认' AND created_day=?"
            ") ORDER BY created_at DESC LIMIT 1", (_today(), _today())).fetchone()
        selected = row["path"] if row else None
    return {
        "selected_path": selected,
        "rules": ["晋升整笔同成同败", "甲/乙路径当天选定不许换",
                  "禁止晋升成功仍读影子必填", "禁止正式影子双启用",
                  "同主题并发晋升排队，后到只记争用"],
        "half_pointer_free": len(split) == 0,
        "splits": split,
    }


@app.get("/api/contracts/by-version/{version}")
def contract_by_version(version: str, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        row = c.execute("SELECT * FROM contracts WHERE version=?", (version,)).fetchone()
        if not row:
            raise HTTPException(404, "契约版本不存在")
        return dict(row)


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
