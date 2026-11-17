# 数据字典

- run_id：规则版本与输入 SHA-256 前 12 位组成的稳定批次标识。
- line_no：从 1 开始的物理来源行号，不因隔离或排序改变。
- business_key：本项目声明的业务唯一键，始终按字符串存储。
- reason_code：隔离原因的稳定英文编码。
- accepted：进入业务计算的记录数。
- quarantined：保留证据但不参与业务计算的记录数。
- 数值：金额、税额、用量和权重使用 Decimal；对外序列化为字符串。
- 时间：存储为带偏移的 ISO 8601，分桶使用半开区间。
- 空值：缺失键、空串和业务哨兵分别记录。
- 幂等：完整输入字节与规则版本相同则复用 run_id，不新增结果。

## M7 中转保障容量

- 容量窗：机场 + UTC 半开区间 [window_start, window_end)，按转机点到达 UTC 分桶。
- version：容量窗单调递增版本号；缩容携带 expected_version，不一致返回 CAPACITY_VERSION_CONFLICT(409)。
- request_id：保障申请业务键；idempotency_key 保证重复提交复用同一申请，不重复占位。
- 申请状态：HELD（整链已占未确认）、WAITLISTED（整链候补）、CONFIRMED（全程保障已确认）、CANCELLED（撤销释放）、REJECTED（衔接无效等，不占容量）。
- 占位状态：HELD、CONFIRMED 为有效占用；RELEASED_SHRINK（缩容释放未确认）、RELEASED_CANCEL（撤销释放）。
- deficit 欠额：max(0, 已确认数 − 当前上限)；欠额 > 0 时 accepting_new=false，暂停一切新增占位，撤销已确认使确认数回落才清偿。
- 转机点：相邻航段的到达机场，按到达 UTC 落入容量窗；直飞单航段不占中转容量。
- 事件流：WINDOW_CREATE/WINDOW_SHRINK、SLOT_HELD/SLOT_CONFIRMED/SLOT_RELEASED_*、REQUEST_*、DEFICIT_OPENED，全部带容量版本与时间戳，支撑回读与对账。

