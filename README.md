# 建立急诊节日值守与家属联络协同服务基础服务

本项目提供节日公共服务场景的通用后台基础层，负责组织、服务站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。领域模块可以在这些稳定边界之上增加自己的状态、规则和接口，而不必重复实现身份、站点与审计能力。

## 目录

- `src/festival_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/emergency_duty/`：急诊节日值守与家属联络模块（班次版本、岗位资格、换岗确认、联络授权、通知窗口投递与人工处置）；
- `tests/`：基础规则、事务边界、接口路由、急诊业务链和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m festival_foundation.acceptance
```

验收命令会在临时 SQLite 数据库中登记组织、操作者、站点和参考资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m festival_foundation.api --database festival_foundation.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。

## 急诊节日值守与家属联络模块

`emergency_duty` 建立在基础层之上，把班次、资格、授权与通知纳入同一业务链，全部状态落库并复用基础层的角色权限、`request_id` 幂等回执和哈希审计。

核心规则：

- **班次版本**：同院区同日期（按院区 IANA 时区计算，跨午夜夜班归入开始当天）重复发布自动递增版本，旧版本停用。
- **岗位资格**：发布班次要求责任人在班次起止区间持有有效资格；临时换岗在双方确认后，接任者资格仍有效才生效。
- **交班事项**：抢救中且患者处于 `rescue` 状态的关键事项既不能随换岗自动转移，也不能手动转移；脱离抢救后方可转移到跨科/跨岗位班次。
- **联络授权**：撤回立即翻转授权状态并阻断该授权下所有未发出的通知（含窗口等待与退避重试），但历史披露记录原样保留。
- **通知投递**：相同 `request_id` + 相同内容安全重放，相同编号不同内容返回 `delivery_conflict`；按授权通知窗口（支持跨午夜窗口）发送；通道失败按 1/5/15 分钟退避，最多 3 次，之后进入**有期限**人工处置队列（默认 2 小时），到期自动关闭，绝不无限重试。
- **重启恢复**：所有未决投递、重试时刻、人工任务均在 SQLite 中；重新启动服务（或后台维护线程）即继续处理。

### 主要后台接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/ed/qualifications`、`/ed/qualifications/revoke` | 登记/撤回岗位资格 |
| POST | `/ed/shifts` | 发布班次（自动版本化） |
| GET | `/ed/responsibility?site_id=` | 当前生效班次与各岗位当前责任人（含资格是否仍有效） |
| POST | `/ed/handovers`、`/ed/handovers/confirm`、`/ed/handovers/reject`、`/ed/handovers/retry` | 提议/双方确认/拒绝/重试临时换岗 |
| GET | `/ed/handovers`、`/ed/handovers/{id}` | 每次交班决定查询 |
| POST | `/ed/handover-items`、`/ed/handover-items/transfer`、`/ed/handover-items/complete` | 交班事项（抢救事项禁转） |
| POST | `/ed/authorizations`、`/ed/authorizations/revoke` | 家属联络授权与撤回 |
| GET | `/ed/authorizations/{id}/access` | 授权历史披露（撤回后仍可查） |
| POST | `/ed/notifications` | 提交通知（幂等，区分重放与内容冲突） |
| GET | `/ed/pending-contacts?site_id=` | 未决通知与待人工处置 |
| POST | `/ed/manual-tasks/resolve` | 结办人工处置任务 |
| POST | `/ed/maintenance/run` | 手动推进一次到期投递与到期任务 |
| GET | `/ed/duty?site_id=` | 当前责任人 + 可披露范围 + 未决联络聚合视图 |

### 急诊模块的测试与验收

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m emergency_duty.acceptance
```

验收脚本用临时数据库走完整链路：跨午夜班次版本化、无资格/抢救阻塞换岗、抢救事项禁转、授权撤回阻断而不抹历史、通知重放与内容冲突、窗口等待、退避重试转人工并到期关闭、进程重启后继续处理，最后校验哈希审计链。

### 急诊模块的 HTTP 服务

急诊模块与基础层共用同一服务、同一数据库：

```bash
PYTHONPATH=src python3 -m emergency_duty.api --database emergency_duty.sqlite3 --host 127.0.0.1 --port 8080
```

服务启动时同时拉起后台维护线程，周期性处理到期投递并关闭超期人工任务。
