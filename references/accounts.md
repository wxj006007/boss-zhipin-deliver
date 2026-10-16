# Edge / Chrome 独立账户：跨账户并行运行

每个账户使用独立配置、画像副本和数据目录。一轮运行中配置与画像固定，另一轮才读取修改后的画像。A/Edge 与 B/Chrome 可以同时 begin 并执行命令；每个账户内部仍按读取、审核、发送、核验的顺序串行执行。并行要求浏览器端点、登录身份和数据目录均独立，换 session 不能将同一端点变成两个并行目标。

HR 活跃度也随账户画像保存：`"hr_activity_filter": {"mode": "today", "unknown": "allow"}` 表示今天活跃可投，信息未知继续按岗位匹配判断。卡片离线不直接剔除，详情页补读后在点击沟通前复核；不符合时单列 `activity_skipped`，不计投递失败且以后可再次评估。未配置或 `mode: "off"` 保持原行为。配置更新从下一轮 begin 生效。

## 一次性准备

本机配置位于 `accounts/a/account.json`（Edge）和 `accounts/b/account.json`（Chrome），画像各自在同目录的 `profile.json`。两个画像只在初始化时从原画像复制，之后没有同步关系。个人文件和 `.account-runtime/` 都已排除在 Git 之外。新安装参考 `examples/account-a.json`、`examples/account-b.json` 和空白画像 `examples/profile.json`；根据实际用户填写，不发送示例内容。

在技能目录运行：

```powershell
python scripts/run_account.py --config accounts/a/account.json bridge-start
python scripts/run_account.py --config accounts/b/account.json bridge-start
```

两个浏览器各自安装并启用官方 Kimi 扩展，登录自己的 BOSS 账户。扩展设置中的连接地址固定为：

| 配置 | 浏览器 | 扩展连接地址 | session |
|---|---|---|---|
| A | Edge | `ws://127.0.0.1:10087/ws` | `boss-a` |
| B | Chrome | `ws://127.0.0.1:10086/ws` | `boss-b` |

`bridge-start` 在空闲端口启动独立前台 daemon，但以隐藏子进程常驻，运行文件保存在 `.account-runtime/bridges/<账户>/`；仅对子进程设置独立 USERPROFILE，不修改系统环境变量。不占用或覆盖默认 daemon 的 PID 文件。已有 WebBridge 服务可直接复用；占用端口的其他服务不会被停止。重启电脑或服务退出后重新执行。没有安装开机自启任务。

`bridge-status` 返回 `extension_connected: true` 后，读取账户身份：

```powershell
python scripts/run_account.py --config accounts/a/account.json inspect
```

这会在该账户 session 中打开推荐页，读取浏览器类型和页面已有的当前用户标识，不读取 Cookie、密码或令牌。请用户核对当前浏览器登录的确为账户 A；智能体展示读取结果，获得对应关系确认后才绑定：

```powershell
python scripts/run_account.py --config accounts/a/account.json --confirm <inspect返回的完整confirm值> bind
```

B 也按同样流程独立绑定。`bind` 会重新读取身份，与确认值一致才更新本地配置。身份不明、错误浏览器或页面结构变化导致无法可靠识别时停止，不回退为只检查“已登录”。同名用户不作为可靠身份；优先用当前用户的稳定 ID，其次才是账户区域中的姓名与脱敏手机号组合。

## 开始、操作与结束

默认逐岗流程（A/Edge和B/Chrome完全相同，仅配置与画像不同；以下旧 `live` 和批量命令仅作兼容入口）：

```powershell
# 先按用户选择确定配置，不默认B。
# A/Edge：accounts/a/account.json；B/Chrome：accounts/b/account.json
$accountConfig = "<用户选定的账户配置路径>"
python scripts/run_account.py --config $accountConfig begin
python scripts/run_account.py --config $accountConfig --run <run-id> immediate start "<符合画像的关键词>"
python scripts/run_account.py --config $accountConfig --run <run-id> immediate next
# 智能体审核返回的完整 JD，写入含 jobId、review_id、action、reason 的决策 JSON。
# approve 还需要 greeting；reject 不发送。决策文件路径相对于终端目录。
python scripts/run_account.py --config $accountConfig --run <run-id> immediate decide <决策文件>
# 继续 next → 审核 → decide；当前范围耗尽且没有 pending 后可 start 另一个关键词。
python scripts/run_account.py --config $accountConfig --run <run-id> report
python scripts/run_account.py --config $accountConfig --run <run-id> end
```

