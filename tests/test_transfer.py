"""站点间读数转移 API 的业务规则测试。"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

DAY = "2025-04-01"
SRC = "SRC"
DST = "DST"


@pytest.fixture()
def client(tmp_path):
    app = create_app(Settings(database_path=str(tmp_path / "test.db")))
    with TestClient(app) as c:
        yield c


def rec(hour, value=None, revision=1, retracted=False, station=SRC, date=DAY):
    body = {
        "station_id": station,
        "date": date,
        "hour": hour,
        "revision": revision,
        "retracted": retracted,
    }
    if not retracted:
        body["value"] = value
    return body


def batch(client, records):
    return client.post("/api/v1/readings/batch", json={"records": records})


def daily(client, station, date=DAY):
    return client.get(f"/api/v1/stations/{station}/daily", params={"date": date}).json()


def transfer(client, op_id, hours, source=SRC, target=DST, date=DAY):
    return client.post(
        "/api/v1/readings/transfer",
        json={
            "operation_id": op_id,
            "source_station_id": source,
            "target_station_id": target,
            "date": date,
            "hours": hours,
        },
    )


def hour_item(hour, revision=1):
    return {"hour": hour, "source_revision": revision}


def seed_full_day(client, station=SRC, value=10.0, date=DAY):
    res = batch(client, [rec(h, value, station=station, date=date) for h in range(24)])
    assert res.status_code == 200, res.text


# ---------------------------------------------------------------- 基本转移
def test_healthz_still_ok(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_transfer_moves_reading_and_appends_retraction(client):
    seed_full_day(client)
    res = transfer(client, "op-1", [hour_item(3)])
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "applied"
    assert body["replayed"] is False
    assert body["transferred_hours"] == [3]
    assert body["transferred_count"] == 1
    assert body["hours"] == [
        {
            "hour": 3,
            "value": 10.0,
            "source_revision_before": 1,
            "source_retraction_revision": 2,
            "target_revision": 1,
        }
    ]

    src = daily(client, SRC)
    dst = daily(client, DST)
    # 源站：小时 3 被更高修订号撤回，历史修订仍保留
    assert src["hours"][3]["state"] == "retracted"
    assert src["hours"][3]["revision"] == 2
    assert src["valid_hours"] == 23
    assert src["retracted_hours"] == [3]
    # 目标站：原浓度以 revision=1 入库
    assert dst["hours"][3]["state"] == "valid"
    assert dst["hours"][3]["value"] == 10.0
    assert dst["hours"][3]["revision"] == 1
    assert dst["valid_hours"] == 1


def test_transfer_preserves_revision_history(client):
    seed_full_day(client)
    batch(client, [rec(5, 77.0, revision=2)])  # 源站先有一次校准修订
    res = transfer(client, "op-hist", [hour_item(5, revision=2)])
    assert res.status_code == 200, res.text
    assert res.json()["hours"][0]["source_retraction_revision"] == 3
    assert res.json()["hours"][0]["value"] == 77.0

    src = daily(client, SRC)
    assert src["hours"][5]["state"] == "retracted"
    assert src["hours"][5]["revision"] == 3
    # 历史修订行没有被覆盖：直接查库确认三个修订都在
    import sqlite3

    conn = sqlite3.connect(client.app.state.database.path)
    rows = conn.execute(
        "SELECT revision, value, retracted FROM pm25_readings "
        "WHERE station_id=? AND obs_date=? AND hour=5 ORDER BY revision",
        (SRC, DAY),
    ).fetchall()
    conn.close()
    assert rows == [(1, 10.0, 0), (2, 77.0, 0), (3, None, 1)]


def test_transfer_multiple_hours_atomic_and_sorted(client):
    seed_full_day(client, value=5.0)
    res = transfer(client, "op-multi", [hour_item(7), hour_item(2), hour_item(11)])
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["transferred_hours"] == [2, 7, 11]
    assert [h["hour"] for h in body["hours"]] == [2, 7, 11]
    assert all(h["value"] == 5.0 for h in body["hours"])

    src = daily(client, SRC)
    assert src["valid_hours"] == 21
    assert src["retracted_hours"] == [2, 7, 11]
    dst = daily(client, DST)
    assert dst["valid_hours"] == 3
    assert dst["hours"][2]["value"] == 5.0
    assert dst["hours"][7]["value"] == 5.0
    assert dst["hours"][11]["value"] == 5.0


def test_daily_reports_reflect_transfer_immediately(client):
    seed_full_day(client, value=8.0)
    assert daily(client, SRC)["daily_mean"] == 8.0
    transfer(client, "op-imm", [hour_item(0), hour_item(1)])
    src = daily(client, SRC)
    dst = daily(client, DST)
    # 立即生效，无缓存
    assert src["valid_hours"] == 22
    assert round(sum(8.0 for _ in range(22)) / 22, 2) == src["daily_mean"]
    assert dst["daily_mean"] is None  # 仅 2 个有效小时，不足 20
    assert dst["status"] == "insufficient_valid_hours"


# ---------------------------------------------------------------- 幂等与操作号冲突
def test_same_operation_id_same_content_replays_original_result(client):
    seed_full_day(client)
    first = transfer(client, "op-idem", [hour_item(4)])
    second = transfer(client, "op-idem", [hour_item(4)])
    assert first.status_code == second.status_code == 200
    assert {**first.json(), "replayed": True} == second.json()
    assert second.json()["replayed"] is True

    # 重放没有副作用：源站仍只撤回一次（修订停在 2），目标站只有一条 revision=1
    src = daily(client, SRC)
    dst = daily(client, DST)
    assert src["hours"][4]["revision"] == 2
    assert dst["hours"][4]["revision"] == 1

    import sqlite3

    conn = sqlite3.connect(client.app.state.database.path)
    op_count = conn.execute(
        "SELECT COUNT(*) FROM pm25_transfer_operations WHERE operation_id='op-idem'"
    ).fetchone()[0]
    src_rows = conn.execute(
        "SELECT COUNT(*) FROM pm25_readings WHERE station_id=? AND hour=4", (SRC,)
    ).fetchone()[0]
    dst_rows = conn.execute(
        "SELECT COUNT(*) FROM pm25_readings WHERE station_id=? AND hour=4", (DST,)
    ).fetchone()[0]
    conn.close()
    assert op_count == 1
    assert src_rows == 2
    assert dst_rows == 1


def test_replay_identical_even_with_different_hour_order(client):
    seed_full_day(client)
    first = transfer(client, "op-order", [hour_item(1), hour_item(2), hour_item(3)])
    second = transfer(client, "op-order", [hour_item(3), hour_item(1), hour_item(2)])
    assert first.status_code == second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["transferred_hours"] == [1, 2, 3]


def test_same_operation_id_different_content_conflicts(client):
    seed_full_day(client)
    assert transfer(client, "op-x", [hour_item(4)]).status_code == 200
    res = transfer(client, "op-x", [hour_item(5)])
    assert res.status_code == 409
    body = res.json()
    assert body["code"] == "operation_id_conflict"
    assert body["rolled_back"] is True
    assert body["operation_id"] == "op-x"
    assert body["existing_request"]["hours"] == [{"hour": 4, "source_revision": 1}]
    assert body["submitted_request"]["hours"] == [{"hour": 5, "source_revision": 1}]

    # 冲突请求未生效：小时 5 仍在源站有效
    assert daily(client, SRC)["hours"][5]["state"] == "valid"
    assert daily(client, DST)["hours"][5]["state"] == "missing"
    # 原操作仍可重放
    assert transfer(client, "op-x", [hour_item(4)]).json()["replayed"] is True


def test_same_operation_id_different_revision_conflicts(client):
    seed_full_day(client)
    assert transfer(client, "op-r", [hour_item(4)]).status_code == 200
    # 同号、同小时但携带不同 source_revision → 异内容冲突
    res = transfer(client, "op-r", [hour_item(4, revision=2)])
    assert res.status_code == 409
    assert res.json()["code"] == "operation_id_conflict"


# ---------------------------------------------------------------- 前置条件失败
def test_source_revision_changed_when_concurrent_supplement(client):
    seed_full_day(client)
    # 源站小时 6 在查询（看到 rev=1）之后被补报为 rev=2
    batch(client, [rec(6, 99.0, revision=2)])
    res = transfer(client, "op-changed", [hour_item(6, revision=1)])
    assert res.status_code == 409
    body = res.json()
    assert body["code"] == "transfer_precondition_failed"
    assert body["rolled_back"] is True
    conflict = body["conflicts"][0]
    assert conflict["reason"] == "source_revision_changed"
    assert conflict["hour"] == 6
    assert conflict["expected_revision"] == 1
    assert conflict["current_revision"] == 2

    # 整次不生效：新读数没有被撤回，目标站没有数据
    src = daily(client, SRC)
    assert src["hours"][6]["state"] == "valid"
    assert src["hours"][6]["value"] == 99.0
    assert src["hours"][6]["revision"] == 2
    assert daily(client, DST)["hours"][6]["state"] == "missing"


def test_source_retracted_reading_cannot_transfer(client):
    seed_full_day(client)
    batch(client, [rec(8, revision=2, retracted=True)])
    # 当前有效修订已是 rev=2 的撤回，与查询所见 rev=1 不符 → 按修订已变化拒绝
    res = transfer(client, "op-retr", [hour_item(8, revision=1)])
    assert res.status_code == 409
    conflict = res.json()["conflicts"][0]
    assert conflict["reason"] == "source_revision_changed"
    assert conflict["expected_revision"] == 1
    assert conflict["current_revision"] == 2
    assert daily(client, DST)["hours"][8]["state"] == "missing"


def test_source_retracted_with_matching_revision_rejected_not_current(client):
    # 修订号恰好匹配、但该修订本身是撤回：属于「源读数非当前有效」
    batch(client, [rec(8, revision=1, retracted=True)])
    res = transfer(client, "op-retr-current", [hour_item(8, revision=1)])
    assert res.status_code == 409
    conflict = res.json()["conflicts"][0]
    assert conflict["reason"] == "source_reading_not_current"
    assert conflict["current_revision"] == 1


def test_source_missing_hour_cannot_transfer(client):
    batch(client, [rec(0, 10.0)])  # 只有小时 0 有数据
    res = transfer(client, "op-miss", [hour_item(9, revision=1)])
    assert res.status_code == 409
    conflict = res.json()["conflicts"][0]
    # 查询所见的 rev=1 已不存在（小时无任何行）→ 按修订已变化拒绝
    assert conflict["reason"] == "source_revision_changed"
    assert conflict["current_revision"] is None
    assert conflict["station_id"] == SRC
    # 没有任何部分写入
    assert daily(client, DST)["valid_hours"] == 0


def test_target_hour_already_occupied_conflicts(client):
    seed_full_day(client)
    batch(client, [rec(3, 55.0, station=DST)])  # 目标站小时 3 已有读数
    res = transfer(client, "op-occ", [hour_item(3)])
    assert res.status_code == 409
    conflict = res.json()["conflicts"][0]
    assert conflict["reason"] == "target_hour_occupied"
    assert conflict["station_id"] == DST
    # 目标原值未被覆盖，源站未被撤回
    assert daily(client, DST)["hours"][3]["value"] == 55.0
    assert daily(client, SRC)["hours"][3]["state"] == "valid"


def test_target_retracted_row_still_counts_as_occupied(client):
    seed_full_day(client)
    # 目标站小时 3 有过入库记录且已撤回：仍是「曾入库」，不允许覆盖
    batch(client, [rec(3, 55.0, station=DST)])
    batch(client, [rec(3, revision=2, retracted=True, station=DST)])
    res = transfer(client, "op-retr-occup", [hour_item(3)])
    assert res.status_code == 409
    assert res.json()["conflicts"][0]["reason"] == "target_hour_occupied"
    assert daily(client, SRC)["hours"][3]["state"] == "valid"


def test_one_bad_hour_rolls_back_whole_operation(client):
    seed_full_day(client, value=3.0)
    batch(client, [rec(12, 40.0, station=DST)])  # 让小时 12 的目标被占用
    res = transfer(
        client,
        "op-rollback",
        [hour_item(10), hour_item(11), hour_item(12)],
    )
    assert res.status_code == 409
    reasons = {c["reason"] for c in res.json()["conflicts"]}
    assert reasons == {"target_hour_occupied"}
    # 同一事务里前两个合法小时也不得落地
    src = daily(client, SRC)
    dst = daily(client, DST)
    for hour in (10, 11, 12):
        assert src["hours"][hour]["state"] == "valid"
        assert src["hours"][hour]["revision"] == 1
    assert dst["hours"][10]["state"] == "missing"
    assert dst["hours"][11]["state"] == "missing"
    assert dst["hours"][12]["value"] == 40.0  # 原值保留


def test_operation_record_not_persisted_after_failure(client):
    seed_full_day(client)
    transfer(client, "op-fail", [hour_item(9, revision=5)])  # 修订号不匹配 → 409
    import sqlite3

    conn = sqlite3.connect(client.app.state.database.path)
    count = conn.execute(
        "SELECT COUNT(*) FROM pm25_transfer_operations WHERE operation_id='op-fail'"
    ).fetchone()[0]
    conn.close()
    assert count == 0
    # 失败的操作号可被重新用于一个合法请求
    ok = transfer(client, "op-fail", [hour_item(9, revision=1)])
    assert ok.status_code == 200


# ---------------------------------------------------------------- 链式转移与跨日隔离
def test_transferred_reading_can_move_again(client):
    seed_full_day(client, value=6.0)
    assert transfer(client, "op-chain-1", [hour_item(1)]).status_code == 200
    # 目标站 DST 现在持有有效 rev=1，可再转到第三个站
    res = transfer(
        client,
        "op-chain-2",
        [hour_item(1)],
        source=DST,
        target="THIRD",
    )
    assert res.status_code == 200, res.text
    assert res.json()["hours"][0]["value"] == 6.0
    assert daily(client, "THIRD")["hours"][1]["value"] == 6.0
    assert daily(client, DST)["hours"][1]["state"] == "retracted"
    assert daily(client, SRC)["hours"][1]["state"] == "retracted"


def test_dates_are_isolated_in_transfer(client):
    seed_full_day(client, value=10.0, date=DAY)
    seed_full_day(client, value=20.0, station=SRC, date="2025-04-02")
    res = transfer(client, "op-date", [hour_item(0)], date="2025-04-02")
    assert res.status_code == 200
    assert daily(client, SRC, "2025-04-02")["hours"][0]["state"] == "retracted"
    # 前一天的数据不受影响
    assert daily(client, SRC, DAY)["hours"][0]["state"] == "valid"
    assert daily(client, DST, DAY)["hours"][0]["state"] == "missing"
    assert daily(client, DST, "2025-04-02")["hours"][0]["value"] == 20.0


# ---------------------------------------------------------------- 请求校验
def test_source_and_target_must_differ(client):
    res = transfer(client, "op-same", [hour_item(0)], source=SRC, target=SRC)
    assert res.status_code == 422
    assert res.json()["code"] == "validation_error"


def test_duplicate_hours_in_request_rejected(client):
    res = transfer(client, "op-dup", [hour_item(1), hour_item(1)])
    assert res.status_code == 422


def test_empty_hours_rejected(client):
    res = transfer(client, "op-empty", [])
    assert res.status_code == 422


@pytest.mark.parametrize("hour", [-1, 24])
def test_transfer_hour_out_of_range(client, hour):
    res = transfer(client, "op-range", [{"hour": hour, "source_revision": 1}])
    assert res.status_code == 422


def test_source_revision_must_be_positive(client):
    res = transfer(client, "op-rev0", [{"hour": 0, "source_revision": 0}])
    assert res.status_code == 422


def test_blank_operation_id_rejected(client):
    res = client.post(
        "/api/v1/readings/transfer",
        json={
            "operation_id": "   ",
            "source_station_id": SRC,
            "target_station_id": DST,
            "date": DAY,
            "hours": [hour_item(0)],
        },
    )
    assert res.status_code == 422


def test_unknown_field_rejected(client):
    res = client.post(
        "/api/v1/readings/transfer",
        json={
            "operation_id": "op-extra",
            "source_station_id": SRC,
            "target_station_id": DST,
            "date": DAY,
            "hours": [hour_item(0)],
            "extra": 1,
        },
    )
    assert res.status_code == 422


def test_transfer_date_with_time_component_rejected(client):
    res = client.post(
        "/api/v1/readings/transfer",
        json={
            "operation_id": "op-dt",
            "source_station_id": SRC,
            "target_station_id": DST,
            "date": "2025-04-01T08:00:00",
            "hours": [hour_item(0)],
        },
    )
    assert res.status_code == 422


@pytest.mark.parametrize("patch", [{"hour": "3"}, {"hour": True}, {"source_revision": "1"}])
def test_transfer_cross_type_coercion_rejected(client, patch):
    item = hour_item(0)
    item.update(patch)
    assert transfer(client, "op-type", [item]).status_code == 422


# ---------------------------------------------------------------- 并发
def test_concurrent_transfers_same_source_hour_only_one_wins(client):
    """两个转移抢同一个源小时：按实际提交顺序，一个成功，另一个源修订已变而失败。"""
    seed_full_day(client)
    barrier = threading.Barrier(2)
    results = []

    def run(op_id, target):
        barrier.wait()
        results.append(
            transfer(client, op_id, [hour_item(5)], target=target)
        )

    threads = [
        threading.Thread(target=run, args=("op-c1", "T1")),
        threading.Thread(target=run, args=("op-c2", "T2")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 409], statuses
    failed = next(r for r in results if r.status_code == 409)
    assert failed.json()["conflicts"][0]["reason"] == "source_revision_changed"
    # 只有一个目标站拿到读数，源站只撤回一次（rev=2）
    src = daily(client, SRC)
    assert src["hours"][5]["state"] == "retracted"
    assert src["hours"][5]["revision"] == 2
    target_states = [
        daily(client, "T1")["hours"][5]["state"],
        daily(client, "T2")["hours"][5]["state"],
    ]
    assert sorted(target_states) == ["missing", "valid"]


def test_concurrent_transfer_and_supplement_judged_by_commit_order(client):
    """转移与补报并发：提交在前的生效；转移不得撤回补报产生的新读数。"""
    seed_full_day(client)
    barrier = threading.Barrier(2)
    outcomes = []

    def do_transfer():
        barrier.wait()
        outcomes.append(("transfer", transfer(client, "op-race", [hour_item(7)])))

    def do_supplement():
        barrier.wait()
        outcomes.append(
            ("supplement", batch(client, [rec(7, 123.0, revision=2)]))
        )

    threads = [threading.Thread(target=do_transfer), threading.Thread(target=do_supplement)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    src = daily(client, SRC)
    transfer_res = next(r for kind, r in outcomes if kind == "transfer")
    supplement_res = next(r for kind, r in outcomes if kind == "supplement")
    if transfer_res.status_code == 200:
        # 转移先提交：源站撤回占用了 rev=2，随后补报 rev=2 同号不同内容 → 409，
        # 补报无法把已转移的读数「写回来」；原值落在目标站。
        assert supplement_res.status_code == 409
        assert src["hours"][7]["state"] == "retracted"
        assert src["hours"][7]["revision"] == 2
        assert daily(client, DST)["hours"][7]["value"] == 10.0  # 转移走的是原值
    else:
        # 补报先提交：转移看到 rev=2 ≠ 期望 rev=1，拒绝且不撤回新读数
        assert supplement_res.status_code == 200
        assert transfer_res.json()["conflicts"][0]["reason"] == "source_revision_changed"
        assert src["hours"][7]["value"] == 123.0
        assert src["hours"][7]["state"] == "valid"
        assert daily(client, DST)["hours"][7]["state"] == "missing"


def test_concurrent_transfers_same_target_hour_never_overwrite(client):
    """两个不同源站向同一目标小时转移：至多一个成功，目标值不被覆盖。"""
    seed_full_day(client, value=10.0, station="A")
    seed_full_day(client, value=20.0, station="B")
    barrier = threading.Barrier(2)
    results = []

    def run(op_id, source):
        barrier.wait()
        results.append(
            transfer(client, op_id, [hour_item(9)], source=source, target=DST)
        )

    threads = [
        threading.Thread(target=run, args=("op-t1", "A")),
        threading.Thread(target=run, args=("op-t2", "B")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 409], statuses
    failed = next(r for r in results if r.status_code == 409)
    assert failed.json()["conflicts"][0]["reason"] == "target_hour_occupied"
    dst = daily(client, DST)
    assert dst["hours"][9]["value"] in (10.0, 20.0)
    assert dst["hours"][9]["revision"] == 1
    # 失败一方的源站未被撤回
    failed_source = "B" if dst["hours"][9]["value"] == 10.0 else "A"
    assert daily(client, failed_source)["hours"][9]["state"] == "valid"


def test_concurrent_same_operation_id_returns_same_result(client):
    """同操作号同内容并发重试：都返回 200，且只落地一次。"""
    seed_full_day(client)
    barrier = threading.Barrier(4)
    results = []

    def run():
        barrier.wait()
        results.append(transfer(client, "op-concur-idem", [hour_item(13)]))

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert daily(client, SRC)["hours"][13]["revision"] == 2
    assert daily(client, DST)["hours"][13]["revision"] == 1
    import sqlite3

    conn = sqlite3.connect(client.app.state.database.path)
    assert conn.execute(
        "SELECT COUNT(*) FROM pm25_transfer_operations WHERE operation_id='op-concur-idem'"
    ).fetchone()[0] == 1
    conn.close()
