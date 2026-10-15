# 搜索筛选参数与兼容批量入口

当前默认投递流程是 `immediate start → next → 智能体审核 → decide`，见 [SKILL.md](../SKILL.md)。本文件说明当前代码怎样组装搜索条件，以及用户明确选择批量或对照测试时仍可使用的入口。历史批次的命中率、交互轮数和卡片数量不作为当前性能承诺。

## 1. 搜索条件如何进入 URL

搜索页使用：

```text
https://www.zhipin.com/web/geek/jobs?query=<编码后的关键词>&city=<城市码>&jobType=<类型码>&scale=<规模码>
```

脚本用 `urllib.parse.quote` 编码关键词，浏览器加载页面后从卡片读取基本信息；完整 JD 要点击对应卡片后读取。默认优先取卡片 Vue 数据中的 jobId，DOM 兜底会从 href 提取 jobId 并保存 href；执行仍按 jobId 定位当前列表中的卡片，不直接打开保存的岗位详情链接。

### 参数来源与优先级

| 入口 | 公司规模 scale | 岗位类型 jobType | 城市 city |
|---|---|---|---|
| `immediate start` | BOSS_SCALE，默认 304,305,306 | BOSS_JOBTYPE，默认 1902 | BOSS_CITY → 本轮画像 city_code → 100010000 |
| `batch jdc` / 默认 `batch go` | 同上 | 同上 | 同上 |
| `scan` | 第 2 个位置参数 → BOSS_SCALE → 304,305,306 | 第 3 个位置参数 → BOSS_JOBTYPE → 1902 | 同上 |
| `harvest` | 第 2 个位置参数 → 304,305,306 | 第 3 个位置参数 → 1902 | 同上 |

immediate 与 batch 共用 `pipeline_batch.kw_url()`；harvest 的 scale/jobType 不读取 BOSS_SCALE/BOSS_JOBTYPE。scan 额外支持第 4 个参数（滚动次数，默认取 BOSS_SCAN_SCROLLS 或 3）、第 5 个参数 `jd`（读取 JD），以及可选的 BOSS_SCAN_PAGE（追加 page 查询参数）。具体页面是否支持分页或继续加载，以实际观察为准。

参数属于搜索范围，不是语义审核结论。当前默认值偏向实习、500 人及以上公司；应按用户要求调整，不能把这些默认值当作所有人的求职条件。

### 当前使用的筛选码

使用枚举码，不使用“1000-9999人”这类展示文案作为 scale 参数。以下为项目沿用的页面筛选映射；平台变更时应核对实际筛选控件与 URL。

| jobType | 含义 |
|---|---|
| 1901 | 全职 |
| 1902 | 实习 |
| 1903 | 兼职 |

| scale | 公司人数档位 |
|---|---|
| 301 | 0–20 人 |
| 302 | 20–99 人 |
| 303 | 100–499 人 |
| 304 | 500–999 人 |
| 305 | 1000–9999 人 |
| 306 | 10000 人以上 |

多选用逗号连接，例如 `304,305,306`。公司规模不能直接证明公司性质、岗位适合度或是否外包；这些仍需结合 JD 与用户画像审核。薪资、经验、学历、融资等其他筛选，先检查当前页面控件与选中后的 URL，不照抄旧版本的未验证码值。

### 账户模式下的环境变量

`accounts.check_environment()` 会拒绝与账户配置冲突的 BOSS_WORKDIR/BOSS_PROFILE，以及与画像 city_code 不一致的 BOSS_CITY。城市变更应修改所选账户画像并在下一轮 begin 生效，不用环境变量覆盖为另一个城市。

BOSS_SCALE/BOSS_JOBTYPE 不属于画像快照，入口也没有锁定它们；每次子命令都从调用环境继承。应在一轮开始前确定，并在该轮保持一致。

```powershell
# 仅示例：确认符合当前用户要求后，在调用脚本前设置。
$env:BOSS_SCALE = "304,305,306"
$env:BOSS_JOBTYPE = "1902"
```

旧批量流程尤其需要保持采集、读 JD、投递的条件一致。默认 go 的 URL 不读取 pool.json 中保存的 scale/job_type：如果 harvest 采了 303 档而 go 只搜索 304–306 档，部分旧卡片会因筛选不同而定位不到。即使参数一致，重新加载也可能导致列表轮换。

## 2. 默认逐岗流程中的列表恢复

immediate start 仅在进入或切换关键词时导航；next 从当前真实列表选取一个未处理岗位，读取 JD 后等待智能体决策。合适即发送，不把多个已审岗位积攒起来再重新搜索。

专属话术确认送达后，使用 `list_return.py` 校验来源并后退一次，尝试恢复浏览器缓存中的原文档。核验原岗位（允许刚投岗位消失）、相对顺序和滚动位置后继续；失败时保留已送达结果并停止，不自动重新打开搜索 URL。不能用“URL 相同”推定“列表没变”。

