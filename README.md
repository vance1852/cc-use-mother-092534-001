# 建立急诊节日值守与家属联络协同服务基础服务

本项目提供节日公共服务场景的通用后台基础层，负责组织、服务站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。领域模块可以在这些稳定边界之上增加自己的状态、规则和接口，而不必重复实现身份、站点与审计能力。

## 目录

- `src/festival_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/festival_duty/`：急诊节日值守与家属联络模块，覆盖班次版本、岗位资格、联络授权、通知窗口与交班决定；
- `tests/`：基础规则、事务边界、接口路由和端到端验收测试。

## 急诊节日值守与家属联络模块

`festival_duty` 复用基础层的操作者、站点、幂等回执与审计链，把以下规则纳入同一业务链：

- 班次按站点时区归属服务日，跨午夜班次归属班次开始所在日期，每次换岗生效后班次版本递增；
- 临时换岗要求接任者岗位资格在发起与生效时均有效，且换岗双方各自确认后才生效；
- 交班时抢救中的事项自动保留在原班次并记录决定，只能凭理由显式转移；
- 家属联络授权决定可披露范围（`identity_only` / `condition_summary` / `critical_updates` / `full`），撤回立即阻止后续披露，历史披露记录保留可查；
- 通知请求按 `request_id` 幂等：相同内容安全重放，不同内容返回冲突；投递限次重试，失败或错过通知窗口后进入有期限的人工处置，超期未办结则终止；
- 全部状态落在 SQLite，进程重启后通过 `recover()`（服务启动时自动执行）继续处理未完成事项。

主要接口（`/duty/` 前缀，写操作经 `X-Actor-Id` 标识操作者）：

- `POST /duty/shifts`、`GET /duty/shifts`、`GET /duty/shifts/current`：班次登记与当前责任人；
- `POST /duty/qualifications`、`POST /duty/qualifications/revoke`：岗位资格授予与撤回；
- `POST /duty/swaps`、`POST /duty/swaps/confirm`、`POST /duty/swaps/decline`：临时换岗；
- `POST /duty/authorizations`、`POST /duty/authorizations/revoke`、`GET /duty/authorizations/scope`：联络授权与可披露范围；
- `POST /duty/notifications`、`POST /duty/notifications/deliver`、`POST /duty/notifications/resolve`、`GET /duty/notifications/pending`、`GET /duty/disclosures`：通知请求、投递、人工办结、未决联络与披露历史；
- `POST /duty/handover-items`、`POST /duty/handover-items/status`、`POST /duty/handovers`、`POST /duty/handovers/transfer-item`、`GET /duty/handovers/decisions`：交班事项与交班决定；
- `POST /duty/maintenance/sweep`：手动推进未决联络（窗口已过转人工、处置超期终止）。

模块离线验收：

```bash
PYTHONPATH=src python3 -m festival_duty.acceptance
```

同时提供基础层与值守模块的 HTTP 服务：

```bash
PYTHONPATH=src python3 -m festival_duty.api --database festival_duty.sqlite3 --host 127.0.0.1 --port 8080
```

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
