# SQLite 存储

应用表使用 SQLite STRICT 类型（要求 SQLite 3.37 或更新版本），金额为 CNY 整数分。连接启用外键；`PRAGMA user_version` 保存 schema 版本 2。初始化在单个事务中按顺序应用尚未执行的迁移：空库创建全部表，版本 1 保留记录并升级到版本 2；失败时结构、数据与版本全部回滚。已有同版本数据库保持原样，更新的未知版本明确报错。

`attempts` 保存 `execution_epoch`（初值 0、非负领取版本）、`lease_worker_id`（持有 worker）、`lease_expires_at`（租约截止）和 `heartbeat_at`（最近心跳）。未持有租约时后三项均为空；持有时三项完整且 epoch 大于 0。释放租约保留 epoch，重新领取时由运行时递增；时间由运行时写入带时区的时间。截止字段有索引用于过期查找。这些字段是持久化基础，当前 worker 仍使用既有互斥领取，自动心跳、过期恢复和业务提交时的执行权检查尚未接入。

| 表 | 保存内容与约束 |
|---|---|
| orders | 模拟订单、支付/退款/补偿金额；退款累计金额在 0 与实付之间 |
| tasks / attempts | 归属、状态、尝试、唯一图线程与模型/配置版本；当前尝试必须属于同一任务 |
| proposals | 以提案 ID 和正整数版本共同标识，参数 JSON 保留政策引用 |
| approvals | 唯一请求 ID、同一提案版本的一次决定、完整提案 JSON 快照与消费时间 |
| operations | 调用前可保存 pending 操作键、确认请求、金额、状态、提交时间及结果 JSON；一份确认绑定一个操作键 |
| events | 按任务唯一的正整数序号、尝试身份、工具 call_id 和至多 16 KiB 的 JSON payload |

同一订单成功退款或成功补偿各最多一笔，改换操作键也受唯一索引限制；退款与补偿可以同时存在。pending/failed 记录不占成功业务名额。历史业务操作允许省略任务与尝试，仍需订单归属及确认记录；当前运行的两项身份须一起保存。无确定订单的转人工允许空订单，金额为空。

```python
from datetime import datetime, timezone

from tool_agent_lab.schemas.events import EventType
from tool_agent_lab.schemas.tasks import Attempt, Task
from tool_agent_lab.storage.database import connect, initialize_database, transaction
from tool_agent_lab.storage.event_repository import EventRepository
from tool_agent_lab.storage.task_repository import TaskRepository

initialize_database("app.sqlite3")
now = datetime.now(timezone.utc)
task = Task(task_id="task-example", owner_id="demo-user", user_message="查询订单", created_at=now)
attempt = Attempt(task_id=task.task_id, owner_id=task.owner_id, attempt_id="attempt-example",
                  thread_id="thread-example", model_version="manual", config_version="app-v1", created_at=now)
with transaction("app.sqlite3") as connection:
    tasks = TaskRepository(connection)
    tasks.add_task(task)
    tasks.add_attempt(attempt)
    tasks.set_current_attempt(task.task_id, task.owner_id, attempt.attempt_id)
    EventRepository(connection).append(attempt, EventType.TASK_STATUS_CHANGED, now, payload={"status": "queued"})
with connect("app.sqlite3") as connection:
    stored = TaskRepository(connection).get_task(task.task_id, task.owner_id)
    events = EventRepository(connection).list_events(task.task_id, task.owner_id, after_seq=0)
```

`transaction()` 使用 `BEGIN IMMEDIATE`，正常退出提交，代码或提交发生异常时回滚，退出后关闭连接。`connect()` 返回字典式 `sqlite3.Row`，用于读取；它使用 SQLite 自动提交模式，业务写入应通过 `transaction()`。事务块内使用 `execute()`/`executemany()`，避免 `executescript()` 隐式提交。调用者负责初始化；连接本身不创建父目录或业务表。

## Repository 接口

三个 repository 接收同一个 `sqlite3.Connection`，不自行打开连接、提交或回滚。读取可使用 `connect()`；所有写方法要求连接已进入显式事务，否则报错，防止自动提交拆散关联操作。使用 `transaction()` 提供的立即写事务完成多表操作。

| 接口 | 行为 |
|---|---|
| TaskRepository | 保存/查询任务与尝试、绑定当前尝试、更新任务状态；按归属、状态及分页列任务；追加提案版本，查询指定或最新版本；保存/读取确认快照，批准记录单次消费及读取消费时间 |
| BusinessRepository | 保存/查询 Order 与 Operation；严格整数分的余额更新；按原操作键/订单成功动作查询账本；将 pending 操作完成为 succeeded 或 failed，保留终态结果 |
| EventRepository | 对照持久化尝试身份追加 Event，分配任务递增 seq；按归属、after_seq（不含游标本身）和 limit 顺序读取 |

读取返回共享 Pydantic 契约；缺失或不属于指定 owner 的单项返回 `None`，列表返回空列表。owner 应由服务端提供。普通更新/确认消费返回布尔值，未匹配归属或已消费/终态时返回 `False`；重复新增保留 SQLite 的唯一约束异常，不覆盖旧记录。未知记录和未消费确认的消费时间都返回 `None`，需要区分时先读取确认记录。

提案按版本追加；省略 version 查询该提案最高版本。保存确认时必须与该归属下已保存的提案快照相同，旧版快照不会被新版覆盖。确认消费只更新已批准且未消费的记录；拒绝、错任务或错归属都不消费。事件序号跨尝试延续；使用立即写事务分配，失败回滚后该序号可重新使用，数据库仍拒绝重复序号。

`schemas/business.py` 的 Order 与 Operation 定义订单与账本记录，金额严格为 CNY 整数分，时间带时区。成功操作必须有提交时间，pending/failed 没有提交时间；转人工无金额，退款/补偿有订单与正整数金额。原始结果保留为 JSON，中文正常往返。

这些接口用于存储。当前提案版本、过期时间、实际调用参数与批准快照、归属及政策资格仍由业务/确认服务集中复核；存储一条批准记录或单次消费并不获得业务执行授权。余额与账本需由服务在同一事务内更新，repository 不自动执行退款规则。测试中的合成确认与 SQL 事务不构成受保护退款或故障恢复流程演示。任务/尝试状态转换、API/SSE 连接及 checkpoint 也由相应服务管理。
