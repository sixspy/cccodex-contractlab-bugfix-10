"""晋升半截夹具：复现“影子晋升正式时指针半截”缺陷。

半截分裂态（由一次非原子的影子晋升中途失败留下）：
- 目录面：v4-shadow 仍在影子轨，却被标成“启用”（目录显示启用）；
- 必填面：入站校验读到的启用正式契约指针没切，必填仍来自旧影子稿语义，
  即同主题正式轨 v3 停用、影子轨反而启用，正式/影子指针各走一半。

另含第二个同主题影子稿 v5-shadow，用于复现“双启用”与并发争用。
本模块只造数据，不包含修复逻辑；修复必须走晋升事务（见 backend/app.py）。
"""

import sqlite3

HALF_SUBJECT = "订单入站"


def _open(db_path):
    # 优先经应用连接工厂打开：测试沙箱仅放行应用自身的库写路径。
    try:
        import sys

        backend = __import__("pathlib").Path(__file__).resolve().parents[1] / "backend"
        if str(backend) not in sys.path:
            sys.path.insert(0, str(backend))
        from app import connect as app_connect  # noqa: WPS433

        return app_connect()
    except Exception:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn


def seed_half_promoted(db_path) -> str:
    """把 db_path 指向的库写成晋升半截分裂态，返回主题名。"""
    conn = _open(db_path)
    try:
        conn.execute(
            "INSERT INTO contracts(version,subject,mode,track,status,required_fields)"
            " VALUES (?,?,?,?,?,?)",
            ("v3", HALF_SUBJECT, "严格", "正式", "停用", "order_id,amount,currency"),
        )
        # 半截点：影子稿在影子轨被置“启用”，正式指针却没切（目录与必填分裂）
        conn.execute(
            "INSERT INTO contracts(version,subject,mode,track,status,required_fields)"
            " VALUES (?,?,?,?,?,?)",
            ("v4-shadow", HALF_SUBJECT, "兼容", "影子", "启用",
             "order_id,amount,currency,channel"),
        )
        # 第二份影子稿：并发/双启用场景用
        conn.execute(
            "INSERT INTO contracts(version,subject,mode,track,status,required_fields)"
            " VALUES (?,?,?,?,?,?)",
            ("v5-shadow", HALF_SUBJECT, "兼容", "影子", "试跑",
             "order_id,amount,currency,channel,tenant"),
        )
        conn.commit()
    finally:
        conn.close()
    return HALF_SUBJECT


def describe_split(conn) -> dict:
    """读出分裂态证据：目录启用指针与入站必填指针各指向谁。"""
    conn.row_factory = sqlite3.Row
    catalog_enabled = [dict(r) for r in conn.execute(
        "SELECT version,track,status,required_fields FROM contracts"
        " WHERE subject=? AND status='启用' ORDER BY id", (HALF_SUBJECT,))]
    formal_enabled = conn.execute(
        "SELECT version,required_fields FROM contracts"
        " WHERE subject=? AND track='正式' AND status='启用'", (HALF_SUBJECT,)).fetchall()
    return {
        "catalog_enabled": catalog_enabled,
        "formal_enabled": [dict(r) for r in formal_enabled],
    }
