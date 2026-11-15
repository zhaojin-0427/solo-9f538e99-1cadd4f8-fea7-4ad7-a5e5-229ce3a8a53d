"""业务逻辑：批量写入、日报查询与站点间读数转移。"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass

from .config import HOURS_PER_DAY, REQUIRED_VALID_HOURS
from .db import Database, utc_timestamp
from .models import (
    ConflictDetail,
    DailyReport,
    HourState,
    ReadingRecord,
    TransferConflictDetail,
    TransferRequest,
)


class RevisionConflictError(Exception):
    """同号不同内容：整批拒绝。"""

    def __init__(self, conflicts: list[ConflictDetail]) -> None:
        self.conflicts = conflicts
        super().__init__(f"{len(conflicts)} revision conflict(s)")


class TransferPreconditionError(Exception):
    """转移前置条件不满足（源修订已变 / 源读数无效 / 目标小时已占用）：整次不生效。"""

    def __init__(self, conflicts: list[TransferConflictDetail]) -> None:
        self.conflicts = conflicts
        super().__init__(f"{len(conflicts)} transfer precondition conflict(s)")


class OperationIdConflictError(Exception):
    """相同操作号但请求内容不同：冲突，本次不生效。"""

    def __init__(self, existing_request: dict, submitted_request: dict) -> None:
        self.existing_request = existing_request
        self.submitted_request = submitted_request
        super().__init__("operation_id already used with a different request")


@dataclass(frozen=True)
class BatchOutcome:
    received: int
    created: int
    unchanged: int
    stations: list[str]
    dates: list[str]


class ReadingService:
    def __init__(self, database: Database) -> None:
        self.db = database

    # -- 批量写入 -------------------------------------------------------
    def apply_batch(self, records: list[ReadingRecord]) -> BatchOutcome:
        """原子地应用一批修订。

        校验规则（全部在同一写事务内、持有写锁时完成）：
        1. 同号同内容 → 幂等跳过，计为 unchanged；
        2. 同号不同内容 → 抛 RevisionConflictError，整批回滚；
        3. 新修订号 → 插入新行（不覆盖旧修订）。
        """
        if not records:
            raise ValueError("batch must contain at least one record")

        unique_keys = {(r.station_id, r.date.isoformat(), r.hour, r.revision) for r in records}
        stations = sorted({r.station_id for r in records})
        dates = sorted({r.date.isoformat() for r in records})

        conn = self.db.connect()
        try:
            with Database.write_transaction(conn):
                # 先在同一事务内查已有修订，避免校验与写入之间的竞态。
                existing = self._load_existing(conn, unique_keys)

                # 批内重复检查：同一 batch 中同一 key 出现两次且内容不同 → 冲突。
                seen: dict[tuple[str, str, int, int], tuple[float | None, bool]] = {}
                conflicts: list[ConflictDetail] = []
                created = 0
                unchanged = 0
                now = utc_timestamp()

                for record in records:
                    key = (record.station_id, record.date.isoformat(), record.hour, record.revision)
                    submitted = record.content()

                    if key in seen and seen[key] != submitted:
                        conflicts.append(
                            self._conflict(
                                record,
                                existing_payload=seen[key],
                                submitted_payload=submitted,
                            )
                        )
                        continue
                    if key in seen:
                        unchanged += 1
                        continue
                    seen[key] = submitted

                    stored = existing.get(key)
                    if stored is not None:
                        if stored != submitted:
                            conflicts.append(
                                self._conflict(
                                    record,
                                    existing_payload=stored,
                                    submitted_payload=submitted,
                                )
                            )
                        else:
                            unchanged += 1
                        continue

                    conn.execute(
                        """
                        INSERT INTO pm25_readings
                            (station_id, obs_date, hour, revision, value, retracted, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record.station_id,
                            record.date.isoformat(),
                            record.hour,
                            record.revision,
                            record.value,
                            1 if record.retracted else 0,
                            now,
                        ),
                    )
                    created += 1

                if conflicts:
                    # 让 with 块内抛异常 → ROLLBACK，本批任何插入都不生效。
                    raise RevisionConflictError(conflicts)
        finally:
            conn.close()

        return BatchOutcome(
            received=len(records),
            created=created,
            unchanged=unchanged,
            stations=stations,
            dates=dates,
        )

    @staticmethod
    def _load_existing(
        conn: sqlite3.Connection,
        keys: set[tuple[str, str, int, int]],
    ) -> dict[tuple[str, str, int, int], tuple[float | None, bool]]:
        if not keys:
            return {}
        existing: dict[tuple[str, str, int, int], tuple[float | None, bool]] = {}

        ordered = sorted(keys)
        # 用 row-value IN 一次查询多组 (station_id, obs_date, hour, revision)，
        # 只取批次涉及的键；分块以避开 SQLite 的绑定变量上限（每键 4 个参数）。
        chunk_size = 200
        for start in range(0, len(ordered), chunk_size):
            chunk = ordered[start : start + chunk_size]
            placeholders = ",".join("(?,?,?,?)" for _ in chunk)
            params = [value for key in chunk for value in key]
            rows = conn.execute(
                f"""
                SELECT station_id, obs_date, hour, revision, value, retracted
                FROM pm25_readings
                WHERE (station_id, obs_date, hour, revision) IN (VALUES {placeholders})
                """,
                params,
            ).fetchall()
            for row in rows:
                existing[
                    (row["station_id"], row["obs_date"], row["hour"], row["revision"])
                ] = (row["value"], bool(row["retracted"]))
        return existing

    @staticmethod
    def _conflict(
        record: ReadingRecord,
        *,
        existing_payload: tuple[float | None, bool],
        submitted_payload: tuple[float | None, bool],
    ) -> ConflictDetail:
        return ConflictDetail(
            station_id=record.station_id,
            date=record.date.isoformat(),
            hour=record.hour,
            revision=record.revision,
            existing=_payload_dict(existing_payload),
            submitted=_payload_dict(submitted_payload),
        )

    # -- 站点间转移 -----------------------------------------------------
    def apply_transfer(
        self, request: TransferRequest
    ) -> tuple[dict, bool]:
        """原子地把源站若干小时的当前读数转移到目标站。

        全程在单个 ``BEGIN IMMEDIATE`` 写事务内完成（持写锁，与补报/其他转移串行）：

        1. 操作号查重：同号同内容 → 重放原结果（replayed=True，无写入）；
           同号异内容 → ``OperationIdConflictError``，整次不生效。
        2. 逐小时校验前置条件：
           * 源站该小时当前修订号必须等于查询时看到的 ``source_revision``，
             否则 ``source_revision_changed``（不能撤回已被修订/撤回的新读数）；
           * 该当前修订必须是带浓度值的有效读数，否则 ``source_reading_not_current``；
           * 目标站该小时必须从未入库（没有任何修订行，含撤回），
             否则 ``target_hour_occupied``（不能覆盖目标值）。
        3. 校验全部通过后：为源站追加更高修订号的撤回记录（MAX(revision)+1），
           把原浓度以 revision=1 写入目标站，并登记操作与结果；与两站记录一同 COMMIT。

        任一前置条件不满足或写入失败均在 COMMIT 前抛错并整体 ROLLBACK。
        """
        obs_date = request.date.isoformat()
        # 请求指纹只包含业务内容，不含 operation_id；hours 按小时排序后规范化，
        # 使同内容的重试在条目顺序不同时仍被识别为同一请求。
        canonical = {
            "source_station_id": request.source_station_id,
            "target_station_id": request.target_station_id,
            "date": obs_date,
            "hours": sorted(
                (
                    {"hour": item.hour, "source_revision": item.source_revision}
                    for item in request.hours
                ),
                key=lambda h: h["hour"],
            ),
        }
        fingerprint = json.dumps(
            canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        submitted_request = dict(canonical)

        conn = self.db.connect()
        try:
            with Database.write_transaction(conn):
                # 1) 操作号登记查重（此时已持有写锁，登记行的检查-插入之间无竞态）。
                operation_row = conn.execute(
                    "SELECT request_fingerprint, request_json, result_json "
                    "FROM pm25_transfer_operations WHERE operation_id = ?",
                    (request.operation_id,),
                ).fetchone()
                if operation_row is not None:
                    if operation_row["request_fingerprint"] != fingerprint:
                        raise OperationIdConflictError(
                            existing_request=json.loads(operation_row["request_json"]),
                            submitted_request=submitted_request,
                        )
                    # 同号同内容：原结果原样返回，不做任何写入。
                    return json.loads(operation_row["result_json"]), True

                hours = sorted((item.hour for item in request.hours))
                expected_revision = {item.hour: item.source_revision for item in request.hours}

                # 2) 读取源站涉及小时的全部修订，定位每个小时的当前（最大修订号）行。
                source_rows = self._load_hour_rows(
                    conn, request.source_station_id, obs_date, hours
                )
                # 3) 读取目标站涉及小时的全部修订（任何一行存在都算已入库）。
                target_rows = self._load_hour_rows(
                    conn, request.target_station_id, obs_date, hours
                )

                conflicts: list[TransferConflictDetail] = []
                for hour in hours:
                    source_latest = source_rows.get(hour)
                    current_revision = None if source_latest is None else source_latest["revision"]
                    if current_revision != expected_revision[hour]:
                        # 查询之后源修订已被补报/撤回/再次转移改写（含当前已撤回、
                        # 小时被删空等情况）→ 一律视为修订已变化，不能撤回新读数。
                        conflicts.append(
                            TransferConflictDetail(
                                reason="source_revision_changed",
                                station_id=request.source_station_id,
                                date=obs_date,
                                hour=hour,
                                expected_revision=expected_revision[hour],
                                current_revision=current_revision,
                                message=(
                                    "source hour no longer has any reading"
                                    if source_latest is None
                                    else "source revision changed since the reading was queried"
                                ),
                            )
                        )
                        continue
                    if bool(source_latest["retracted"]):
                        # 修订号虽未变化，但该修订本身是撤回：没有可转移的有效读数。
                        conflicts.append(
                            TransferConflictDetail(
                                reason="source_reading_not_current",
                                station_id=request.source_station_id,
                                date=obs_date,
                                hour=hour,
                                expected_revision=expected_revision[hour],
                                current_revision=current_revision,
                                message="source hour's current revision is a retraction",
                            )
                        )
                        continue
                    if hour in target_rows:
                        # 目标站该小时已有入库记录（含历史撤回）→ 不能覆盖目标值。
                        conflicts.append(
                            TransferConflictDetail(
                                reason="target_hour_occupied",
                                station_id=request.target_station_id,
                                date=obs_date,
                                hour=hour,
                                expected_revision=expected_revision[hour],
                                current_revision=target_rows[hour]["revision"],
                                message="target station already has a record for this hour",
                            )
                        )

                if conflicts:
                    # with 块内抛错 → ROLLBACK：两站读数与操作登记都不会落地。
                    raise TransferPreconditionError(conflicts)

                # 4) 前置条件全部满足：源站追加撤回、目标站写入原值，一次提交。
                now = utc_timestamp()
                hour_results = []
                for hour in hours:
                    source_latest = source_rows[hour]
                    value = source_latest["value"]
                    retraction_revision = source_latest["revision"] + 1

                    conn.execute(
                        """
                        INSERT INTO pm25_readings
                            (station_id, obs_date, hour, revision, value, retracted, created_at)
                        VALUES (?, ?, ?, ?, NULL, 1, ?)
                        """,
                        (
                            request.source_station_id,
                            obs_date,
                            hour,
                            retraction_revision,
                            now,
                        ),
                    )
                    # 目标站该小时已确认没有任何行，因此以 revision=1 建立首条记录。
                    conn.execute(
                        """
                        INSERT INTO pm25_readings
                            (station_id, obs_date, hour, revision, value, retracted, created_at)
                        VALUES (?, ?, ?, 1, ?, 0, ?)
                        """,
                        (
                            request.target_station_id,
                            obs_date,
                            hour,
                            value,
                            now,
                        ),
                    )
                    hour_results.append(
                        {
                            "hour": hour,
                            "value": value,
                            "source_revision_before": expected_revision[hour],
                            "source_retraction_revision": retraction_revision,
                            "target_revision": 1,
                        }
                    )

                result = {
                    "operation_id": request.operation_id,
                    "status": "applied",
                    "source_station_id": request.source_station_id,
                    "target_station_id": request.target_station_id,
                    "date": obs_date,
                    "transferred_hours": hours,
                    "transferred_count": len(hours),
                    "hours": hour_results,
                    "replayed": False,
                }
                conn.execute(
                    """
                    INSERT INTO pm25_transfer_operations
                        (operation_id, request_fingerprint, request_json,
                         result_json, status, created_at)
                    VALUES (?, ?, ?, ?, 'applied', ?)
                    """,
                    (
                        request.operation_id,
                        fingerprint,
                        json.dumps(
                            submitted_request,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        json.dumps(result, ensure_ascii=False),
                        now,
                    ),
                )
        finally:
            conn.close()

        return result, False

    @staticmethod
    def _load_hour_rows(
        conn: sqlite3.Connection,
        station_id: str,
        obs_date: str,
        hours: list[int],
    ) -> dict[int, sqlite3.Row]:
        """读取某站某日给定小时的全部修订，返回每小时最大修订号对应的行。"""
        if not hours:
            return {}
        latest: dict[int, sqlite3.Row] = {}
        chunk_size = 200
        for start in range(0, len(hours), chunk_size):
            chunk = hours[start : start + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT hour, revision, value, retracted
                FROM pm25_readings
                WHERE station_id = ?
                  AND obs_date = ?
                  AND hour IN ({placeholders})
                """,
                [station_id, obs_date, *chunk],
            ).fetchall()
            for row in rows:
                current = latest.get(row["hour"])
                if current is None or row["revision"] > current["revision"]:
                    latest[row["hour"]] = row
        return latest

    # -- 日报查询 -------------------------------------------------------
    def daily_report(self, station_id: str, day: dt.date) -> DailyReport:
        """按每小时最大修订号计算日报。"""
        obs_date = day.isoformat()
        conn = self.db.connect()
        try:
            # 单条 SELECT 在 WAL 模式下读到的是一致快照，不会看到写入中途的状态。
            # 子查询取每小时的最大修订号，再与该修订行关联，保证「旧修订不覆盖新值」。
            rows = conn.execute(
                """
                SELECT r.hour, r.value, r.retracted, r.revision
                FROM pm25_readings AS r
                WHERE r.station_id = ?
                  AND r.obs_date = ?
                  AND r.revision = (
                      SELECT MAX(revision)
                      FROM pm25_readings AS m
                      WHERE m.station_id = r.station_id
                        AND m.obs_date = r.obs_date
                        AND m.hour = r.hour
                  )
                ORDER BY r.hour
                """,
                (station_id, obs_date),
            ).fetchall()
        finally:
            conn.close()

        latest = {row["hour"]: row for row in rows}
        hours: list[HourState] = []
        missing_hours: list[int] = []
        retracted_hours: list[int] = []
        valid_values: list[float] = []

        for hour in range(HOURS_PER_DAY):
            row = latest.get(hour)
            if row is None:
                # 从未收到过数据。
                missing_hours.append(hour)
                hours.append(HourState(hour=hour, state="missing"))
            elif row["retracted"]:
                # 最新修订是撤回：该小时同样没有有效值，计入缺失小时，
                # 同时单独列在 retracted_hours 中以便区分「撤回」与「未报」。
                missing_hours.append(hour)
                retracted_hours.append(hour)
                hours.append(HourState(hour=hour, state="retracted", revision=row["revision"]))
            else:
                valid_values.append(row["value"])
                hours.append(
                    HourState(
                        hour=hour,
                        state="valid",
                        value=row["value"],
                        revision=row["revision"],
                    )
                )

        valid_hours = len(valid_values)
        if valid_hours >= REQUIRED_VALID_HOURS:
            daily_mean = round(sum(valid_values) / valid_hours, 2)
            status = "ok"
        else:
            daily_mean = None
            status = "insufficient_valid_hours"

        return DailyReport(
            station_id=station_id,
            date=obs_date,
            required_valid_hours=REQUIRED_VALID_HOURS,
            valid_hours=valid_hours,
            daily_mean=daily_mean,
            status=status,
            missing_hours=missing_hours,
            retracted_hours=retracted_hours,
            hours=hours,
        )


def _payload_dict(payload: tuple[float | None, bool]) -> dict:
    value, retracted = payload
    return {"value": value, "retracted": retracted}
