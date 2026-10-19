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
避免两个 Agent 同时编辑同一份代码。账户入口支持 A/Edge 与 B/Chrome 并行：每个执行进程固定配置、端点、session 和 run；同一账户及同一浏览器端点仍互斥。旧入口使用 `boss-main`，启用多账户后必须走账户入口。
回滚或切换版本应避开运行中的任务。

个人配置 `user_profile.json`、记录 `boss_applied.json`、Python 缓存及临时备份由 `.gitignore` 排除。
这些个人文件保留在本地；原始基线提交仍包含当时的配置，尚未改写历史。
原始回滚基线标签：`baseline-2026-10-13`。未配置远程仓库。

## 数据目录

本次仅统一技能代码与 Git，未迁移投递数据。
运行数据和 HTML 默认仍位于 `D:\Dev\code\my_github\boss-zhipin-assistant\boss-data`，也可通过 `BOSS_WORKDIR` 指定。
统一入口不会自动扩大 Agent 的文件写入或网络权限。

## 独立账户与并行运行

使用 `scripts/run_account.py --config <配置文件>` 显式选择账户。配置示例在 `examples/`，本机 A/Edge 和 B/Chrome 的配置、独立画像副本在 Git 忽略的 `accounts/a/`、`accounts/b/`。旧历史不自动迁移。

完整的扩展连接、身份绑定、begin/end、并行示例及故障恢复步骤见 [账户操作说明](references/accounts.md)。两个终端或智能体可以分别操作 A、B，各自独立 begin/end；同一账户不能同时运行两条操作命令。

运行状态位于 `.account-runtime/accounts/<账户>/`；浏览器端点锁位于 `.account-runtime/targets/`。共享注册表仅在检查和写入时短暂加锁。历史已投和 uncertain 继续留在各自数据目录，不因 end 或重新 begin 清空。升级前应等旧命令结束；如果仍有旧版活动记录，使用原账户和 run 执行 end 后再 begin，不删除锁或历史文件。

离线测试（模拟身份和本地假桥接，不发送消息）：

```powershell
python -m unittest discover -s tests -v
```

并发测试使用独立临时目录和真实 Python 子进程，覆盖双账户同时执行、同账户/同端点互斥、父进程崩溃、注册冲突、历史与报告隔离及旧版运行收尾。2026-10-16 经用户授权完成真实 A/Edge、B/Chrome 并行验证：各发送 1 条专属招呼并核验送达，发送命令执行区间重叠约 13.5 秒，记录归属独立且均无 uncertain。B 恢复原列表成功；A 送达后因原列表文档未保留触发停止，未重发。该结果验证一次双账户并行发送，不代表长时间连续投递的稳定性。

## 发送后的列表保留

专属话术在点击「立即沟通」之前就分离标签页：保留原搜索列表，从已审核卡片读取真实详情链接，在另一个标签页核对账户、岗位 ID 和 JD，再发起沟通与发送。已有聊天标签页确认无草稿后可复用。随后按 tabId 切回列表，核验原文档、节点、顺序与滚动位置；不刷新原列表、不依赖后退缓存。实现见 `scripts/delivery_tab.py` 和 `scripts/greeting.py`。

2026-10-19 A/Edge 实投 1 岗验证：新详情标签页跳转聊天时发现加载过渡被误判，修复等待逻辑并核实会话无本人消息后，在同一会话完成一次专属话术发送。原列表的文档、节点、顺序和滚动位置全部保留，随后成功读取下一岗；未发送第二岗。已送达与 uncertain 去重、账户身份和会话目标检查保持不变。此为一次 A 账户验证，尚未实测 B/Chrome 或长时间连续运行；聊天标签页保留供复用，不自动关闭。

## 后台加载岗位

岗位列表无需切到前台。共享滚动函数会向真实滚动容器及页面派发 scroll 事件，再核验新增岗位 ID、分页和加载状态；不激活标签页、不模拟焦点。每个账户沿用独立端点、session 和锁，可在后台分别加载。

超时或无增长返回加载停滞，扫描批次上限返回待继续；只有明确无更多岗位才报告当前搜索列表已结束。逐岗流程会先审核最后一批岗位。可通过账户入口 `scroll-check --keyword <关键词> --steps 3` 做只读验证，结果保存在所选账户的 `data/scroll_checks/`。2026-10-19 A/Edge、B/Chrome 真实并行验证分别15→99岗、15→98岗，均在第7页确认无更多；全程 hidden，前台未变、原文档保留，执行重叠15.648秒，没有发送投递。画像、投递历史及待审核状态哈希未变。

## 旧同步与恢复工具

`tools/manage_skill.py` 为原双副本布局保留。现在源和目标解析为同一真实目录，工具会拒绝运行，不再使用 `sync` 或 `restore`。
版本管理直接使用真实目录的 Git；恢复已跟踪文件前先保存当前未提交修改。
此前迁移产生的 `.restore-backups/` 和旧安装副本中的三个 `.bak-20261013` 文件已核对并清理。