`immediate status` 可查看本轮待审项和处理状态。旧 run 的审核标识不能用于新 run；历史已投及 uncertain 仍共用 `stream_progress.json`。`immediate_events.jsonl` 保存审核理由与阶段耗时，`command_timings.jsonl` 保存新旧命令耗时，便于对照；时间不足以单独证明轮换原因，需结合定位结果。

专属话术在同一账户/session的聊天标签页发送，原列表标签页保持打开；送达后切回并核验 `list_return.preserved=true` 且未停止才继续。返回时核验同一文档、原节点、剩余岗位顺序和滚动位置；失败保留已送达结果并停止，不重新搜索或重复发送。聊天标签页按岗位ID精确选择会话，遇已有草稿不切换、不覆盖。下面保留A账户的旧live示例，B同样可用自己的配置执行该兼容入口：

```powershell
python scripts/run_account.py --config accounts/a/account.json begin
# 将返回的 run 值记为 <run-id>，本轮每条命令都使用它。
python scripts/run_account.py --config accounts/a/account.json --run <run-id> live walk
python scripts/run_account.py --config accounts/a/account.json --run <run-id> live open <jobId>
# 智能体读完整 JD，结合 accounts/a/profile.json 中本轮画像判断并生成话术。
python scripts/run_account.py --config accounts/a/account.json --run <run-id> live deliver <jobId> --greeting-file <话术文件>
python scripts/run_account.py --config accounts/a/account.json --run <run-id> live walk --next
python scripts/run_account.py --config accounts/a/account.json --run <run-id> report
python scripts/run_account.py --config accounts/a/account.json --run <run-id> end
```

A、B 可以各自 `begin`，分别获得运行标识，无需等待另一账户结束。不要把 A 的标识交给 B。账号配置中的相对路径相对于配置文件；命令中的决策/话术文件路径相对于当前终端目录。将 A、B 的决策和话术文件分别保存在各自目录，不共享同名临时文件。

统一入口也支持：`scan` → page_scan、`harvest` → harvest_pool、`batch` → pipeline_batch、`env`、`verify`、`diag`、`cleanup`。后三种报告/核验/诊断命令不另传 `--mode`，所有命令均不允许另传 `--workdir`。批量例：

```powershell
python scripts/run_account.py --config accounts/a/account.json --run <run-id> scan Python "304,305,306" 1902 3 jd
python scripts/run_account.py --config accounts/a/account.json --run <run-id> batch go <决策文件> --current-page
```

是否实际投递仍取决于用户授权；配置、连接和只读验证不自动授权发送。

## 故障与数据归属

- `status` 查看所选账户的运行标识，`legacy_active` 显示尚未结束的旧版全局运行；执行命令期间也可查询。`bridge-status` 只读服务状态。A 活动时 B 可独立 `begin`、`inspect`、`bind`、`bridge-start`，但不能指向 A 的端点、身份或历史目录。同一账户活动期间仍禁止 inspect/bind/bridge-start，以免导航或变更正在使用的浏览器。
- 命令异常退出后保留该账户的运行占用与投递状态。同一配置、同一 run 可继续诊断；仍活着的子命令会阻止该账户重入或 `end`，其他账户可继续。子命令等待在途 PID 写入成功后才启动实际操作；父进程提前退出时不会绕过该检查。不按名称杀进程，不自动清理公共 PID 文件。
- 已确认命令结束后可用原账户别名及 run 执行 `end`，再重新 `begin`。配置变化后也能结束原运行；新运行才读取新画像。`uncertain` 仍需核实消息结果，结束运行不清除它。
- 首次 `begin` 成功后，会启用多账户入口限制，旧投递命令不能再隐式使用 `boss-main`。旧单账户用法在此前保持兼容。
- 新数据目录必须为空且不能与其他账户共用或嵌套。目录写入账户绑定标记，别名不能悄悄切换身份、浏览器端点或历史目录。迁移旧历史应另行明确归属，不能将旧目录复制给两个账户。
- `cleanup` 只影响所选账户，仍属于显式清理操作，不能用于自动恢复 uncertain；默认不执行清理。
- 运行日志输出账户、浏览器、run；画像快照存于数据目录 `runs/`，投递记录带账户与运行标记，HTML 报告显示来源。原有报告统计仍为该账户累计记录。

