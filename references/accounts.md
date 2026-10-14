# Edge / Chrome 独立账户：第一版串行运行

每个账户使用独立配置、画像副本和数据目录。一轮运行中配置与画像固定，另一轮才读取修改后的画像。第一版只有一个投递任务能处于开始状态；两个浏览器及桥接服务可以同时打开。

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

A 结束后，再用 B 的配置 `begin`，获得新的运行标识。不要把 A 的标识交给 B。账号配置中的相对路径相对于配置文件；命令中的决策/话术文件路径相对于当前终端目录。

统一入口也支持：`scan` → page_scan、`harvest` → harvest_pool、`batch` → pipeline_batch、`env`、`verify`、`diag`、`cleanup`。后三种报告/核验/诊断命令不另传 `--mode`，所有命令均不允许另传 `--workdir`。批量例：

```powershell
python scripts/run_account.py --config accounts/a/account.json --run <run-id> scan Python "304,305,306" 1902 3 jd
python scripts/run_account.py --config accounts/a/account.json --run <run-id> batch go <决策文件> --current-page
```

是否实际投递仍取决于用户授权；配置、连接和只读验证不自动授权发送。

## 故障与数据归属

- `status` 查看活动账户和运行标识。已有活动任务时，另一账户的 `begin`、`inspect`、`bind`、`bridge-start` 均被拒绝；`bridge-status` 只读服务状态。
- 命令异常退出后保留运行占用与投递状态。同一配置、同一 run 可继续诊断；仍活着的子命令会阻止重入或 `end`。不按名称杀进程，不自动清理公共 PID 文件。
- 已确认命令结束后可用原账户别名及 run 执行 `end`，再重新 `begin`。配置变化后也能结束原运行；新运行才读取新画像。`uncertain` 仍需核实消息结果，结束运行不清除它。
- 首次 `begin` 成功后，会启用多账户入口限制，旧投递命令不能再隐式使用 `boss-main`。旧单账户用法在此前保持兼容。
- 新数据目录必须为空且不能与其他账户共用或嵌套。目录写入账户绑定标记，别名不能悄悄切换身份、浏览器端点或历史目录。迁移旧历史应另行明确归属，不能将旧目录复制给两个账户。
- `cleanup` 只影响所选账户，仍属于显式清理操作，不能用于自动恢复 uncertain；默认不执行清理。
- 运行日志输出账户、浏览器、run；画像快照存于数据目录 `runs/`，投递记录带账户与运行标记，HTML 报告显示来源。原有报告统计仍为该账户累计记录。

## 后续并行

账户配置、独立端点及数据结构无需另做一套。后续并行需要将当前技能级串行占用改为按浏览器目标占用，并完成双任务同时运行和故障恢复测试；本版本不宣称支持并行。
