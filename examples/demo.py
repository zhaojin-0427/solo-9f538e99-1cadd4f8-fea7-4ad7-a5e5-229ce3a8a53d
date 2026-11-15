#!/usr/bin/env python3
"""可运行的 API 调用示例（仅用标准库）。

用法：
    python3 examples/demo.py                      # 默认 http://localhost:8000
    python3 examples/demo.py http://localhost:9000

它会：补报 20 个有效小时 → 查询日均 → 撤回 1 小时 → 再次查询（跌破门槛）→
用更大修订号重新报回 → 再次查询（恢复日均）→ 把两个小时转移到另一个站点 →
同号重试（幂等）与同号异内容（冲突）。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
STATION = "DEMO-001"
DATE = "2025-03-01"


def post_batch(records: list[dict]) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{BASE}/api/v1/readings/batch",
        data=json.dumps({"records": records}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def get_daily() -> dict:
    url = f"{BASE}/api/v1/stations/{STATION}/daily?date={DATE}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.load(resp)


def post_transfer(payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{BASE}/api/v1/readings/transfer",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def summarize(label: str) -> None:
    report = get_daily()
    print(
        f"{label}: status={report['status']} valid={report['valid_hours']} "
        f"mean={report['daily_mean']} missing={report['missing_hours']} "
        f"retracted={report['retracted_hours']}"
    )


def main() -> None:
    readings = [
        {"station_id": STATION, "date": DATE, "hour": h, "revision": 1, "value": 40.0 + h}
        for h in range(20)
    ]
    status, body = post_batch(readings)
    print(f"补报 20 小时 -> HTTP {status} {body}")

    status, body = post_batch([dict(readings[0])])  # 幂等重试
    print(f"重复提交同号同内容 -> HTTP {status} {body}")

    summarize("初始日报")

    status, body = post_batch(
        [{"station_id": STATION, "date": DATE, "hour": 0, "revision": 2, "retracted": True}]
    )
    print(f"撤回 0 点 -> HTTP {status} {body}")
    summarize("撤回后")

    status, body = post_batch(
        [{"station_id": STATION, "date": DATE, "hour": 0, "revision": 3, "value": 66.6}]
    )
    print(f"以修订号 3 补回 -> HTTP {status} {body}")
    summarize("补回后")

    status, body = post_batch(
        [{"station_id": STATION, "date": DATE, "hour": 1, "revision": 1, "value": 0.0}]
    )
    print(f"同号改内容 -> HTTP {status} code={body.get('code')} rolled_back={body.get('rolled_back')}")
    summarize("冲突被拒后（应保持不变）")

    # -------------------------------------------------------------- 站点间转移
    target = "DEMO-002"
    transfer_payload = {
        "operation_id": "DEMO-TRF-001",
        "source_station_id": STATION,
        "target_station_id": target,
        "date": DATE,
        "hours": [
            {"hour": 2, "source_revision": 1},
            {"hour": 3, "source_revision": 1},
        ],
    }
    status, body = post_transfer(transfer_payload)
    print(f"转移小时 2/3 到 {target} -> HTTP {status} {body}")

    url = f"{BASE}/api/v1/stations/{target}/daily?date={DATE}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        target_report = json.load(resp)
    print(
        f"目标站日报: status={target_report['status']} valid={target_report['valid_hours']} "
        f"hours_2_3={[target_report['hours'][h] for h in (2, 3)]}"
    )
    summarize("源站转移后（小时 2/3 应被撤回）")

    status, body = post_transfer(transfer_payload)  # 同号同内容重试
    print(f"同号同内容重试 -> HTTP {status} replayed={body.get('replayed')}")

    conflicting = dict(transfer_payload)
    conflicting["hours"] = [{"hour": 4, "source_revision": 1}]
    status, body = post_transfer(conflicting)
    print(f"同号异内容 -> HTTP {status} code={body.get('code')}")


if __name__ == "__main__":
    main()
