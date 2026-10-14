# BOSS 直聘技能：Codex 与 WorkBuddy 共用

唯一的真实目录及 Git 仓库：
`D:\Dev\skill\boss-zhipin-deliver`

两个应用通过 Windows 目录联接（Junction）读取同一份文件：

- Codex：`C:\Users\wxj20\.agents\skills\boss-zhipin-deliver`
- WorkBuddy：`C:\Users\wxj20\.workbuddy\skills\boss-zhipin-deliver`

这两个入口均指向上述真实目录，不是独立副本。修改代码、提交或回滚会立即影响两个入口，无需复制同步。
应用可能需要重新加载技能或重启才能刷新技能列表。技能执行需要可用的 WebBridge 和已登录的 Chrome 或 Edge。

## 保存修改

在真实目录执行 `git status`、`git diff`，核对后按需 `git add` 和 `git commit`。
避免两个 Agent 同时编辑同一份代码。投递第一版仍串行运行：旧入口使用 `boss-main`，新账户入口显式选择独立端点和 session，并以 begin/end 控制整轮占用。
回滚或切换版本应避开运行中的任务。

个人配置 `user_profile.json`、记录 `boss_applied.json`、Python 缓存及临时备份由 `.gitignore` 排除。
这些个人文件保留在本地；原始基线提交仍包含当时的配置，尚未改写历史。
原始回滚基线标签：`baseline-2026-10-13`。未配置远程仓库。

## 数据目录

本次仅统一技能代码与 Git，未迁移投递数据。
运行数据和 HTML 默认仍位于 `D:\Dev\code\my_github\boss-zhipin-assistant\boss-data`，也可通过 `BOSS_WORKDIR` 指定。
统一入口不会自动扩大 Agent 的文件写入或网络权限。

## 独立账户与串行切换

使用 `scripts/run_account.py --config <配置文件>` 显式选择账户。配置示例在 `examples/`，本机 A/Edge 和 B/Chrome 的配置、独立画像副本在 Git 忽略的 `accounts/a/`、`accounts/b/`。旧历史不自动迁移。

完整的扩展连接、身份绑定、begin/end、逐卡/批量命令及故障恢复步骤见 [账户操作说明](references/accounts.md)。本版不支持两个账户同时投递。

## 旧同步与恢复工具

`tools/manage_skill.py` 为原双副本布局保留。现在源和目标解析为同一真实目录，工具会拒绝运行，不再使用 `sync` 或 `restore`。
版本管理直接使用真实目录的 Git；恢复已跟踪文件前先保存当前未提交修改。
此前迁移产生的 `.restore-backups/` 和旧安装副本中的三个 `.bak-20261013` 文件已核对并清理。
