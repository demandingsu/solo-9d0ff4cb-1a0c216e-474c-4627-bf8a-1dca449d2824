"""端到端实测脚本（仅用标准库，可 `python3 -I` 运行）。

覆盖：
1. 预登记（学员可去多个考场）
2. 并行交册（并发 POST，服务端串行核定）
3. 补交空册：显式 [] 算已交，触发缺考净增
4. 同批跨场更正：一批同时覆盖多场，缺考净增/净减同时发生
5. 拒收：版号未高于已交版号、列了不能去该场的学员、同批号换内容
6. 幂等：同批号原样再交取回原回执
7. 缺考名单查询、交册状态查询
8. 漏传 student_ids：多场同批任一册漏传即整批拒收、数据原状、
   补齐后同批号可重交；升级后批次重交同样须带齐名单
9. 旧批次重交：升级前收的批次（漏传名单已存成 []）按旧口径核对，
   原样重交取回原回执，改内容仍拒收

第 9 项需要把一批“旧格式”数据直接写库（模拟升级前已收的批次）：
- 服务跑在 Docker Compose 时：默认经 `docker compose exec` 在容器内写库；
- 本地直跑 uvicorn 时：设 ABSENCE_DB_PATH 指向同一个 SQLite 文件，
  脚本直接写库（SEED_MODE=local 可强制，SEED_MODE=docker 强制走容器）。
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
SID = os.environ.get("SESSION_ID", f"e2e-{int(time.time())}")
COMPOSE_FILE = pathlib.Path(__file__).resolve().parent.parent / "docker-compose.yml"

passed = 0
failed = 0


def call(method: str, path: str, body: dict | None = None,
         expect_error: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            obj = json.loads(resp.read().decode())
            status = resp.status
    except urllib.error.HTTPError as e:
        obj = json.loads(e.read().decode())
        status = e.code
    if expect_error is not None:
        assert status >= 400, f"期望失败却成功: {method} {path} -> {obj}"
        assert obj["error"] == expect_error, (
            f"期望错误 {expect_error}，实际 {obj}")
        ok(f"拒收符合预期: {expect_error}")
        return None
    assert status < 400, f"请求失败: {method} {path} -> {status} {obj}"
    return obj


def ok(msg: str) -> None:
    global passed
    passed += 1
    print(f"  ✓ {msg}")


def check(cond: bool, msg: str) -> None:
    global failed
    assert cond, msg
    ok(msg)


def seed_legacy_batch(batch_id: str, session_id: str,
                      rosters: list[dict], receipt: dict) -> None:
    """以旧版格式直接落库一批，模拟升级前已收的批次。

    旧版行为：漏传的 student_ids 被当空册存成 []；batch 行没有
    format_version 概念（迁移后取默认值 1=旧格式）。这里完全按旧口径
    生成 request_json / payload_hash，且不显式写 format_version。
    """
    stored = {"batch_id": batch_id, "session_id": session_id,
              "rosters": rosters}
    request_json = json.dumps(stored, ensure_ascii=False, sort_keys=True)
    blob = json.dumps(stored, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    row = [batch_id, session_id, hashlib.sha256(blob).hexdigest(),
           request_json, json.dumps(receipt, ensure_ascii=False)]

    mode = os.environ.get("SEED_MODE", "auto")
    if mode == "auto":
        mode = "local" if os.environ.get("ABSENCE_DB_PATH") else "docker"
    if mode == "local":
        conn = sqlite3.connect(os.environ["ABSENCE_DB_PATH"])
        try:
            conn.execute(
                "INSERT INTO batch(batch_id, session_id, payload_hash,"
                " request_json, receipt_json) VALUES(?, ?, ?, ?, ?)", row)
            conn.commit()
        finally:
            conn.close()
    else:
        script = (
            "import json,os,sqlite3,sys;"
            "r=json.loads(sys.argv[1]);"
            "c=sqlite3.connect(os.environ.get('ABSENCE_DB_PATH',"
            "'/data/absence.db'));"
            "c.execute('INSERT INTO batch(batch_id,session_id,payload_hash,"
            "request_json,receipt_json) VALUES(?,?,?,?,?)',r);"
            "c.commit();c.close()"
        )
        subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE),
             "exec", "-T", "api", "python", "-c", script, json.dumps(row)],
            check=True,
        )


def main() -> None:
    print(f"== 目标 API: {BASE}，期: {SID} ==")

    # ---------- 1. 预登记 ----------
    # 考场 A、B；学员 s1~s4
    # s1 可去 A/B；s2 可去 A；s3 可去 A/B；s4 只可去 B
    call("POST", "/admin/sessions", {
        "session_id": SID,
        "name": "补考第一期",
        "rooms": [{"room_id": "A", "name": "第一考场"},
                 {"room_id": "B", "name": "第二考场"}],
        "students": [
            {"student_id": "s1", "name": "张三", "room_ids": ["A", "B"]},
            {"student_id": "s2", "name": "李四", "room_ids": ["A"]},
            {"student_id": "s3", "name": "王五", "room_ids": ["A", "B"]},
            {"student_id": "s4", "name": "赵六", "room_ids": ["B"]},
        ],
    })
    ok("预登记成功：2 个考场、4 名学员（含多考场学员）")

    # ---------- 2. 并行交册 ----------
    # A 册（v1）：s1、s2 在场；B 册（v1）：s3 在场
    def submit(body):
        return call("POST", "/rosters/batches", body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(submit, {
            "batch_id": "B-A1", "session_id": SID,
            "rosters": [{"room_id": "A", "version": 1,
                         "student_ids": ["s1", "s2"]}]})
        f2 = pool.submit(submit, {
            "batch_id": "B-B1", "session_id": SID,
            "rosters": [{"room_id": "B", "version": 1,
                         "student_ids": ["s3"]}]})
        r_a, r_b = f1.result(), f2.result()
    check(r_a["status"] == "accepted" and r_b["status"] == "accepted",
          "并行交册两批均被接收（服务端串行核定）")
    # 两场都已交，开始判定：
    # s1 在 A 最新版 -> 不缺考；s2 在 A -> 不缺考；
    # s3 在 B -> 不缺考；s4 只可去 B，B 最新版无 s4 -> 缺考
    absent = call("GET", f"/sessions/{SID}/absences")["absent_students"]
    check([x["student_id"] for x in absent] == ["s4"],
          "两场交册后缺考名单仅 s4")

    # ---------- 3. 补交空册 ----------
    # B 场补交 v2 空册：空册也算已交，B 最新版变为无人在场
    r = call("POST", "/rosters/batches", {
        "batch_id": "B-B2-empty", "session_id": SID,
        "rosters": [{"room_id": "B", "version": 2, "student_ids": []}]})
    check(r["rooms_accepted"][0]["present_count"] == 0,
          "空册被接收，present_count=0")
    # s1 仍在 A 最新版在册 -> 不缺考；s3 在 A 无记录、B 空册 -> 新缺考；
    # s4 只可去 B，B 空册 -> 维持缺考
    check(r["added_absent"] == ["s3"] and r["removed_absent"] == [],
          f"补交空册回执：净增 {r['added_absent']}（仅 s3），无净减")
    absent = call("GET", f"/sessions/{SID}/absences")["absent_students"]
    check(sorted(x["student_id"] for x in absent) == ["s3", "s4"],
          "空册生效后缺考名单为 s3、s4（s1/s2 仍在 A 在场）")
    rooms = call("GET", f"/sessions/{SID}/rooms")["rooms"]
    b_room = next(x for x in rooms if x["room_id"] == "B")
    check(b_room["submitted"] is True and b_room["version"] == 2
          and b_room["present_count"] == 0,
          "交册状态显示 B 场 v2 已交、在册 0 人（空册算已交）")

    # ---------- 4. 同批跨场更正 ----------
    # 更正前：A v1=[s1,s2]，B v2=空册；缺考 s3、s4
    # 一批同时交 A v2（去掉 s2）与 B v3（补登 s1、s3、s4）：
    # s2：只能去 A，A v2 已无 s2 -> 新缺考（净增）
    # s3：B v3 补登 -> 不再缺考（净减）
    # s4：B v3 补登 -> 不再缺考（净减）
    # s1：A v2 仍在册 -> 维持不缺考
    r = call("POST", "/rosters/batches", {
        "batch_id": "B-FIX", "session_id": SID,
        "rosters": [
            {"room_id": "A", "version": 2, "student_ids": ["s1"]},
            {"room_id": "B", "version": 3,
             "student_ids": ["s1", "s3", "s4"]},
        ]})
    check(len(r["rooms_accepted"]) == 2, "同批两场签到册一并接收")
    check(r["added_absent"] == ["s2"],
          f"跨场更正净增缺考: {r['added_absent']}（s2）")
    check(set(r["removed_absent"]) == {"s3", "s4"},
          f"跨场更正净减缺考: {r['removed_absent']}（s3、s4）")
    check(r["absence_after"] == ["s2"],
          f"回执 absence_after={r['absence_after']}")
    absent = call("GET", f"/sessions/{SID}/absences")["absent_students"]
    check([x["student_id"] for x in absent] == ["s2"],
          "更正后缺考名单仅 s2（名单与签到册同事务生效）")

    # ---------- 5. 拒收：版号未高于已交版号 ----------
    call("POST", "/rosters/batches", {
        "batch_id": "B-OLD", "session_id": SID,
        "rosters": [{"room_id": "A", "version": 2, "student_ids": ["s2"]}]},
        expect_error="version_not_higher")
    rooms = call("GET", f"/sessions/{SID}/rooms")["rooms"]
    a_room = next(x for x in rooms if x["room_id"] == "A")
    check(a_room["batch_id"] == "B-FIX", "被拒批次未改动任何已交数据")

    # ---------- 6. 拒收：列了不能去该场的学员 ----------
    call("POST", "/rosters/batches", {
        "batch_id": "B-BAD", "session_id": SID,
        "rosters": [{"room_id": "B", "version": 4,
                     "student_ids": ["s2"]}]},  # s2 只能去 A
        expect_error="student_not_allowed")
    rooms = call("GET", f"/sessions/{SID}/rooms")["rooms"]
    b_room = next(x for x in rooms if x["room_id"] == "B")
    check(b_room["version"] == 3, "非法学员导致整批拒收，B 场仍停留在 v3")

    # ---------- 7. 同批号：原样再交取回回执，换内容拒收 ----------
    original = call("GET", f"/rosters/batches/B-FIX")
    resent = call("POST", "/rosters/batches", {
        "batch_id": "B-FIX", "session_id": SID,
        "rosters": [
            {"room_id": "A", "version": 2, "student_ids": ["s1"]},
            {"room_id": "B", "version": 3,
             "student_ids": ["s1", "s3", "s4"]},
        ]})
    check(resent == original, "同批号原样再交，取回与原回执完全一致")
    call("POST", "/rosters/batches", {
        "batch_id": "B-FIX", "session_id": SID,
        "rosters": [{"room_id": "A", "version": 9, "student_ids": []}]},
        expect_error="batch_id_conflict")
    ok("同批号换内容被拒收")

    # ---------- 8. 漏传 student_ids：整批拒收，补齐后同批号重交 ----------
    # 当前状态：A v2=[s1]，B v3=[s1,s3,s4]；缺考名单仅 s2
    # 同批两场：A 显式空册 + B 漏传名单 -> 任一册漏传，整批拒收
    call("POST", "/rosters/batches", {
        "batch_id": "B-MISS", "session_id": SID,
        "rosters": [
            {"room_id": "A", "version": 3, "student_ids": []},
            {"room_id": "B", "version": 4},  # 漏传 student_ids
        ]}, expect_error="missing_student_ids")
    rooms = call("GET", f"/sessions/{SID}/rooms")["rooms"]
    a_room = next(x for x in rooms if x["room_id"] == "A")
    b_room = next(x for x in rooms if x["room_id"] == "B")
    check(a_room["version"] == 2 and b_room["version"] == 3,
          "漏传整批拒收后各场版号原状（A v2、B v3）")
    absent = call("GET", f"/sessions/{SID}/absences")["absent_students"]
    check([x["student_id"] for x in absent] == ["s2"],
          "漏传整批拒收后缺考名单原状（仅 s2）")

    # 补齐名单后同一批号重交 -> 接收；A 显式 [] 仍按空册收下
    # （s2 只能去 A，故 B 册补登 s1、s3、s4）
    r = call("POST", "/rosters/batches", {
        "batch_id": "B-MISS", "session_id": SID,
        "rosters": [
            {"room_id": "A", "version": 3, "student_ids": []},
            {"room_id": "B", "version": 4,
             "student_ids": ["s1", "s3", "s4"]},
        ]})
    check(r["status"] == "accepted" and len(r["rooms_accepted"]) == 2,
          "补齐后同一批号重交被接收（拒收不占批号）")
    a_acc = next(x for x in r["rooms_accepted"] if x["room_id"] == "A")
    check(a_acc["present_count"] == 0,
          "显式 [] 仍算空册（A v3 在册 0 人）")
    # s2 只能去 A，A v3 为空册 -> 维持缺考；其余学员均在 B v4 在册
    check(r["added_absent"] == [] and r["removed_absent"] == []
          and r["absence_after"] == ["s2"],
          f"重交回执：缺考无净增净减，absence_after={r['absence_after']}")

    # 升级后收下的批次：重交也须带齐名单（规则随批次落库，重启仍生效）
    call("POST", "/rosters/batches", {
        "batch_id": "B-MISS", "session_id": SID,
        "rosters": [
            {"room_id": "A", "version": 3, "student_ids": []},
            {"room_id": "B", "version": 4},  # 重交仍漏传 -> 拒收
        ]}, expect_error="missing_student_ids")
    ok("升级后批次重交漏传名单同样被拒")
    again = call("POST", "/rosters/batches", {
        "batch_id": "B-MISS", "session_id": SID,
        "rosters": [
            {"room_id": "A", "version": 3, "student_ids": []},
            {"room_id": "B", "version": 4,
             "student_ids": ["s1", "s3", "s4"]},
        ]})
    check(again == r, "升级后批次带齐名单原样重交，取回与原回执一致")

    # ---------- 9. 旧批次重交（升级前已收的批次） ----------
    legacy_session = f"{SID}-legacy"
    call("POST", "/admin/sessions", {
        "session_id": legacy_session,
        "rooms": [{"room_id": "C"}],
        "students": [{"student_id": "s8", "room_ids": ["C"]},
                     {"student_id": "s9", "room_ids": ["C"]}],
    })
    legacy_receipt = {
        "batch_id": "B-LEGACY", "session_id": legacy_session,
        "status": "accepted",
        "rooms_accepted": [
            {"room_id": "C", "version": 1, "present_count": 0}],
        "absence_before": [], "absence_after": ["s8", "s9"],
        "added_absent": ["s8", "s9"], "removed_absent": [],
    }
    # 旧库落库形态：漏传的 student_ids 已被存成 []
    seed_legacy_batch("B-LEGACY", legacy_session,
                      [{"room_id": "C", "version": 1, "student_ids": []}],
                      legacy_receipt)
    ok("已按旧格式落库一批（升级前收的，漏传名单存成 []）")

    # 原样重交（仍漏传 student_ids）-> 按旧口径核对匹配，返回原回执
    r = call("POST", "/rosters/batches", {
        "batch_id": "B-LEGACY", "session_id": legacy_session,
        "rosters": [{"room_id": "C", "version": 1}]})
    check(r == legacy_receipt, "旧批次原样重交（仍漏传名单）取回原回执")
    # 显式空册重交 -> 旧口径下与漏传同形（[]），同样匹配原回执
    r = call("POST", "/rosters/batches", {
        "batch_id": "B-LEGACY", "session_id": legacy_session,
        "rosters": [{"room_id": "C", "version": 1, "student_ids": []}]})
    check(r == legacy_receipt, "旧批次显式空册重交，同样匹配原回执")
    # 改了内容（补上名单）-> 仍拒收
    call("POST", "/rosters/batches", {
        "batch_id": "B-LEGACY", "session_id": legacy_session,
        "rosters": [{"room_id": "C", "version": 1, "student_ids": ["s8"]}]},
        expect_error="batch_id_conflict")
    ok("旧批次改内容重交仍被拒收")

    print(f"\n全部通过：{passed} 项断言，失败：{failed} 项")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\n断言失败: {e}", file=sys.stderr)
        sys.exit(1)
