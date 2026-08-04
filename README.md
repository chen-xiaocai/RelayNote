# RelayNote

RelayNote 是一个本机优先的 macOS 菜单栏待办调度器。你只需写下一句简洁待办；每隔五分钟，调度器会审视清单、准备安全的工作目录并让 Codex CLI 执行。Codex 缺少细节时，通过不抢焦点的桌面悬浮窗向你提问。

## v0.1 能力

- 菜单栏弹窗：新增、查看、拖动优先级；自动执行中的任务固定置顶。
- 待办详情：完整原文、追加内容、问题和回答、Codex 进度、最终汇报与工作目录时间线。
- 全局单自动槽：`进行中`、`等待你`、`正在停止` 合计最多一个；已接管会话可并存。
- 全局 Ask FIFO：一次 Ask 调用可提交多个问题；阻塞问题优先于完成通知，每题实际展示后计时 120 秒，超时选择第一个选项。
- Codex app-server：独立任务进程、同一 thread 追加、30 秒安全停止、interrupt 兜底、远程 TUI 接管。
- DeepSeek 调度：`deepseek-v4-flash`、OpenAI Responses API、手写工具循环、持久上下文和 128k 压缩。
- 工作目录：由 orchestrator 根据待办内容用 `bash mkdir` 创建；RelayNote 不初始化或管理 Git 仓库。
- SQLite WAL、CAS 状态变更、工具幂等、完整原始 JSONL 记录和单实例锁。

v0.1 会把“每天做……”当成一次性待办；周期任务将在后续版本加入。验收只做软归档，不会删除文件或 Codex thread；RelayNote 不维护 Git 环境。

## 环境要求

- macOS 13 或更高版本
- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- `codex-cli 0.146.0`，并已通过用户自己的 Codex 登录
- 可选：VS Code 的 `code` 命令，用于接管时打开工作区
- `~/.relaynote/config.toml` 中配置 API key、base URL 和模型

Codex 的模型、provider、认证和已有 MCP 沿用用户当前 `~/.codex/config.toml`。RelayNote 启动的任务进程只禁用 Apps，并添加项目自带的 RelayNote Ask MCP，不注入代理环境变量。

## 安装与运行

```bash
uv sync --frozen --all-groups
uv run relaynote --doctor
uv run relaynote
```

请先创建 `~/.relaynote/config.toml`；没有 key 时菜单栏与手工管理仍可使用，但五分钟自动调度停用。

默认位置：

- 配置文件：`~/.relaynote/config.toml`
- 数据库与事件：配置中的 `data_dir`
- managed workspace：配置中的 `workspaces`

配置模板：

```toml
[api]
key = ""
base_url = "https://api.deepseek.com"
model = "deepseek-v4-flash"

[paths]
data_dir = "~/Library/Application Support/RelayNote"
workspaces = "~/RelayNote/Workspaces"
codex_path = "codex"
expected_codex_version = "codex-cli 0.146.0"
shell = ""
```

## CLI 诊断与管理

```bash
uv run relaynote --add "整理发布说明"
uv run relaynote --list
uv run relaynote --append TODO_ID "发布前还要跑 Intel 架构测试"
uv run relaynote --state TODO_ID suspended
uv run relaynote --start TODO_ID --workspace-dir /absolute/workspace
uv run relaynote --run-tick
uv run relaynote --doctor
```

`--start` 会一直等待该 Codex turn 结束后退出。`--workspace-dir` 必须指向已存在的目录。

## 状态语义

| 状态 | 含义 |
|---|---|
| 待完成 | 可由五分钟调度器接手 |
| 进行中 | Codex turn 正在运行 |
| 等待你 | Ask MCP 的问题已显示，Codex 被阻塞 |
| 正在停止 | 已要求 Codex 释放资源，最多等待 30 秒 |
| 挂起 | 自动调度不会接手 |
| 已完成 | Codex 已退出本轮，等待人工验收 |
| 异常 | 不自动重试，需人工触发 |
| 已接管 | 永久退出自动调度，由用户操作同一会话 |

完成通知中的“其他回答”会作为新指令沿用原 thread。自动槽空闲时立即开始新 turn；槽被占用时则回到最高优先级的待完成状态。

## 架构

```text
AppKit status item / popover / NSPanel
                 │
                 ▼
       Runtime + SQLite state machine
          │                 │
          │                 └── 5-minute scheduler ── DeepSeek Responses API
          │                                           └── serialized local tools
          ▼
per-task Codex app-server (Unix WebSocket, loopback fallback)
          │
          ├── v2 JSON-RPC thread / turn / item events
          └── stdio RelayNote Ask MCP ── 0600 Unix socket ── global question FIFO
```

DeepSeek Responses API 按无状态方式调用：每轮重新构造固定系统提示、压缩总结、压缩后的原始工具历史，并把最新待办/会话快照放在最底部。旧快照不会进入持久重放上下文。所有改变状态的工具串行执行，以 `run_id + call_id` 幂等。当模型退出响应的 `cached_input_tokens + input_tokens` 超过 128k 时触发压缩。

## 测试

```bash
uv run --python 3.12 pytest -q
```

测试覆盖状态机与单自动槽、旧库迁移、不可变追加、FIFO 展示计时、Ask socket、无状态工具循环、工具幂等、Codex v2 协议事件、配置加载、停止后归档/完成和多 MB 日志完整性。

正式发布前还必须在目标 macOS 架构用固定 Codex 版本手工验证：Unix socket、loopback fallback、Ask UI、同 thread resume/steer、30 秒停止、远程 TUI 接管与 app-server 重连。Linux 开发机只能验证 loopback WebSocket 握手。

## 构建

在对应架构的 macOS runner 执行：

```bash
uv sync --frozen --all-groups
uv run python setup.py py2app
codesign --force --deep --sign - dist/RelayNote.app
ditto -c -k --sequesterRsrc --keepParent dist/RelayNote.app RelayNote.zip
```

GitHub Actions 分别在 Apple Silicon `macos-15` 和 Intel `macos-15-intel` 构建。产物仅做 ad-hoc 签名，不做 notarization。

## 日志与安全

业务事件以完整 JSONL 写入，不截断字符串、数组、嵌套对象或堆栈，也不记录流程式日志。Authorization、API key、访问令牌和 IPC token 字段会被拒绝写入。Ask socket 的父目录权限为 0700、socket 为 0600，IPC token 只存在于进程环境中。

## License

MIT
