# BOSS 搜索筛选码 与 批量投递模式（v6.11 新增）

> 本文档记录 2026-10-12 实测确认的 **BOSS 搜索 URL 筛选码** 与 **整页批量投递** 方法。
> 这两项把投递命中率从 ~20% 提升到接近 100%，并把「一次一张卡」的交互轮次压缩到 1/15。

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

## 2. 批量投递模式（整页判读，替代逐卡行走）

### 为什么可以批量

- BOSS 搜索结果**单页 15 张卡全部常驻 DOM**（非虚拟列表，无分页，滚动不增长）。
- `cmd_open(jid)` / `cmd_deliver(jid)` 都用 `a[href*="job_detail/<jid>"]` 按 **jid 定位卡片**，
  与 walk 游标无关；`deliver_click` 只在 `/web/geek/jobs` 路径内原地操作。
- **结论**：只要卡片在**当前已加载的那一页**里，就能在同一次页面态内连续 reject/deliver。

### 三段式流程（把 15 轮压缩成 3 轮）

| 阶段 | 动作 | 命令 |
|---|---|---|
| ① 采集 | 多关键词 × `jobType=1902&scale=304,305,306` 轮扫，累积去重候选池 | `scripts/harvest_pool.py <keywords.json>` |
| ② 分诊 | 按来源关键词分组 → 短名单拉 JD 正文（只对存疑的拉） | `scripts/pipeline_batch.py jdc <jid...>` |
| ③ 执行 | 按分组 navigate → 页内批量 reject/deliver | `scripts/pipeline_batch.py go <decisions.json>` |

`decisions.json` 形如 `{"deliver": ["jid1","jid2"], "reject": ["jid3"]}`。

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
表现为 `panel_ok:False` 且打印出的 JD 正文是**上一张卡**的残留内容（陈旧面板）。
处理：重试一次；仍失败就放弃该卡，改用新关键词重新采一轮（不要反复硬怼同一个 jid）。

### 3.3 卡片表面信息不足以判届别

卡面 `degree` 只显示**最低学历**，JD 里可能写死更高要求（例：卡面"本科"、JD"硕士及以上"）。
`工` 常见两种实习口径：

| 措辞 | 含义 |
|---|---|
| `ByteIntern：面向2027届毕业生（2026.9–2027.8）` | 27届专属转正实习 → 非27届**不要投** |
| `日常实习：面向全体在校生，为期3个月以上` | 全体在校生 → 可投 |
| `27或者28年毕业，实习可转正，也有日常实习岗位` | 含28届 → 可投 |
| `面向 2028及之后毕业同学` | 28届可投 |

### 3.4 标题会骗人

实测样例：
- 智谱「**Coding实习生**」→ JD 实为 *AI Coding 数据集专家标注*（纯数据标注岗）。
- 腾讯云雀「多模态大模型 **caption** 实习生」→ 纯视频标注 + 要求影视功底。
- 腾讯「**Golang**」→ JD 写「目标同学：**26届**毕业生暑期实习」。
- 盒马「阿里巴巴**春招**实习生-服务端开发」→ JD 写「阿里巴巴**2024**应届春招」「不招23届」。
**凡薪资异常/标题含糊/公司名带"子公司/腾佳/云雀"字样，一律先读 JD 再决定。**

---

## 4. 快速上手（复制即用）

```bash
D=<skill>/scripts
PY=<python>

# 1) 采集（keywords.json 是关键词数组）
$PY $D/harvest_pool.py keywords.json "304,305,306" 1902

# 2) 只看候选池
$PY $D/pipeline_batch.py ls            # 全部
$PY $D/pipeline_batch.py ls "Agent 实习"  # 按来源关键词过滤

# 3) 抽查 JD（会按来源关键词自动分组导航）
$PY $D/pipeline_batch.py jdc <jid1> <jid2>

# 4) 批量执行
$PY $D/pipeline_batch.py go decisions.json
```
