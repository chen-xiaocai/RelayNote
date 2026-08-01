# RelayNote

RelayNote 是面向个人本机使用的 macOS 13+ 菜单栏待办调度器。它以 Python 3.12、PyObjC/AppKit、SQLite 和独立 Codex app-server 进程构建；DeepSeek Responses API 负责每五分钟审视一次待办队列。

当前 v0.1 仓库包含状态机、CAS/唯一自动租约、Ask FIFO、墙钟调度、工作区准备、Codex JSON-RPC 客户端、stdio Ask MCP、无状态 orchestrator 上下文和基础菜单栏入口。发布前仍须在目标 macOS 架构上完成真实 Codex 0.146.0 兼容门禁。

## 开发

```bash
uv sync --all-extras
uv run pytest
uv run relaynote --add "整理项目说明"
uv run relaynote --list
```

macOS 上直接从带有 `DEEPSEEK_API_KEY` 的终端运行 `uv run relaynote`。默认数据目录是 `~/Library/Application Support/RelayNote`，managed workspace 是 `~/RelayNote/Workspaces`。外网流量使用 `127.0.0.1:7890`，localhost/Unix socket 不走代理。

## 构建

在相应架构的 macOS 13+ 环境执行：

```bash
uv sync --frozen --all-extras
uv run python setup.py py2app
codesign --force --deep --sign - dist/RelayNote.app
```

产物只做 ad-hoc 签名，不做公证。验收/归档不会自动 commit、merge 或删除 worktree。

## 真实发布门禁

使用固定 `codex-cli 0.146.0` 手工验证 Unix socket、Ask MCP、thread start/resume、steer/interrupt、远程 TUI 接管、app-server 重连和 30 秒停止策略。任一核心门禁失败都不应发布。

## License

MIT
