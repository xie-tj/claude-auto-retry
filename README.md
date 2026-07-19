# claude-auto-retry

[![Tests](https://github.com/xie-tj/claude-auto-retry/actions/workflows/tests.yml/badge.svg)](https://github.com/xie-tj/claude-auto-retry/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

为 Claude Code 提供 API 错误自动恢复能力：当 Claude Code 自带的重试机制最终仍因 **API 超时**或**上游服务过载**而失败时，自动恢复原会话并安全继续未完成的任务。

## 为什么需要它

Claude Code 会自行重试短暂的 API 故障，但当内置重试最终失败时，当前任务通常会停在：

```text
⏺ API Error: The operation timed out.
```

代理或网关也可能把服务过载包装成其他状态码，例如：

```text
⏺ API Error: 422 格式转换错误: Responses upstream service_unavailable_error:
Our servers are currently overloaded. Please try again later.
```

`claude-auto-retry` 不会重放原始 prompt，也不会重新执行整项任务。它会恢复同一个 Claude Code session，并发送一条安全 continuation，要求 Claude 先检查工作区和外部状态、复用已有结果，并避免重复已经完成的副作用操作。

## 功能

- 等待 Claude Code 内置重试结束，只在最终失败后介入。
- 支持 API timeout：默认等待 `5 / 15 / 30` 秒后继续。
- 支持 upstream overloaded：默认等待 `15 / 30 / 60` 秒后继续。
- timeout 与 overloaded 共用最多 3 次自动 continuation。
- 不会因为普通 HTTP 422 自动继续。
- 子代理失败只记录，不向父会话注入 continuation。
- 如果 Claude Code 的 `UserPromptSubmit` Hook 在倒计时结束前送达，用户手动提交的新 prompt 会取消待执行恢复。
- 手动输入与倒计时恰好同时发生时仍存在 tmux 竞态，不承诺原子取消。
- 五分钟以前的故障事件视为过期，避免电脑休眠后误提交。
- 十分钟没有连续故障后重置恢复计数。
- 不会自动添加 `--dangerously-skip-permissions` 或提升权限。
- 支持交互式终端、`claude -p`、JSON 和 `stream-json` 输出。
- 支持全局/单会话暂停、取消、查看状态、清理和完整卸载。
- 日志只保存时间、错误类别、恢复次数和动作，不保存 prompt、回复、工具输出、源码或完整错误文本。

## 工作原理

### 交互式会话

未来从终端启动的 `claude` 会运行在受管 tmux session 中：

1. Claude Code 的 `StopFailure` Hook 报告最终 API 失败。
2. Hook 仅将归一化类别 `timeout` 或 `overloaded` 发送给本地 watchdog。
3. watchdog 按退避策略倒计时。
4. 倒计时结束后，它将安全 continuation 粘贴到该会话的精确 tmux pane，等待 250 毫秒让 Claude Code TUI 完成粘贴处理，再发送首次 Enter。
5. 如果尚未出现任何 `UserPromptSubmit`，watchdog 会在首次 Enter 后约 250 毫秒快速补按一次，并在首次 Enter 后 5 秒进行最后一次补按。每次补按前都会重新检查 recovery provenance、暂停/取消状态和目标 pane identity。
6. 任何 `UserPromptSubmit` 都会停止后续补按：匹配 continuation 时进入等待回复状态，其他内容则视为用户接管并取消自动恢复。
7. 最后一次补按后再等待 5 秒；仍没有提交 Hook 时才显示 `not confirmed` 并停止自动按键。

一次 continuation 最多尝试三次 Enter，但仍只计为一次 `recovery n/3`。如果 Hook 已消费 recovery provenance、对应事件尚未同步到 watchdog，状态栏会显示 `submit detected · syncing acknowledgement`，期间不会继续补按。如果最终仍未确认且 continuation 还在输入框中，可以手动按一次 Enter；其一次性 provenance 在五分钟内仍有效，因此会被识别为同一次自动恢复，而不是新的人工任务。

终端高度至少 16 行时，底部会显示两行 watchdog 状态；较小终端自动改用 tmux pane border 状态栏。

如果 `Ctrl-b X` 尚未被占用，它会被绑定为“取消待执行恢复”。已有 tmux 绑定不会被覆盖。

### 非交互式会话

对于 `claude -p` 等非交互式调用：

- 启动时预分配 session UUID。
- 失败后使用官方 `--resume <session-id>` 恢复。
- 原始 prompt 和 stdin 只发送一次；含歧义输入参数（例如 `--file`）时会关闭外层恢复，而不是冒险重放任务。
- text/JSON 模式只向 stdout 输出最终一次结果。
- `stream-json` 保持实时，并且不会添加私有事件。
- 最终失败时返回最后一个底层 Claude 进程的退出码。

## 系统要求

- macOS 或 Linux
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
- Python 3.9 或更高版本
- tmux
- zsh、bash 或其他读取 `~/.profile` 的 shell

安装 tmux：

```bash
# macOS
brew install tmux

# Ubuntu / Debian
sudo apt-get update && sudo apt-get install -y tmux

# Fedora
sudo dnf install tmux

# Arch Linux
sudo pacman -S tmux
```

## 安装

### 推荐：先查看源码再安装

```bash
git clone https://github.com/xie-tj/claude-auto-retry.git
cd claude-auto-retry
./install.sh
```

### 一行安装

```bash
curl -fsSL https://raw.githubusercontent.com/xie-tj/claude-auto-retry/main/install.sh | bash
```

安装器会：

1. 找到当前官方 `claude`、`tmux` 和 Python 的真实路径。
2. 安装源码到 `~/.local/share/claude-auto/`。
3. 创建 `claude`、`claude-auto` 和 `claude-raw` 三个入口。
4. 合并五个全局 Claude Code Hooks，不覆盖已有 settings 或 Hooks。
5. 向当前 shell rc 添加带标记的 PATH 配置。
6. 运行离线自检。

安装后打开新终端，或运行安装器输出的 `source` 命令，然后验证：

```bash
command -v claude
claude-auto doctor
```

`command -v claude` 应指向：

```text
~/.local/claude-auto/bin/claude
```

> [!IMPORTANT]
> 安装只影响以后启动的 Claude Code 进程。安装前已经运行的会话不会被迁移或重启；全局 Hook 可以观察其错误，但不会猜测输入目标并自动注入。

## 使用

正常启动即可：

```bash
claude
```

命名受管会话：

```bash
claude-auto new --name my-project --
```

查看状态和会话：

```bash
claude-auto status
claude-auto list
claude-auto attach <session-name>
claude-auto logs <session-name>
```

取消待执行恢复：

```bash
claude-auto cancel <session-name>
```

在受管 tmux session 中也可以使用：

```text
Ctrl-b X
```

全局暂停和恢复：

```bash
claude-auto pause
claude-auto resume
```

暂停或恢复单个会话：

```bash
claude-auto pause <session-name>
claude-auto resume <session-name>
```

暂停会立即取消当前倒计时，但不会终止 Claude Code，也不会撤销已经执行的操作。

清理非活动状态：

```bash
claude-auto clean
```

## 直接运行官方 Claude Code

如果需要完全绕过自动恢复层：

```bash
claude-raw
```

`claude-raw` 仍会加载已有 Claude Code settings、MCP、插件、`CLAUDE.md` 和其他 Hooks；它只让本项目的 Hooks 不执行。

以下 Claude Code 模式也会自动旁路外层恢复：

- `--safe-mode`
- `--bare`
- `--bg` / `--background`
- 官方 `--tmux`
- `--no-session-persistence`
- `--input-format stream-json`
- `--max-budget-usd`

`auth`、`doctor`、`mcp`、`plugin`、`install`、`update` 等管理命令会直接交给官方 CLI。

## 权限和副作用安全

本项目绝不会自行添加：

```text
--dangerously-skip-permissions
```

只有用户显式输入该参数时，原会话才使用它。自动恢复层不提升权限。

continuation 会要求 Claude：

- 先检查工作区和可观察的外部状态。
- 复用已完成结果。
- 不重复成功的命令、文件写入或远程操作。
- 对删除、推送、部署、支付等副作用，先确认先前操作是否完成。
- 如果无法安全确认副作用状态，停止并说明。

这是一层安全提示和状态检查策略，不是数据库事务或 exactly-once 保证。任何基于 tmux 的输入注入都存在终端状态竞态：倒计时结束时如果用户仍在输入，自动恢复可能与人工输入冲突；本项目无法原子地读取或锁定 Claude Code 的 TUI 输入框。如果任务包含高风险副作用，请保持人工监督，或在输入前先运行 `claude-auto cancel <session-name>`。

## 错误匹配

结构化错误分类优先。文本回退只匹配稳定短语：

### Timeout

- `the operation timed out`
- `request timed out`
- 结构化 `timeout` / `request_timeout`

### Overloaded

- 结构化 `overloaded`
- `service_unavailable_error`
- `servers are currently overloaded`

匹配不区分大小写并折叠空白。不使用模糊匹配，也不提供任意用户正则。

以下内容本身不会触发恢复：

```text
API Error: 422
格式转换错误
```

## 不启用外层恢复的情况

- `--max-budget-usd`：跨进程重复预算上限可能增加总成本。
- `--no-session-persistence`：无法使用 session resume。
- `--input-format stream-json`：由上游控制器负责重试和输入语义。
- 子代理的 timeout/overloaded：交给父 Claude 处理失败或部分结果。
- Ctrl-C 或用户主动取消。交互式受管会话中断 attach 时会写入会话取消标记，watchdog 不会继续执行当时待发送的 continuation。

## 升级

重新运行最新安装器即可；安装过程是幂等的，不会重复添加 Hooks 或 PATH 块：

```bash
cd claude-auto-retry
git pull
./install.sh
```

Claude Code 版本变化后，下一次启动会自动执行离线兼容性检查。检查失败时自动注入会停用，但观察和状态信息仍保留。

本项目不会自行下载或执行自动更新。

## 卸载

先预览：

```bash
claude-auto uninstall --dry-run
```

确认后卸载：

```bash
claude-auto uninstall
```

卸载会删除：

- 本项目添加的五个 Hook
- 带标记的 PATH 块
- 三个命令入口
- 本项目源码、配置和运行状态

卸载会保留：

- Homebrew
- tmux
- 官方 Claude Code 安装
- 其他 Claude Code settings 和 Hooks

如果仍有活动的受管会话，卸载会拒绝执行，不会终止或接管现有会话。

## 隐私

本地事件日志只包含：

- 时间戳
- 不透明 run ID / 受管 session 名称
- `timeout` 或 `overloaded`
- 恢复计数
- 动作或状态

不会写入：

- prompt 或完整回复
- 工具输出和源码
- stdin
- 完整 API 错误
- MCP JSON、启动参数或凭据

启动参数通过位于当前用户私有 `0700` IPC 目录中的一次性 Unix socket 传递；默认目录为 `/tmp/claude-auto-<uid>`，socket 文件权限为 `0600`，内容不会写入日志。自定义 IPC 目录必须足够短以满足 Unix socket 路径限制，安装器会提前拒绝过长路径。

正常退出会删除当前 session 日志。异常残留最多保留 24 小时；临时输出文件最多保留 1 小时。

## 开发与测试

项目只使用 Python 标准库：

```bash
python3 -m py_compile src/claude_auto.py
python3 -m unittest discover -s tests -t . -v
```

测试使用隔离 HOME 和假的 Claude/tmux，不会修改真实用户配置，也不会调用 API。

## 已知限制

- 交互式恢复依赖 tmux。
- Finder 启动的 IDE 服务、嵌入式 IDE Claude 集成及使用绝对官方二进制路径的程序不会被 PATH shim 强制接管。
- Claude Code 没有公开“当前 TUI 输入框状态”的 API，因此 tmux 输入注入无法做到严格事务级安全。
- 本项目不是 Anthropic 官方产品。

## License

[MIT](LICENSE)
