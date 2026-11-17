# 航班时刻运行台

面向真实数据复核的中型数据工程工具，包含七个可独立调用的业务模块。规则、失败原因与证据字段见 docs/DATA-DICTIONARY.md，陷阱和测试映射见 docs/DEPTH.md。

## 启动

docker compose up --build

浏览器访问 http://localhost:18219。测试：docker compose exec -T app pytest -q

## 模块

- M1 航段采信：页面 /legs，接口 /api/legs/import
- M2 机场时区：页面 /airports，接口 /api/airports
- M3 当地时刻：页面 /local-time，接口 /api/local-time/resolve
- M4 UTC 航链：页面 /itineraries，接口 /api/itineraries/build
- M5 衔接判定：页面 /connections，接口 /api/connections/check
- M6 批次导出：页面 /batches，接口 /api/reconcile
- M7 中转保障容量：页面 /capacity，接口 /api/capacity/*

## M7 中转保障容量

机场按 UTC 半开时窗维护中转保障容量与单调递增的容量版本，接入 M1/M4/M5 的真实航段、UTC 换算与衔接判定，不改出票与当地时刻解析。

- 容量调整：`PUT /api/capacity/windows`，携带 `expected_version` 做乐观并发裁决，过期返回 409。
- 保障申请：`POST /api/capacity/apply`，整链获配（HELD）或整链候补（WAITLISTED）；衔接无效拒绝（REJECTED）且不占容量。
- 确认/撤销：`POST /api/capacity/requests/{id}/confirm|cancel`，撤销中断后续跑并按 FIFO 回填，重试不重复归还。
- 缩容：释放未确认占位转候补；已确认超额单列欠额（DEFICIT_OPENED），欠额清偿前该窗暂停新增占位。
- 回读：`GET /api/capacity/snapshot` 返回各时窗容量/欠额、申请状态、占位/确认/候补/释放进度与完整事件流。

## 运行规则

同一输入内容生成相同运行标识，重复执行复用已有批次。隔离记录包含原因码、原始行号与原值；业务导出只保留一份证据清单。

