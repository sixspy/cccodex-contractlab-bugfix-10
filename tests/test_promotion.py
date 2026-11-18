"""晋升半截修复的失败闭测试。

顺序约束：本文件先于修复落地，先红后绿。
覆盖口径：
- 半截夹具复现“目录显示启用但必填仍是影子稿”的分裂；
- 晋升必须整笔同成同败，注入故障即整笔回滚，失败入晋升失败闭册；
- 禁止晋升成功仍读影子必填、禁止正式影子双启用；
- 同主题并发晋升排队，后到只记争用；
- 甲/乙路径当天选定即入册，不许换径，偏离记“半截偏离”；
- 版本目录、入站列表、详情、对账账册、策略附页均无半截指针。
"""

import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import app, init_db, connect, DB_PATH
from half_promoted_split import seed_half_promoted, describe_split, HALF_SUBJECT

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _client_token(username="admin", password="123456"):
    c = TestClient(app)
    tok = c.post("/api/login", json={"username": username, "password": password}).json()["token"]
    return c, {"Authorization": f"Bearer {tok}"}


def _reset_to_split():
    """清空契约表并写入晋升半截夹具态。"""
    with connect() as conn:
        conn.execute("DELETE FROM contracts")
        conn.commit()
    seed_half_promoted(DB_PATH)


def _all_contracts():
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT version,subject,mode,track,status,required_fields FROM contracts ORDER BY id")]


def _enabled(subject=HALF_SUBJECT):
    return [r for r in _all_contracts() if r["subject"] == subject and r["status"] == "启用"]


# ---- 1. 夹具先复现分裂 ----------------------------------------------------

def test_fixture_reproduces_catalog_required_split():
    _reset_to_split()
    with connect() as conn:
        split = describe_split(conn)
    # 目录面：影子稿被标“启用”
    assert [r["version"] for r in split["catalog_enabled"]] == ["v4-shadow"]
    assert split["catalog_enabled"][0]["track"] == "影子"
    # 必填面：正式轨没有任何启用契约，入站必填指针根本没切过来
    assert split["formal_enabled"] == []


def test_validate_fails_closed_on_half_pointer():
    _reset_to_split()
    c, h = _client_token()
    r = c.post("/api/ingress/validate", headers=h,
               json={"subject": HALF_SUBJECT, "payload_fields": "order_id,amount,currency,channel"})
    # 分裂态下禁止拿影子稿当必填口径，必须失败闭（而不是放行/读影子必填）
    assert r.status_code in (409, 503)
    assert "半截" in r.json()["detail"]


# ---- 2. 甲路径：同一事务整笔切换 ------------------------------------------

