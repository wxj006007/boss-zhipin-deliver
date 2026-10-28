# BOSS 直聘自动化投递 Skill

读取完整岗位 JD，由运行本技能的智能体结合本地画像审核匹配度、生成专属招呼语，通过 Kimi WebBridge 发送并回读核验，最后生成 HTML 汇总。支持任意多个本地账户；账户身份与浏览器连接分别配置。

## 快速开始

需要 Python 3.10+、Kimi WebBridge、已启用 Kimi 扩展的 Chrome 或 Edge，以及能读取技能文件并执行本地命令的智能体。Python 脚本运行仅使用标准库；部分测试还需要 Node.js 来验证页面脚本。

在 clone 后的项目根目录执行（PowerShell 示例）：

```powershell
New-Item -ItemType Directory -Path accounts/me, connections
Copy-Item examples/account.json accounts/me/account.json
Copy-Item examples/profile.json accounts/me/profile.json
Copy-Item examples/connection-chrome.json connections/chrome.json
```

修改本地账户别名，填写自己的画像，确认连接端口与浏览器扩展设置一致。`expected_identity` 先保持 null，随后通过 inspect / bind 读取并确认真实登录身份。

```powershell
python scripts/manage_accounts.py --config accounts/me/account.json --connection connections/chrome.json bridge-start
python scripts/manage_accounts.py --config accounts/me/account.json --connection connections/chrome.json inspect
# 核对 inspect 显示的本人身份，再使用它返回的完整 confirm 值。
python scripts/manage_accounts.py --config accounts/me/account.json --connection connections/chrome.json --confirm <confirm> bind
python scripts/run_account.py --config accounts/me/account.json --connection connections/chrome.json begin
```

完整的配置、投递、换浏览器与故障处理见 [账户和连接说明](references/accounts.md)。智能体执行任务前阅读 [SKILL.md](SKILL.md)。克隆仓库不会带入任何预先绑定的账户，模板不能直接用于发送。

## 账户与连接

- 账户保存稳定身份、画像路径、独立历史目录；账户数量和别名不写死。
- 连接保存浏览器类型、WebBridge 端点和 session；同一账户结束一轮后可选择其他连接，继续使用原历史。
- 不同账户可以顺序复用同一端点，前提是先结束原运行，并由用户在浏览器切换登录身份。程序会核验身份，不代替用户登录。
- 并行运行需要独立端点和独立登录环境。两个 Chrome 也可配置为独立连接；不同 session 不能隔离同一浏览器的 Cookie。
- 从 begin 到 end 独占账户和端点；命令另有进程锁。错误身份、旧 run、跨账户历史写入和不确定发送继续被拦截。

## 代码结构

| 模块 | 职责 |
|---|---|
| `scripts/accounts.py`、`manage_accounts.py` | 账户身份与历史归属、注册、锁、账户管理 CLI |
| `scripts/connections.py`、`browser_session.py` | 独立连接配置、浏览器身份核验 |
| `scripts/execution_context.py` | 投递代码消费的只读运行契约，不读取注册表 |
| `scripts/run_account.py`、`account_worker.py` | 装配账户、连接和业务，创建运行、启动子命令并收尾 |
| `scripts/immediate.py`、`_live_driver.py` | 默认逐岗读取、智能体决策与投递 |
| `scripts/greeting.py`、`delivery_tab.py`、`chat_tab.py`、`list_return.py` | 保留列表、独立标签页发送、核验与返回 |
| `scripts/config_v3.py`、`hr_activity.py`、`job_list_loading.py` | 画像、活跃度筛选和后台加载 |
| `scripts/phase_stream.py`、`chat_audit.py` | 汇总、送达与聊天核对 |
| `scripts/page_scan.py`、`harvest_pool.py`、`pipeline_batch.py` | 按用户选择使用的兼容批量流程 |
| `tests/` | 模拟浏览器、真实子进程与隔离回归测试 |

账户管理不导入投递模块，投递模块不导入账户管理；runner 是装配边界。设计取舍和验收见 [解耦决策](references/account-decoupling.md)。

## 本地文件与发布

`accounts/`、`connections/`、`.account-runtime/`、`boss-data/`、个人画像与日志备份被 Git 忽略。账户数据里的报告、岗位缓存和历史均含个人信息，不能作为仓库示例发布。旧单账户入口的默认数据目录是项目下的 `boss-data/`；已有账户仍使用其配置目录。

旧单账户安装升级时，必须在运行前把 `BOSS_WORKDIR` 显式设为原历史目录，或使用支持 `--workdir` 的命令指定该目录。本版本移除了旧机器的绝对默认路径，不能依赖新默认目录继续旧任务，否则旧 delivered/uncertain 不会被读取。多账户混合配置的显式 workdir 保持兼容；历史不会被自动迁移。

提交前检查当前源文件；需要时提供本机配置，检查已知身份和画像值（报告只输出路径与类别）：

```powershell
python tools/check_publication.py
python tools/check_publication.py --private-config accounts/me/account.json --private-config accounts/me/profile.json
python tools/check_publication.py --history
```

发布要求针对新版本源文件：不提交本机账户、身份、画像或投递记录。既有 Git 历史按项目维护者的明确选择保留，普通 clone 会包含旧提交；本项目不宣称历史已脱敏。`--history` 用于可选审计，历史发现不阻止正常追加提交；`.gitignore` 不会删除已提交的旧内容。

可生成不包含 `.git` 和被忽略本机数据的源文件目录：

```powershell
python tools/check_publication.py --export .local-archive/public-source
```

导出目录必须不存在。自动检查不替代对发布内容的人工审查；导出不推送远程、不改写旧历史。

## 开发验证

已有 dev worktree 时在 dev 中开发，审查后再合并到安装目录；不要在活动投递中切换代码版本。避免多个编辑进程同时修改同一份代码。

```powershell
python -m unittest discover -s tests -v
```

测试使用合成身份和本地假桥接，不向真实网站发送消息。`tools/manage_skill.py` 仅用于独立安装副本的旧布局；共享目录直接用 Git 管理。`bridge_holder.py` 是需要父进程保活的环境可选工具，必须显式指定账户和连接。
