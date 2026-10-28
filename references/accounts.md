# 本地账户、浏览器连接与运行

账户表示一个稳定的 BOSS 登录身份及其历史；连接表示当前可使用的浏览器环境。二者分别配置，在 begin 时组合。同一账户可跨轮次更换连接，不复制或清空历史。

## 首次配置

将 `examples/account.json` 和 `examples/profile.json` 复制到 `accounts/<本地别名>/`。修改 account 字段并填写真实画像；不要在 examples 中填写个人资料。账户配置：

```json
{"account":"my-account","profile":"profile.json","workdir":"data","expected_identity":null}
```

将 `examples/connection-chrome.json` 或 `examples/connection-edge.json` 复制到 `connections/`。连接配置示例：

```json
{"browser":"chrome","bridge":{"host":"127.0.0.1","port":10086},"session":"boss"}
```

这里的端口是可修改的示例，不属于任何特定账户。浏览器扩展连接地址须与配置一致，例如 `ws://127.0.0.1:10086/ws`。并行使用多个环境时，每个环境使用独立端口和独立网站登录状态；同一浏览器品牌也可以。程序不创建或切换浏览器用户资料，不通过 session 隔离 Cookie。

账户配置中的路径相对于该配置文件；命令里的配置、连接、决策和话术路径相对于当前终端目录。克隆或导出后的个人数据目录应保持在 Git 忽略路径内。

## 管理账户

```powershell
$accountConfig = "accounts/me/account.json"
$connection = "connections/chrome.json"
python scripts/manage_accounts.py --config $accountConfig --connection $connection bridge-start
python scripts/manage_accounts.py --config $accountConfig --connection $connection bridge-status
python scripts/manage_accounts.py --config $accountConfig --connection $connection inspect
python scripts/manage_accounts.py --config $accountConfig --connection $connection --confirm <inspect返回的confirm> bind
```

inspect 打开推荐页，从页面读取浏览器类型和当前用户标识。智能体展示结果，请用户核对本人身份后再 bind；bind 再次读取并比对确认值。不读取 Cookie、密码和令牌，不以同名用户代替稳定身份。无法可靠识别时停止。

`bridge-start` 在空闲端口启动隐藏的独立 WebBridge daemon；仅对子进程设置独立 USERPROFILE，不修改系统环境。服务目录按端点区分。已有预期服务可复用，不停止端口上的其他程序。内置启动器目前只支持 Windows；其他系统可自行准备桥接服务。

账户在活动运行中禁止 inspect/bind/bridge-start；其他账户也不能通过这些命令接管该运行占用的端点。bridge-status 只读取服务状态，status 只读取本地运行状态。

## 投递运行

```powershell
python scripts/run_account.py --config $accountConfig --connection $connection begin
# 保存返回的 run；之后不需要重复传连接。
python scripts/run_account.py --config $accountConfig --run <run> immediate start "<关键词>"
python scripts/run_account.py --config $accountConfig --run <run> immediate next
# 智能体读取完整 JD，结合本轮画像写出决策文件。
python scripts/run_account.py --config $accountConfig --run <run> immediate decide <决策文件>
python scripts/run_account.py --config $accountConfig --run <run> report
python scripts/run_account.py --config $accountConfig --run <run> end
```

发送仍需要用户授权；配置和只读检查不授权投递。默认逐岗审核，不积攒一批后再回头发送。`immediate step <决策>` 合并 decide 和下一次 next；`immediate status` 查看待审状态。

begin 固定连接和画像快照。配置或画像的修改从下一轮生效；运行中显式传入不同连接会被拒绝。旧 review_id 不得用于新 run。账户内串行，不同账户在不同端点可并行；注册表只在读写时短暂加锁。

专属话术发送前保留原列表，从已审卡片读取真实详情链接，在独立标签页复核账户、岗位与 JD，再进入精确岗位会话发送。核验本人消息文本与送达状态后按 tabId 返回列表，确认文档、节点、顺序与滚动位置仍保留才继续。uncertain 或列表返回失败会停止，不能重发或通过重开 run 清空记录。

## 更换或复用连接

同一账户：先 end，确认另一浏览器已登录相同 BOSS 身份，再用同一个账户配置和另一个 `--connection` begin。身份与 workdir 的绑定保留，浏览器类型、端口、session 只属于本轮运行。

不同账户顺序复用：先结束原运行，由用户在浏览器切换到目标身份，再用目标账户配置及该连接 begin。实际身份不符会拒绝。账户历史不能互相复制；同一身份不能注册两套去重历史。

账户别名和历史目录仍是长期归属，不能静默换身份或历史目录。这保护的是数据归属，不是浏览器品牌。迁移真实身份或合并历史需另外核对数据，不通过删除注册表或标记文件绕过。

## 兼容和故障处理

若升级的是旧单账户模式，先显式设置 `BOSS_WORKDIR` 为原数据目录；支持 `--workdir` 的命令也可显式指定。旧机器的绝对默认路径已移除，新默认是项目内 boss-data。未指定原目录就继续会漏读旧已投与 uncertain，因此不要把新空目录当成旧任务历史。迁移到新账户配置时，非空旧目录仍需先明确身份归属，不能自动接管或复制给多个账户。

旧混合账户配置仍可用；其中 browser / bridge / session 只作为 begin 默认连接，可用 `--connection` 覆盖。旧 registered.json 的浏览器字段不再构成永久限制，下一次该账户注册时仅保存身份与历史归属。无需修改现有本机配置或历史。

升级前先结束活动运行，避免新旧代码并用。旧全局 active.json 由原账户及 run 执行 end；不复制或删除运行控制文件。原 `run_account.py` 中的 inspect/bind/status/bridge-* 仍委托同一账户管理实现，兼容已有调用。

`status` 可在执行期间查询。父进程异常后活着的子命令会阻止账户重入或 end；其他独立账户可继续。锁文件是否占用由操作系统决定，不删除文件解锁。配置改变后仍可用账户与原 run end。

本地控制文件在 `.account-runtime/`：registered.json 保存身份与历史归属，accounts/<别名>/ 保存 active.json、inflight.json、command.lock，targets/ 保存端点锁。begin 到 end 的活动租约阻止空闲命令间隙被另一个账户接管。

账户 data 中保存 stream_progress.json（含 delivered/uncertain）、immediate_state.json、审核和命令日志、岗位缓存、runs/ 快照与 HTML 报告。end 不清空它们。画像中的 HR 活跃度筛选按每轮快照读取，详见 SKILL.md。

其他命令：live、scan、harvest、batch、env、verify、verify-full、diag、cleanup、scroll-check。账户模式禁止另传 --workdir。cleanup 只在用户明确要求时使用，不能自动消除 uncertain。后台加载验证使用 `scroll-check --keyword <关键词> --steps 3`，只读证据保存在当前 data/scroll_checks/。

## 依赖边界与验证

账户管理入口不导入投递模块；投递业务仅消费 execution_context 的运行契约以及浏览器适配器，不读取注册表。runner 负责装配。完整结构见 README，取舍和验证见 [解耦决策](account-decoupling.md)。

`python -m unittest discover -s tests -v` 运行离线回归，包括真实子进程、假桥接、多账户隔离、连接切换与旧配置兼容。模拟浏览器结果不代表真实网页结构和扩展连接已经验证。