列表长度、是否懒加载和可用的缓存均以实际页面为准。next 当前最多检查三轮列表并滚动两次；view_exhausted 只说明本次有限范围无未处理项，不保证所有岗位都已读完或整站供给耗尽。

## 3. 兼容批量入口（仅用户明确选择时使用）

所有示例在技能目录执行，通过账户入口包装，不直接运行底层浏览器脚本。使用前完成 [账户准备](accounts.md) 并 begin，以下变量沿用同一配置与 run：

```powershell
$accountConfig = "accounts/b/account.json"
$runId = "替换为begin返回的run"
```

### 多关键词候选池

```powershell
# keywords.json 是关键词字符串数组；规模/岗位类型显式传给 harvest。
python scripts/run_account.py --config $accountConfig --run $runId harvest accounts/b/data/keywords.json "304,305,306" 1902
python scripts/run_account.py --config $accountConfig --run $runId batch ls
# 将下列示例 ID 替换为实际待审岗位；每个待投岗位都需要完整 JD。
python scripts/run_account.py --config $accountConfig --run $runId batch jdc jid1 jid2
# 智能体完成审核及话术后才执行：
python scripts/run_account.py --config $accountConfig --run $runId batch go accounts/b/data/decisions.json
```

harvest 逐个导航关键词页，读取当时已加载的卡片，不主动滚动；按 jobId 并入 pool.json，记录来源 _kw，过滤已投递项，但不会更新已在池中的同 ID 元数据。它不会在采集阶段过滤所有 uncertain，执行阶段仍由共享进度阻止重复发送。

jdc 按 _kw 分组重新导航，再逐个读取 JD。当前 jd_cache.json 保存本次调用结果，会覆盖此前文件；不能把它当成完整历史缓存，也不能把 panel_ok=false/空正文/错误项作为已读 JD。

默认 go 再按 _kw 分组导航并执行。因此采集、读 JD、投递可能处于不同批搜索结果，手里的 jobId 不保证当前卡片还在。不要反复追找同一个消失岗位。

### 同页扫描与执行

```powershell
python scripts/run_account.py --config $accountConfig --run $runId scan "Python 实习" "304,305,306" 1902 3 jd
# 审核读取到的完整 JD；不切换页面，再执行：
python scripts/run_account.py --config $accountConfig --run $runId batch go accounts/b/data/decisions.json --current-page
```

scan 在一次导航后采集并按参数滚动；jd 模式复用有效的 jd_*.json 缓存（排除 jd_cache.json），其余逐条读取并落盘。缓存并非页面当前存在或 JD 未变的证明，投递前仍需核验。

--current-page 检查当前域名、路径、关键词与决策来源 _kw 一致，不执行组间导航；它没有核验所有筛选参数或整页岗位集合，也不能锁住页面状态。同页扫描仍属于先读一批再执行，与默认 immediate 的单条待审机制不同。

### 批量决策与发送方式

```json
{
  "deliver": ["jid1"],
  "reject": ["jid2"],
  "greetings": {
    "jid1": "智能体依据该岗位JD和真实画像生成的专属话术"
  }
}
```

提供 greetings 时须覆盖每个待投岗位；缺少映射会在浏览器操作前报错。不提供 greetings 时走平台默认招呼路径，仅在用户明确选择这种方式时使用。专属话术路径与 immediate 共用消息核验、浏览器后退及原列表恢复；恢复失败也会停止批量执行。

reject 写入 walk_state.json.rejected_jids；它与 immediate_state.json.handled 是不同的记录机制，不要假定两个入口自动共享所有拒绝结果。已投和 uncertain 等共享进度由执行器检查；批量的 run_result.json 是本次执行结果，不是历史累计报告。

## 4. 读取失败与审核边界

- panel_ok=false 时，当前 cmd_open 返回空 detail/title，不把上一岗位残留正文作为目标 JD。not_found 表示有限定位后当前列表中未找到，不代表岗位已下架。
- 不能用职位标题、搜索关键词或卡片学历替代完整 JD。届别、学历、实习安排和岗位职责都要与当前用户匹配；“日常实习”本身不等于所有人都符合。
- 历史样例仅能说明需要读正文，不能推导某个公司或职业的固定通过/拒绝规则。
- 不通过直连岗位搜索接口刷分页来替代当前浏览器流程。出现验证、上限或未知页面状态时停止并核对，不根据历史“几秒后恢复”等经验自动继续。
- 诊断无浮层不能证明消息未发送。uncertain 先核对目标会话；不得清空去重记录来提高“成功率”。

## 5. 收尾与比较口径

```powershell
python scripts/run_account.py --config $accountConfig --run $runId report
python scripts/run_account.py --config $accountConfig --run $runId end
```

投递汇总.html 读取所选账户 workdir 的累计 stream_progress.json，本轮结果应按 run 单独核对。性能比较应区分读取成功率、卡片缺失、审核耗时、发送及返回耗时、独立岗位覆盖数和确认送达数量，不将一次小样本或历史描述当作固定提速倍数。各子命令耗时见 command_timings.jsonl，逐岗阶段记录见 immediate_events.jsonl。
