# BOSS 搜索筛选码 与 批量投递模式（v6.11 新增）

> 本文档记录 2026-10-12 实测确认的 **BOSS 搜索 URL 筛选码** 与 **整页批量投递** 方法。
> 当前默认已改为逐岗审核即时投递，见 [SKILL.md](../SKILL.md)。本文保留筛选码与旧批量操作说明，仅在用户明确选择批量或对照测试时使用；历史命中率和交互轮数不代表当前性能。

---

## 1. 搜索 URL 的筛选参数（关键：传「筛选码」，不是文案）

搜索页 URL：`https://www.zhipin.com/web/geek/jobs?query=<kw>&city=<code>&<filters>`

**坑**：`scale` 传区间文案（`scale=1000-9999人`）会被 BOSS **静默忽略**，返回结果不筛选。
必须传**枚举码**，多选时用逗号连接。

### 求职类型 jobType

| 码 | 含义 |
|---|---|
| `1901` | 全职 |
| `1902` | **实习** ← 投实习岗必须带，否则结果被社招淹没 |
| `1903` | 兼职 |
| `0` / 省略 | 不限 |

### 公司规模 scale（★ 中大厂硬筛选，命中率关键）

| 码 | 含义 |
|---|---|
| `301` | 0-20人 |
| `302` | 20-99人 |
| `303` | 100-499人 |
| `304` | 500-999人 ← 独角兽/明星创业常见档位（如智谱、第四范式、九识） |
| `305` | 1000-9999人 ← 中厂主力（如得物、小红书、SHEIN） |
| `306` | 10000人以上 ← 大厂（字节/腾讯/阿里/百度/小米/海康/快手） |

**推荐组合**：`&scale=304,305,306` —— 单页 15 张卡基本全是中大厂，无需逐张读 JD 就能筛掉小微。

### 其它筛选码（同页 `.filter-select-dropdown li[ka]` 可枚举）

| 维度 | ka 前缀 | 已知码 |
|---|---|---|
| 薪资 | `sel-job-rec-salary-` | `402`…`407` |
| 经验 | `sel-job-rec-exp-` | `108`…`102`（108=在校/应届） |
| 学历 | `sel-job-rec-degree-` | `202`…`209` |
| 融资阶段 | `sel-job-rec-stage-` | `801`…`808` |

> 枚举方法（不用点开下拉）：
> ```js
> [...document.querySelectorAll('.filter-select-dropdown li[ka]')]
>   .map(li => li.getAttribute('ka'))
> ```
> 想拿到某个筛选的**规范 URL 写法**：用 JS `.click()` 勾选该 `li`，再读 `window.location.href`。

---

## 2. 批量投递模式（保留的兼容入口）

### 为什么可以批量

- 读取当前已加载在DOM中的卡片；数量与是否继续加载以实际页面为准，不固定假设15张或滚动不增长。
- `cmd_open(jid)` / `cmd_deliver(jid)` 都用 `a[href*="job_detail/<jid>"]` 按 **jid 定位卡片**，
  与 walk 游标无关；`deliver_click` 只在 `/web/geek/jobs` 路径内原地操作。
- **结论**：只要卡片在**当前已加载的那一页**里，就能在同一次页面态内连续 reject/deliver。

### 三段式流程（把 15 轮压缩成 3 轮）

| 阶段 | 动作 | 命令 |
|---|---|---|
| ① 采集 | 多关键词 × `jobType=1902&scale=304,305,306` 轮扫，累积去重候选池 | `scripts/harvest_pool.py <keywords.json>` |
| ② 分诊 | 按来源关键词分组 → 每个待投岗位都读完整JD，由智能体结合画像审核 | `scripts/pipeline_batch.py jdc <jid...>` |
| ③ 执行 | 按分组 navigate → 页内批量 reject/deliver | `scripts/pipeline_batch.py go <decisions.json>` |

`decisions.json` 形如 `{"deliver": ["jid1","jid2"], "reject": ["jid3"]}`。

默认专属话术还需增加 `"greetings":{"jid1":"该岗位专属话术"}` 并覆盖所有待投岗位；不提供时走平台默认招呼，仅用户明确选择时使用。专属消息确认送达后，当前代码通过浏览器后退恢复原列表，核验失败则停止；不再用重新打开搜索URL作为发送后的返回方式。旧jdc和默认go的分组导航仍会重新加载搜索页，因此采集到的jobId不保证还能在列表中找到。

### 必须去重

- **已投递去重**：`stream_progress.json.delivered[].jobId`（BOSS 也会把已投卡片从列表移除，但重排后可能再出现）。
- **已判否去重**：`walk_state.json.rejected_jids`。
- harvest 会自动过滤已投递项。

---

## 3. 已知坑（实测）

### 3.1 不要直连 `/wapi/zpgeek/search/joblist.json`

该接口在页面内用同步 XHR 调用时**前 2 次成功**，之后连续返回 `code:37`，并把整站跳转到
`/web/passport/zp/security.html?...code=37` 安全验证页（约 6 秒后自动放行并带回 `_security_check=1`）。
**不要为了拿 30 条/页或分页去刷这个接口**——有账号风控风险。用页面 DOM 就够。