def test_path_jia_atomic_switch_heals_split():
    _reset_to_split()
    c, h = _client_token()
    r = c.post("/api/contracts/promote", headers=h,
               json={"version": "v4-shadow", "path": "甲"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"] == "成功" and body["path"] == "甲"

    enabled = _enabled()
    assert len(enabled) == 1                       # 禁止双启用
    formal = enabled[0]
    assert formal["track"] == "正式"               # 指针落在正式轨
    assert formal["required_fields"] == "order_id,amount,currency,channel"  # 必填随整笔切来
    # 影子稿必须停用，不存在任何“影子+启用”的半截行
    assert not [r for r in _all_contracts() if r["track"] == "影子" and r["status"] == "启用"]

    # 入站必填已切到正式稿：缺 channel 必须被正式口径拦下
    r2 = c.post("/api/ingress/validate", headers=h,
                json={"subject": HALF_SUBJECT, "payload_fields": "order_id,amount,currency"})
    assert r2.status_code == 200
    assert r2.json()["version"] == formal["version"]
    assert "channel" in r2.json()["missing"]

    ledger = c.get("/api/promotion-ledger", headers=h).json()["items"]
    assert len(ledger) == 1 and ledger[0]["result"] == "成功" and ledger[0]["path"] == "甲"


def test_promote_failure_rolls_back_whole_transaction(monkeypatch):
    _reset_to_split()
    before = _all_contracts()

    import app as appmod

    def boom(stage):
        if stage == "after_formal_insert":
            raise RuntimeError("注入故障：正式行已插、影子未停")

    monkeypatch.setattr(appmod, "_promotion_fault", boom)
    c, h = _client_token()
    r = c.post("/api/contracts/promote", headers=h,
               json={"version": "v4-shadow", "path": "甲"})
    assert r.status_code == 500
    assert "回滚" in r.json()["detail"]

    # 整笔同败：契约表与故障前逐行一致，没有留下新的半截指针
    assert _all_contracts() == before
    assert len(_enabled()) == 1 and _enabled()[0]["track"] == "影子"

    # 失败闭册留痕
    ledger = c.get("/api/promotion-ledger", headers=h).json()["items"]
    assert len(ledger) == 1 and ledger[0]["result"] == "失败" and "回滚" in ledger[0]["detail"]


# ---- 3. 正常态晋升：禁止双启用、禁止成功后读影子必填 ------------------------

def test_normal_promotion_no_dual_enable_no_shadow_read():
    c, h = _client_token()  # init_db 种子：v3 正式启用 / v4-shadow 影子试跑
    r = c.post("/api/contracts/promote", headers=h, json={"version": "v4-shadow"})
    assert r.status_code == 200, r.text
    enabled = _enabled()
    assert len(enabled) == 1 and enabled[0]["track"] == "正式"
    rows = {r["version"]: r for r in _all_contracts()}
    assert rows["v3"]["status"] == "停用"
    assert rows["v4-shadow"]["status"] == "停用"

    r2 = c.post("/api/ingress/validate", headers=h,
                json={"subject": "订单入站", "payload_fields": "order_id,amount,currency"})
    assert r2.json()["version"] == enabled[0]["version"]
    assert "channel" in r2.json()["missing"]      # 不再读旧正式 v3 的必填口径


def test_same_day_second_promotion_recorded_as_contention():
    c, h = _client_token()
    assert c.post("/api/contracts/promote", headers=h, json={"version": "v4-shadow"}).status_code == 200
    # 同主题当天再抢晋升：拒绝且只记争用，不产生第二次切换
    r = c.post("/api/contracts/promote", headers=h, json={"version": "v4-shadow"})
    assert r.status_code == 409 and "争用" in r.json()["detail"]
    assert len(_enabled()) == 1
    dev = c.get("/api/promotion-deviations", headers=h).json()["items"]
    assert any(d["kind"] == "争用" for d in dev)


# ---- 4. 同主题并发晋升排队 ------------------------------------------------

def test_concurrent_promotions_queue_with_one_contention():
    barrier = threading.Barrier(2)
    results = {}

    def worker(name, creds):
        c, _ = _client_token(*creds)
        h = {"Authorization": f"Bearer {c.post('/api/login', json={'username': creds[0], 'password': creds[1]}).json()['token']}"}
        barrier.wait()
        results[name] = c.post("/api/contracts/promote", headers=h,
                               json={"version": "v4-shadow", "operator": name}).status_code

    t1 = threading.Thread(target=worker, args=("值班甲", ("admin", "123456")))
    t2 = threading.Thread(target=worker, args=("值班乙", ("viewer", "view123")))
    t1.start(); t2.start(); t1.join(15); t2.join(15)
    assert sorted(results.values()) == [200, 409]
    assert len(_enabled()) == 1 and _enabled()[0]["track"] == "正式"

    c, h = _client_token()
    ledger = c.get("/api/promotion-ledger", headers=h).json()["items"]
    assert [x["result"] for x in ledger].count("成功") == 1
    dev = c.get("/api/promotion-deviations", headers=h).json()["items"]
    assert any(d["kind"] == "争用" for d in dev)


# ---- 5. 乙路径：草稿快照 + 确认后原子切；当天不许换径 ----------------------

def test_path_yi_draft_then_atomic_confirm():
    c, h = _client_token()
    r = c.post("/api/contracts/promotion-drafts", headers=h,
               json={"version": "v4-shadow"})
    assert r.status_code == 200, r.text
    draft_id = r.json()["draft_id"]

    # 草稿阶段不得动任何指针：目录仍是晋升前形态
    rows = {r["version"]: r for r in _all_contracts()}
    assert rows["v3"]["status"] == "启用" and rows["v4-shadow"]["status"] == "试跑"

    r2 = c.post(f"/api/contracts/promotion-drafts/{draft_id}/confirm", headers=h)
    assert r2.status_code == 200, r2.text
    enabled = _enabled()
    assert len(enabled) == 1 and enabled[0]["track"] == "正式"
    assert enabled[0]["required_fields"] == "order_id,amount,currency,channel"
    ledger = c.get("/api/promotion-ledger", headers=h).json()["items"]
    assert ledger[0]["path"] == "乙" and ledger[0]["result"] == "成功"


def test_path_cannot_switch_same_day_deviation_recorded():
    c, h = _client_token()
    # 先立乙草稿（选定乙径入册）
    assert c.post("/api/contracts/promotion-drafts",
                  headers=h, json={"version": "v4-shadow"}).status_code == 200
    # 当天改走甲路径：拒绝并记半截偏离
    r = c.post("/api/contracts/promote", headers=h,
               json={"version": "v4-shadow", "path": "甲"})
    assert r.status_code == 409 and "半截偏离" in r.json()["detail"]
    dev = c.get("/api/promotion-deviations", headers=h).json()["items"]
    assert any(d["kind"] == "半截偏离" for d in dev)
    # 指针未被任何半截动作触碰
    rows = {r["version"]: r for r in _all_contracts()}
    assert rows["v3"]["status"] == "启用" and rows["v4-shadow"]["status"] == "试跑"


def test_yi_confirm_failure_full_rollback(monkeypatch):
    import app as appmod
    c, h = _client_token()
    draft_id = c.post("/api/contracts/promotion-drafts",
                      headers=h, json={"version": "v4-shadow"}).json()["draft_id"]
    before = _all_contracts()

    def boom(stage):
        if stage == "after_shadow_disable":
            raise RuntimeError("注入故障：确认切换中途")

    monkeypatch.setattr(appmod, "_promotion_fault", boom)
    r = c.post(f"/api/contracts/promotion-drafts/{draft_id}/confirm", headers=h)
    assert r.status_code == 500
    assert _all_contracts() == before
    ledger = c.get("/api/promotion-ledger", headers=h).json()["items"]
    assert any(x["result"] == "失败" for x in ledger)


# ---- 6. 所有视图均无半截指针 ----------------------------------------------

def test_all_views_free_of_half_pointer_after_promotion():
    c, h = _client_token()
    assert c.post("/api/contracts/promote", headers=h,
                  json={"version": "v4-shadow"}).status_code == 200

    listing = c.get("/api/contracts", headers=h).json()["items"]
    assert not [x for x in listing if x["track"] == "影子" and x["status"] == "启用"]
    detail = c.get("/api/contracts/by-version/v4", headers=h)
    assert detail.status_code == 200 and detail.json()["track"] == "正式"
    ingress = c.get("/api/ingress", headers=h).json()["items"]
    assert all(x["contract_version"] != "v4-shadow" for x in ingress)
    recon = c.get("/api/reconcile", headers=h).json()
    assert recon["aligned"] is True
    policy = c.get("/api/promotion-policy", headers=h).json()
    assert policy["selected_path"] in ("甲", "乙")
    assert policy["half_pointer_free"] is True


def test_promote_unknown_version_rejected():
    c, h = _client_token()
    r = c.post("/api/contracts/promote", headers=h, json={"version": "nope-shadow"})
    assert r.status_code == 404
    assert c.get("/api/promotion-ledger", headers=h).json()["items"] == []


def test_failure_does_not_lock_path_retry_succeeds(monkeypatch):
    import app as appmod
    c, h = _client_token()

    calls = {"n": 0}

    def flaky(stage):
        if stage == "after_formal_insert":
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("首次注入故障")

    monkeypatch.setattr(appmod, "_promotion_fault", flaky)
    r1 = c.post("/api/contracts/promote", headers=h, json={"version": "v4-shadow", "path": "甲"})
    assert r1.status_code == 500
    # 失败闭册不应锁死路径，同口径重试成功
    r2 = c.post("/api/contracts/promote", headers=h, json={"version": "v4-shadow", "path": "甲"})
    assert r2.status_code == 200, r2.text
    ledger = c.get("/api/promotion-ledger", headers=h).json()["items"]
    results = [x["result"] for x in ledger]
    assert results.count("失败") == 1 and results.count("成功") == 1
    assert len(_enabled()) == 1