## 两个终端并行操作

两边终端都在技能目录运行；也可由两个智能体分别负责一个账户。以下示例仅开始、查询和结束运行，不包含发送命令；begin 会读取并核验真实浏览器身份。配置须已完成身份绑定。

终端 A：

```powershell
$a = (python scripts/run_account.py --config accounts/a/account.json begin | ConvertFrom-Json).run
python scripts/run_account.py --config accounts/a/account.json status
# 经授权后在此使用 --run $a 执行 A 的逐岗流程。
# A 本轮工作结束后才执行：
python scripts/run_account.py --config accounts/a/account.json --run $a end
```

终端 B 可在 A 尚未结束时运行：

```powershell
$b = (python scripts/run_account.py --config accounts/b/account.json begin | ConvertFrom-Json).run
python scripts/run_account.py --config accounts/b/account.json status
# 经授权后在此使用 --run $b 执行 B 的逐岗流程。
# B 本轮工作结束后才执行：
python scripts/run_account.py --config accounts/b/account.json --run $b end
```

每条操作命令持有账户锁与 `host + port` 端点锁，执行完毕释放；从 begin 到 end 的活动记录和注册绑定防止其他任务接管该账户。`registered.json` 的检查与更新使用短时间全局锁，不在浏览器操作和子进程等待期间持有。每个子进程单独设置画像和路由环境，不能在同一个 Python 进程里通过反复修改环境变量切换账户。

控制文件位于 `.account-runtime/accounts/a/`、`.account-runtime/accounts/b/`，分别保存 `active.json`、`inflight.json`、`command.lock`。共享端点锁位于 `.account-runtime/targets/`。锁文件可长期存在，是否占用由操作系统锁决定，不要通过删除文件解锁。

## 从串行版升级与验证

升级前等待旧命令退出，不同时运行新旧版本。如果 `status` 返回 `legacy_active`，用其中的原账户和 run 执行 `end`。仍存活的旧命令会阻止结束；不能将旧 `active.json` 复制到两个账户目录。旧运行结束后再分别 begin，账户配置、注册绑定、画像、数据目录和投递历史均沿用原文件。

结束或重开任一账户不清空历史已投、uncertain、审核状态或另一账户的运行记录；uncertain 仍需按原流程核对。更换 run 后旧审批不能用于发送。

执行 `python -m unittest discover -s tests -v` 运行离线回归。`tests/test_account_concurrency.py` 使用临时工作区、两个本地假桥接及真实 Python 子进程，验证同时执行、端点/账户互斥、注册冲突、父进程崩溃后的隔离恢复、身份与数据路由、历史去重和旧版收尾。离线测试不读取本机账户配置、不调用真实发送。

2026-10-16 已另行按用户授权完成一次真实并行验证：A/Edge、B/Chrome 各发送 1 条专属招呼，均返回消息 ID、`delivered_ok=true`、`verify=true`，记录归属正确且 uncertain 均为空。两条发送命令执行区间重叠约 13.5 秒。B 的 `list_return.preserved=true`；A 送达后列表文档变化，返回 `preserved=false` 并停止，已送达记录保留且未重发。两轮均已 report/end 收尾；A 的停止状态保留，后续继续须按 SKILL.md 的核对恢复流程处理。这是一次双账户并行发送实测，不代表长时间连续投递的稳定性。

随后双subagent独立验收也各成功发送1条，发送命令重叠6.04秒，A再次出现原列表恢复失败。排查取得浏览器明确原因 `broadcastchannel-message`：发送引发的广播使原列表被移出后退缓存。现改为保留列表页、同session聊天标签页发送后切回，避免依赖缓存。修复后A真实发送1条专属招呼，`verify=true`，`method=retained_tab`，`same_document/same_nodes/order_preserved/preserved/scroll_restored` 均为 true；随后可读取下一岗，重放旧审核被拦截，没有重发。另修复了后退历史包含前进条目时总长度不增加而导致来源误判的问题，历史兼容路径优先核对 Navigation API 的条目标识与位置。
