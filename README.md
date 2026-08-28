# OpenClaw Mail Notifier（重要邮件通知）

零 token 的 IMAP 邮件过滤与推送工具。纯规则过滤、不落盘、不调 AI，通过 OpenClaw 的消息通道推送重要邮件通知。

## 前置条件

本工具依赖 **OpenClaw** 的消息通道进行推送。你需要：

1. 安装并运行 OpenClaw
2. 配置至少一个消息通道（微信或飞书）
3. 确保 `openclaw` CLI 可用（或在 `MAIL_CLI_PATH` 环境变量中指定路径）

## 功能

- **每小时检查**：IMAP 增量检查新邮件（`UID + 1:*`），只拉头部不拉正文（省流量）
- **纯规则过滤**：`HARD_SKIP` / `IMPORTANT` 列表，零 token 成本
- **双通道推送**：重要邮件通过 OpenClaw 同时推送微信 + 飞书
- **每日汇总**：17:00 输出今日邮件概况（三态：无新邮件/已推送重要/无重要）
- **汇总守卫**：微信通道故障时自动走飞书逃生通道补发
- **紧急邮件队列**：紧急发件人实时检测，多事件队列（不因已有事件丢弃后续）
- **飞书失败重试队列**：推送失败消息持久化，每次最多重试 10 条
- **TUN 代理绕过**：macOS 梯子开启时也能直连 IMAP（绑物理网卡 + 自解析 DNS）
- **显示名防伪造**：发件人解析真实邮箱地址后精确匹配，不信任显示名

## 文件结构

```
├── config.example.py     # 配置模板
├── mail_filter.py        # 核心：邮件过滤 + 推送
├── mail_sync.py          # 邮件完整同步（手动使用）
├── emergency_watch.py    # 紧急发件人增量检测
├── emergency_push.py     # 紧急事件队列与重复推送
├── feishu_notifier.py    # 飞书推送工具
├── daily_report_guard.py # 每日汇总投递守卫
├── mail_utils.py         # 共享模块：原子状态写入、文件锁、IMAP 响应校验
├── tests/
│   └── test_mail_scripts.py  # 回归测试（11 项）
├── README.md
├── LICENSE
└── .gitignore
```

## 快速开始

### 1. 配置 OpenClaw 消息通道

**微信通道（企业微信）：**
```bash
openclaw gateway config.patch '{"channels": {"openclaw-weixin": {...}}}'
```

**飞书通道：**
```bash
openclaw gateway config.patch '{"channels": {"feishu": {...}}}'
```

具体配置方式请参考 [OpenClaw 文档](https://docs.openclaw.ai)。

### 2. 配置脚本

```bash
# 复制配置模板
cp config.example.py config.py

# 编辑 config.py 填入你的配置
# 或设置环境变量（推荐，避免凭据落盘）
export QQ_MAIL_USER="your-email@qq.com"
export QQ_MAIL_PASS="your-authorization-code"
export FEISHU_TARGET="ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
export WECHAT_TARGET="xxx@im.wechat"
```

### 3. 测试

```bash
python3 mail_filter.py --list   # 只读列出新邮件（不推进断点）
python3 mail_filter.py          # 检查并推送重要邮件
```

### 4. 设置定时任务

```bash
# 每小时检查
0 * * * * cd /path/to/project && python3 mail_filter.py
# 每日 17:00 汇总
0 17 * * * cd /path/to/project && python3 mail_filter.py --daily
```

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `QQ_MAIL_USER` | ✅ | 邮箱地址 |
| `QQ_MAIL_PASS` | ✅ | IMAP 授权码 |
| `FEISHU_TARGET` | ❌ | 飞书用户 ID（需先在 OpenClaw 配置飞书通道） |
| `WECHAT_TARGET` | ❌ | 微信推送目标 ID（需先在 OpenClaw 配置微信通道） |
| `WECHAT_ACCOUNT` | ❌ | 微信推送账号 ID |
| `SUMMARY_JOB_ID` | ❌ | 汇总 cron job ID |
| `MAIL_CLI_PATH` | ❌ | OpenClaw CLI 路径，默认 `openclaw`（注意：不要用 `OPENCLAW_CLI`，与 gateway 内置变量冲突） |
| `MAIL_STATE_DIR` | ❌ | 状态目录，默认 `<项目根>/mail/state` |
| `EMERGENCY_SENDER` | ❌ | 精确匹配的紧急发件人邮箱地址 |
| `FEISHU_TIMEOUT_IS_SUCCESS` | ❌ | 飞书 CLI 超时时是否按已发送处理，默认 `1` |

## 推送通道说明

推送通过 OpenClaw 的消息通道实现，并非直接调用微信/飞书 API。

### 微信通道（稳定性 ⚠️ 不推荐）

微信开放平台的企业微信推送接口（OpenClaw 的 `openclaw-weixin` 通道）存在间歇性故障：
- 现象：`ret=-2 prepare failed`，消息无法推送
- 频率：每 1-2 周一次，持续 2-26 小时
- 原因：微信服务端 `prepare` 接口（aewebpodproxy.weixin.qq.com）不稳定
- 无法本地修复，需等待微信服务端自愈

**强烈建议优先配置飞书通道。**

### 飞书通道（推荐 ✅）

OpenClaw 的飞书消息通道相对稳定，推送失败时自动重试 3 次。

## 许可证

MIT
## 状态与可靠性

- 所有 JSON 状态采用原子替换写入（`tmp + fsync + replace`）并设置 `0600` 权限，避免进程中断生成半截文件。
- 定时脚本使用文件锁（`fcntl.LOCK_EX | LOCK_NB`），重叠执行时后启动的实例会安全退出。
- IMAP 单封拉取失败时不会越过该 UID 推进断点，避免永久漏信。
- 状态 JSON 损坏时明确报错停止，不静默重置断点。
- 飞书失败消息保留在 `filter_state.json` 的待重试队列中，每次最多重试 10 条。
- 紧急邮件由单事件状态升级为多事件队列，不因已有事件丢弃后续。
- 双通道均失败时保留紧急状态，不第一次失败就删除任务。
- `--list` 是只读诊断模式，不更新断点或每日统计。

## 兼容性

- Python 3.9+（已通过 `from __future__ import annotations` 兼容系统 Python 3.9）
- macOS / Linux

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖：原子写入、JSON 损坏、IMAP 响应校验、文件锁重叠、共享模块各工具函数。

## 备份

修改前会备份至 `backups/mail-script-audit-<timestamp>/`，包含所有受影响的文件副本。
