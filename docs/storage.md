# SQLite 存储

应用表使用 SQLite STRICT 类型（要求 SQLite 3.37 或更新版本），金额为 CNY 整数分。连接启用外键；`PRAGMA user_version` 保存 schema 版本 1。初始化在单个事务中创建所有表并设置版本，失败时全部回滚；已有同版本数据库保持原样，其他非零版本明确报错。

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
from tool_agent_lab.storage.database import connect, initialize_database, transaction

initialize_database("app.sqlite3")
with transaction("app.sqlite3") as connection:
    # 将同一业务动作的订单变更与账本写入放在此事务中。
    connection.execute("UPDATE orders SET refunded_amount_minor = ? WHERE order_id = ?", (12900, "ORD-1001"))
with connect("app.sqlite3") as connection:
    rows = connection.execute("SELECT * FROM orders").fetchall()
```

`transaction()` 使用 `BEGIN IMMEDIATE`，正常退出提交，代码或提交发生异常时回滚，退出后关闭连接。`connect()` 返回字典式 `sqlite3.Row`，用于读取；它使用 SQLite 自动提交模式，业务写入应通过 `transaction()`。事务块内使用 `execute()`/`executemany()`，避免 `executescript()` 隐式提交。调用者负责初始化；连接本身不创建父目录或业务表。

这些约束只保证持久化关联、类型及唯一性。确认归属、当前提案版本、快照与实际参数一致、是否批准/过期/已消费，以及订单政策规则由服务校验；直接 SQL 插入并不获得授权。这里的事务检查使用合成记录，不构成受保护退款执行演示。事件递增序号由写入方分配，数据库拒绝重复序号。
