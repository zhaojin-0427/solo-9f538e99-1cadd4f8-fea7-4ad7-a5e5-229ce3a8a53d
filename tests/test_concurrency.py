"""端到端并发测试：启动真实 uvicorn 服务，用多线程 HTTP 请求验证竞态。

TestClient 的并发能力有限，这里单独起一个进程内 uvicorn，通过真实 socket 压测。
"""

from __future__ import annotations

import concurrent.futures
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest
import uvicorn

from app.config import Settings
from app.main import create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server(tmp_path):
    port = _free_port()
    app = create_app(Settings(database_path=str(tmp_path / "live.db")))
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/healthz", timeout=0.5).read()
            break
        except Exception:
            time.sleep(0.05)
    else:
        raise RuntimeError("server did not start")

    yield base

    srv.should_exit = True
    thread.join(timeout=10)


def _post(base: str, records: list[dict]) -> int:
    req = urllib.request.Request(
        base + "/api/v1/readings/batch",
        data=json.dumps({"records": records}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _get_json(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=15) as resp:
        return json.load(resp)


def _rec(hour, value=None, revision=1, retracted=False, station="S", date="2025-05-01"):
    body = {
        "station_id": station,
        "date": date,
        "hour": hour,
        "revision": revision,
        "retracted": retracted,
    }
    if value is not None:
        body["value"] = value
    return body


def test_parallel_writers_same_revision_only_one_wins(server):
    """同一修订号、不同内容并发提交：恰好 1 个 200，其余 409，库中只有一个值。"""
    contenders = [float(v) for v in range(10, 26)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(contenders)) as pool:
        statuses = list(pool.map(lambda v: _post(server, [_rec(3, v, revision=1)]), contenders))

    assert statuses.count(200) == 1, statuses
    assert statuses.count(409) == len(contenders) - 1, statuses

    body = _get_json(server, "/api/v1/stations/S/daily?date=2025-05-01")
    value = body["hours"][3]["value"]
    assert value in contenders
    assert body["hours"][3]["revision"] == 1


def test_parallel_revisions_converge_to_max(server):
    """不同修订号乱序并发：最终结果一定是最新修订号的值。"""
    revisions = list(range(1, 13))
    records = [_rec(11, float(rev), revision=rev) for rev in revisions]
    barrier = threading.Barrier(len(records))

    def send(record):
        barrier.wait()
        return _post(server, [record])

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(records)) as pool:
        statuses = list(pool.map(send, records))

    assert statuses == [200] * len(records)
    body = _get_json(server, "/api/v1/stations/S/daily?date=2025-05-01")
    assert body["hours"][11]["revision"] == 12
    assert body["hours"][11]["value"] == 12.0


def test_conflicting_batch_never_partially_commits(server):
    """并发提交的冲突批次不得留下任何记录。"""
    assert _post(server, [_rec(0, 1.0, revision=1)]) == 200

    def send(offset):
        return _post(
            server,
            [
                _rec(14 + offset, 5.0, revision=1),
                _rec(0, 100.0 + offset, revision=1),  # 与基线冲突
            ],
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        statuses = list(pool.map(send, range(5)))

    assert statuses == [409] * 5
    body = _get_json(server, "/api/v1/stations/S/daily?date=2025-05-01")
    assert body["hours"][0]["value"] == 1.0
    for hour in range(14, 19):
        assert body["hours"][hour]["state"] == "missing"


def test_reads_reflect_writes_immediately(server):
    """写入后立即查询即可看到结果；撤回后立即消失。"""
    assert _post(server, [_rec(h, 10.0) for h in range(24)]) == 200
    body = _get_json(server, "/api/v1/stations/S/daily?date=2025-05-01")
    assert body["daily_mean"] == 10.0

    assert _post(server, [_rec(0, revision=2, retracted=True)]) == 200
    body = _get_json(server, "/api/v1/stations/S/daily?date=2025-05-01")
    assert body["valid_hours"] == 23
    assert body["retracted_hours"] == [0]
    assert body["hours"][0]["state"] == "retracted"


# ---------------------------------------------------------------- 站点间转移
def _transfer(base: str, op_id: str, hours: list[dict], source="SA", target="SB",
              date="2025-05-01") -> tuple[int, dict]:
    req = urllib.request.Request(
        base + "/api/v1/readings/transfer",
        data=json.dumps(
            {
                "operation_id": op_id,
                "source_station_id": source,
                "target_station_id": target,
                "date": date,
                "hours": hours,
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_transfer_end_to_end_moves_reading(server):
    """真实服务下转移：源站追加撤回、目标站收到原值，日报立即反映。"""
    assert _post(server, [_rec(h, 11.0, station="SA") for h in range(24)]) == 200
    status, body = _transfer(server, "t-1", [{"hour": 4, "source_revision": 1}])
    assert status == 200, body
    assert body["transferred_hours"] == [4]
    assert body["hours"][0] == {
        "hour": 4,
        "value": 11.0,
        "source_revision_before": 1,
        "source_retraction_revision": 2,
        "target_revision": 1,
    }

    src = _get_json(server, "/api/v1/stations/SA/daily?date=2025-05-01")
    dst = _get_json(server, "/api/v1/stations/SB/daily?date=2025-05-01")
    assert src["hours"][4]["state"] == "retracted"
    assert src["hours"][4]["revision"] == 2
    assert src["valid_hours"] == 23
    assert dst["hours"][4]["state"] == "valid"
    assert dst["hours"][4]["value"] == 11.0


def test_parallel_transfers_same_source_only_one_wins(server):
    """并发转移同一源小时：恰好一个成功，其余因源修订已变失败，读数只落地一个目标。"""
    assert _post(server, [_rec(6, 9.0, station="SA")]) == 200
    targets = [f"T{i}" for i in range(8)]
    barrier = threading.Barrier(len(targets))

    def send(index_target):
        index, target = index_target
        barrier.wait()
        return _transfer(
            server,
            f"pt-{index}",
            [{"hour": 6, "source_revision": 1}],
            source="SA",
            target=target,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as pool:
        results = list(pool.map(send, enumerate(targets)))

    statuses = [status for status, _ in results]
    assert statuses.count(200) == 1, statuses
    assert statuses.count(409) == len(targets) - 1, statuses
    for status, body in results:
        if status == 409:
            assert body["code"] == "transfer_precondition_failed"
            assert body["conflicts"][0]["reason"] == "source_revision_changed"
            assert body["rolled_back"] is True

    src = _get_json(server, "/api/v1/stations/SA/daily?date=2025-05-01")
    assert src["hours"][6]["state"] == "retracted"
    assert src["hours"][6]["revision"] == 2
    occupied = [
        t
        for t in targets
        if _get_json(server, f"/api/v1/stations/{t}/daily?date=2025-05-01")["hours"][6]["state"]
        == "valid"
    ]
    assert len(occupied) == 1


def test_parallel_transfers_same_target_never_overwrite(server):
    """不同源站并发转移到同一目标小时：只有一个写入成功，目标值不被覆盖。"""
    assert _post(server, [_rec(8, 1.0, station="A1")]) == 200
    assert _post(server, [_rec(8, 2.0, station="A2")]) == 200
    barrier = threading.Barrier(2)

    def send(job):
        op_id, source = job
        barrier.wait()
        return _transfer(
            server, op_id, [{"hour": 8, "source_revision": 1}],
            source=source, target="SHARED",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, [("g-1", "A1"), ("g-2", "A2")]))

    statuses = sorted(status for status, _ in results)
    assert statuses == [200, 409], results
    failed = next(body for status, body in results if status == 409)
    assert failed["conflicts"][0]["reason"] == "target_hour_occupied"
    dst = _get_json(server, "/api/v1/stations/SHARED/daily?date=2025-05-01")
    assert dst["hours"][8]["value"] in (1.0, 2.0)
    assert dst["hours"][8]["revision"] == 1


def test_transfer_replay_under_concurrency_applies_once(server):
    """同操作号同内容并发重试：全部 200，两站记录与操作登记都只落地一次。"""
    assert _post(server, [_rec(10, 7.0, station="SA") for _ in range(1)]) == 200
    barrier = threading.Barrier(6)

    def send(_):
        barrier.wait()
        return _transfer(
            server, "idem-op", [{"hour": 10, "source_revision": 1}],
            source="SA", target="SB",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(send, range(6)))

    assert all(status == 200 for status, _ in results), results
    assert sum(1 for _, body in results if body["replayed"]) == 5
    src = _get_json(server, "/api/v1/stations/SA/daily?date=2025-05-01")
    dst = _get_json(server, "/api/v1/stations/SB/daily?date=2025-05-01")
    assert src["hours"][10]["revision"] == 2
    assert dst["hours"][10]["revision"] == 1


def test_transfer_vs_supplement_race_judged_by_commit_order(server):
    """转移与补报并发：按实际提交顺序判定，转移不能撤回补报的新读数。"""
    assert _post(server, [_rec(12, 10.0, station="SA")]) == 200
    barrier = threading.Barrier(2)

    def transfer_call():
        barrier.wait()
        return _transfer(
            server, "race-op", [{"hour": 12, "source_revision": 1}],
            source="SA", target="SB",
        )

    def supplement_call():
        barrier.wait()
        return _post(server, [_rec(12, 123.0, station="SA", revision=2)])

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        transfer_future = pool.submit(transfer_call)
        supplement_future = pool.submit(supplement_call)
        t_status, t_body = transfer_future.result()
        s_status = supplement_future.result()

    src = _get_json(server, "/api/v1/stations/SA/daily?date=2025-05-01")
    if t_status == 200:
        # 转移先提交：源站 rev=2 已是撤回，补报 rev=2 同号不同内容被 409
        assert s_status == 409
        assert src["hours"][12]["state"] == "retracted"
        assert _get_json(server, "/api/v1/stations/SB/daily?date=2025-05-01")["hours"][12][
            "value"
        ] == 10.0
    else:
        # 补报先提交：转移的乐观锁失败，新读数不被撤回
        assert s_status == 200
        assert t_body["conflicts"][0]["reason"] == "source_revision_changed"
        assert src["hours"][12]["value"] == 123.0
        assert src["hours"][12]["state"] == "valid"