### 3.2 同一关键词两次加载，卡片集**会变**

BOSS 搜索结果是动态轮换的。`pipeline go` 失败常见原因是 `clicked:False` —— 即该卡**已不在 DOM**。
当前代码在 `panel_ok:False` 时返回空JD，不使用上一张卡的残留正文；有限定位仍找不到时返回not_found，不代表岗位已下架。
处理：重试一次；仍失败就放弃该卡，改用新关键词重新采一轮（不要反复硬怼同一个 jid）。

### 3.3 学历与届别须结合完整 JD 和账户画像审核

卡面 `degree` 只显示**最低学历**，JD 里可能有更高学历要求或指定届别（例：卡面"本科"、JD"硕士及以上"）。必须读取完整 JD，再按用户最新明确指示及所选账户本轮画像快照判断；下面的措辞解释不是通用的可投/拒绝规则。

**账户 A、B（2026-10-16 更新）**：两个账户均忽略「硕士及以上」学历要求及「面向2027届」等届别限制，不以这些条件拒绝；其他投递条件仍须符合各自画像。只有明确排除在校生（如要求已毕业）才按此拒绝；招呼语仍如实说明本科、2028 届。

| 措辞 | 含义与审核方式 |
|---|---|
| `ByteIntern：面向2027届毕业生（2026.9–2027.8）` | JD 限定 27 届；A、B 均不以届别拒绝，继续核对各自画像中的其他条件 |
| `硕士及以上` | JD 设学历要求；A、B 均不以该要求拒绝，继续核对各自画像中的其他条件 |
| `日常实习：面向全体在校生，为期3个月以上` | 面向在校生；继续核对账户实习时长及其他条件 |
| `27或者28年毕业，实习可转正，也有日常实习岗位` | 包含 28 届；继续核对其他条件 |
| `面向 2028及之后毕业同学` | 包含 28 届；继续核对其他条件 |
| `要求已毕业，不接受在校生` | 明确排除在校生；A、B 的学历/届别放宽均不覆盖此要求 |

### 3.4 标题会骗人

实测样例：
- 智谱「**Coding实习生**」→ JD 实为 *AI Coding 数据集专家标注*（纯数据标注岗）。
- 腾讯云雀「多模态大模型 **caption** 实习生」→ 纯视频标注 + 要求影视功底。
- 腾讯「**Golang**」→ JD 写「目标同学：**26届**毕业生暑期实习」。
- 盒马「阿里巴巴**春招**实习生-服务端开发」→ JD 写「阿里巴巴**2024**应届春招」「不招23届」。
**凡薪资异常/标题含糊/公司名带"子公司/腾佳/云雀"字样，一律先读 JD 再决定。**

### 3.5 harvest 与 go 的 scale 必须一致（BOSS_SCALE）

`go` 会按 `_kw` 重新导航到「来源关键词页」，该 URL 的 `scale` 取自**环境变量**
`BOSS_SCALE`（默认 `304,305,306`，见 `pipeline_batch.py:30`），
**不是** harvest 写进 `pool.json` 的那个 scale。

所以 harvest 用 `303,304,305,306` 采、go 时忘了
`export BOSS_SCALE=303,304,305,306`，页面会把 303 卡过滤掉 →
定位不到卡片 → `not_found` / `panel_ok:false`，表现为「采到了却投不出去」。

`page_scan.py` 的scale取命令行第2参，未传才读 `BOSS_SCALE`；`harvest_pool.py` 的scale取第2参或默认值，不读取该环境变量。新 `immediate start` 与batch共用URL组装函数，读取 `BOSS_SCALE` / `BOSS_JOBTYPE`，默认分别为 `304,305,306` / `1902`；应按画像确定并保持本轮一致。
三者要指向同一档位。

另：`accounts.py:65-68 check_environment` 会拦下 `BOSS_CITY` 与画像
`city_code` 不一致的情况，因此**不能用环境变量临时换城市**——城市只能改画像
`city_code`（下一轮 `begin` 生效）。`BOSS_SCALE` 不在拦截名单内，可按轮次 export。

---

## 4. 快速上手（兼容批量入口）

```powershell
# A/Edge：accounts/a/account.json；B/Chrome：accounts/b/account.json，按用户选择。
$accountConfig = "<选定账户配置路径>"
$runId = "<该账户begin返回的run>"

# 1) 采集（keywords.json 是关键词数组）
python scripts/run_account.py --config $accountConfig --run $runId harvest keywords.json "304,305,306" 1902

# 2) 只看候选池
python scripts/run_account.py --config $accountConfig --run $runId batch ls
python scripts/run_account.py --config $accountConfig --run $runId batch ls "Agent 实习"

# 3) 每个待投岗位都读完整JD（会按来源关键词自动分组导航）
python scripts/run_account.py --config $accountConfig --run $runId batch jdc <jid1> <jid2>

# 4) 批量执行
python scripts/run_account.py --config $accountConfig --run $runId batch go decisions.json
```
