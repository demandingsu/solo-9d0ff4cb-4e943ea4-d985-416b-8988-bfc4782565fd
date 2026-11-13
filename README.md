# 连锁便利店电价套餐比较 API + 电表校时整理 API

能源采购员按门店未来 12 个月的峰谷用电预测，比较公开电价套餐的年度成本，辅助续签决策。
门店能源资料员可用校时整理接口，从供电商明细恢复可用于核对峰谷时段的真实采样时间。

- **Web 框架**：Python 3.11 + FastAPI（`app/main.py`）
- **抓取与计费/校时核心**：仅依赖 Python 标准库（`urllib` + `html.parser` + `decimal`），离线可测
- **本地报价站**：随仓库提供的纯静态站点（`quote-site/`），nginx 容器托管，无需外网
- **本地电表明细站**：含时钟漂移、跨页重复与缺号的演示数据（`meter-site/`）
- **部署**：Docker Compose 一键启动 API + 报价站 + 明细站

## 目录结构

```
.
├── app/
│   ├── main.py           # FastAPI 路由与比较/校时编排
│   ├── schemas.py        # 请求/响应 Pydantic 模型
│   ├── scraper.py        # 报价站分页列表 + 套餐详情抓取解析
│   ├── meter_scraper.py  # 电表明细站分页抓取解析
│   ├── calibration.py    # 去重/冲突检测/锚点校时/分段核验
│   ├── fetcher.py        # http/https/file 统一抓取层与错误归一化
│   ├── html_dom.py       # 标准库实现的轻量 HTML DOM
│   └── billing.py        # 峰谷逐档计费、资格校验、年度汇总
├── quote-site/        # 本地报价站（静态 HTML，虚构演示数据）
├── meter-site/        # 本地电表明细站（静态 HTML，虚构演示数据）
│   ├── index.html         # 明细第 1 页（序号 1-8，设备时钟漂移）
│   ├── index-2.html       # 明细第 2 页（序号 7-14，7/8 与第 1 页跨页重复）
│   ├── index-3.html       # 明细第 3 页（序号 15-20，缺 17 号）
│   └── nginx.conf
├── tests/             # 标准库 unittest 离线测试（21 个用例）
├── demo/request.json            # 套餐比较示例请求
├── demo/calibrate-request.json  # 校时整理示例请求
├── Dockerfile
└── docker-compose.yml
```

## 快速开始（Docker Compose，推荐）

```bash
docker compose up --build
```

- 报价站：http://localhost:8080/index.html
- 电表明细站：http://localhost:8081/index.html
- API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

发起比较请求（容器内通过 `http://quote-site/index.html` 访问报价站）：

```bash
curl -X POST http://localhost:8000/api/v1/compare-plans \
  -H "Content-Type: application/json" \
  --data @demo/request.json
```

发起校时整理请求（容器内通过 `http://meter-site/index.html` 访问明细站）：

```bash
curl -X POST http://localhost:8000/api/v1/calibrate-readings \
  -H "Content-Type: application/json" \
  --data @demo/calibrate-request.json
```

## 无 Docker 的离线运行

核心抓取/计费/校时逻辑仅用标准库，可直接运行测试：

```bash
python3 -I -m unittest discover -s tests -t . -v
```

手动启动报价站、明细站与 API（需自行安装 `requirements.txt`）：

```bash
python3 -m http.server 8080 --directory quote-site
python3 -m http.server 8081 --directory meter-site
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 请求中将 list_url 改为 http://127.0.0.1:8080/index.html
# first_page_url 改为 http://127.0.0.1:8081/index.html
# 也支持 file:// 绝对路径，完全离线：
#   "list_url": "file:///workspace/quote-site/index.html"
#   "first_page_url": "file:///workspace/meter-site/index.html"
```

也可直接运行演示脚本（自动启动本地站点并调用 API）：

```bash
demo/run_demo.sh              # 套餐比较演示
demo/run_calibration_demo.sh  # 电表校时整理演示
```

## 请求格式

`POST /api/v1/compare-plans`

| 字段 | 说明 |
| --- | --- |
| `list_url` | 报价站套餐列表页 URL，支持 `http(s)://` 与 `file://` |
| `region` | 门店地区，需与套餐详情页地区标签精确匹配（如 `上海`） |
| `monthly_usage` | 恰好 12 条记录，每条含 `month`(1-12)、`peak_kwh`、`valley_kwh`（非负） |
| `timeout_seconds` | 可选，单次抓取超时 1-30 秒，默认 8 |

## 计费与筛选规则

1. **地区过滤**：套餐适用地区不包含请求地区 → 排除（`region_not_applicable`）。
2. **费率完整性**：缺峰段或谷段费率表、费率档位无法解析、月基本费缺失 → 排除
   （`missing_rates` / `invalid_tier_structure` / `missing_base_fee`）。
3. **档位覆盖校验**：任一月峰或谷用电量超过该费率表已公布的最高档位上限
   → 排除（`usage_exceeds_published_tiers`，响应中注明月份与段别）。无上限档（止为空）可承接任意电量。
4. **逐档计费**：每月峰、谷电量分别沿各自费率表按 `[下限, 上限)` 区间逐档计费，
   金额用 `Decimal` 保留两位小数（四舍五入），再加固定月基本费得月合计。
5. **年度汇总与排序**：12 个月月合计求和为年度成本，按从低到高排序返回。

## 抓取失败处理

- 列表页（含分页）或套餐详情抓取失败（连接错误、超时、HTTP 4xx/5xx、结构无法解析）时，
  在响应 `failures` 中列出失败地址、阶段（`list`/`detail`）、原因与 HTTP 状态码。
- 只要存在任何抓取失败，`conclusion.verification_status` 标记为 `pending_verification`（待核实）；
  全部成功且存在有效套餐时为 `verified`。无有效套餐时同样为 `pending_verification`。
- 分页跟随 `a[rel=next]`，并设有 50 页安全上限与去重，避免异常站点造成无限抓取。

## 演示数据覆盖的场景

| 套餐 | 地区 | 演示中的处理结果 |
| --- | --- | --- |
| P001 安能 峰谷A | 上海/江苏/浙江/华东 | 有效，参与排序 |
| P002 安能 峰谷B | 上海/华东 | 有效，参与排序 |
| P003 沪电 阶梯标准版 | 上海 | 有效，示例数据下年度最低价 |
| P004 通商 华北套餐 | 北京/天津/华北 | 地区不匹配，排除 |
| P005 安能 A+ | 上海/江苏/华东 | 缺谷段费率，排除 |
| P006 沪电 封顶套餐 | 上海 | 第 1 月峰用电即超 150 kWh 封顶档，排除 |
| P007 安能 C | 上海/华东 | 详情页 404，进入 `failures`，最低价标为待核实 |

示例请求下的复核年度成本（元）：**P003 6899.40 < P001 7247.40 < P002 7842.00**。

## 响应结构（摘要）

```json
{
  "conclusion": {
    "lowest_plan_id": "P003",
    "lowest_plan_name": "沪电零售 · 峰谷阶梯标准版",
    "lowest_annual_total_cny": 6899.4,
    "verification_status": "pending_verification",
    "note": "存在抓取失败的列表页或套餐详情，最低价结论待核实"
  },
  "ranked_plans": [ { "rank": 1, "annual_total_cny": 6899.4,
                      "monthly_bills": [ { "peak_charges": [...], "valley_charges": [...] } ] } ],
  "excluded_plans": [ { "plan_id": "P006", "reason": "usage_exceeds_published_tiers", "detail": "..." } ],
  "failures": [ { "url": ".../p007.html", "stage": "detail", "reason": "HTTP 404", "status": 404 } ]
}
```

每个参与排序的套餐均带 `detail_url`（详情来源）与 `list_url`（发现该套餐的列表页），
月度账单逐档列出 charged_kwh 与 charge_cny，便于采购员复核。

## 电表校时整理 API

`POST /api/v1/calibrate-readings`

资料员怀疑电表时钟走偏时，用可信 UTC 时间锚点把供电商明细里的设备时间
还原为可核对峰谷时段的真实采样时间。

### 请求格式

| 字段 | 说明 |
| --- | --- |
| `first_page_url` | 供电商明细首页 URL，支持 `http(s)://` 与 `file://` |
| `anchors` | 至少 2 个校时锚点，每条含 `seq`（采样序号，≥1）与 `trusted_time_utc`（可信 UTC 时间，ISO 8601 须带时区），序号互不相同 |
| `timeout_seconds` | 可选，单次抓取超时 1-30 秒，默认 8 |

### 校时与核验规则

1. **抓取**：从首页跟随 `a[rel=next]` 翻页（50 页上限 + 去重），采集每行的
   序号、带偏移的设备时间、电量及来源分页地址。
2. **去重**：同序号且内容一致的记录只计一次（跨页重复）；同序号内容不一致
   视为记录冲突。
3. **锚点偏差**：锚点处偏差 = 可信 UTC 时间 − 该序号设备时间。
4. **线性插值**：相邻锚点之间按序号对偏差线性插值，
   修正时间 = 设备时间 + 插值偏差。
5. **整段待核定**（段 = 相邻两锚点之间的序号区间），以下任一情况触发，
   段内记录**不会**混入 `trusted_records`，并在 `pending_segments` 中列出
   原因（`code`/`detail`）与来源（`sources`）：
   - `anchor_missing`：锚点序号的记录缺失、冲突或无法解析；
   - `sequence_gap`：段内缺号；
   - `record_conflict` / `record_unparseable`：段内记录冲突或无法解析；
   - `non_monotonic_corrected_time`：修正时间未随序号严格递增；
   - `anchor_coverage_missing`：序号超出首末锚点覆盖范围，无法插值。
6. **抓取失败**：任一分页抓取失败仍返回已采集记录与 `failures`
   （失败地址、阶段、原因、HTTP 状态码），但整份结果
   `verification_status = pending_verification`；全部核验通过且无待核定段
   时为 `verified`。

### 响应结构（摘要）

```json
{
  "verification_status": "pending_verification",
  "note": "1 个序号段待核定，待核定记录未混入可信记录",
  "crawl": { "visited_page_urls": ["..."], "rows_collected": 21,
             "unique_records": 19, "duplicates_skipped": 2, "failure_count": 0 },
  "anchors": [ { "seq": 1, "trusted_time_utc": "2026-10-01T00:00:00Z",
                 "device_time_utc": "2026-10-01T00:05:00Z",
                 "offset_seconds": -300.0, "status": "applied", "source_url": "..." } ],
  "trusted_records": [ { "seq": 2, "device_time_utc": "2026-10-01T00:20:20Z",
                         "corrected_time_utc": "2026-10-01T00:15:00Z",
                         "offset_seconds": -320.0, "energy_kwh": 3.4, "source_url": "..." } ],
  "pending_segments": [ { "start_seq": 12, "end_seq": 20,
                          "reasons": [ { "code": "sequence_gap", "detail": "段内缺号：17",
                                         "sources": ["..."] } ],
                          "records": [ { "seq": 13, "device_time_utc": "...",
                                         "device_time_raw": "...", "energy_kwh": 4.0,
                                         "source_url": "..." } ] } ],
  "failures": []
}
```

### 演示数据（meter-site）说明

- 电表 M-2026-1001，15 分钟采样，真实时刻为 2026-10-01T00:00:00Z 起整点刻；
- 设备时钟初始快 300 秒，且每个序号再漂移 20 秒（线性漂移）；
- 第 2 页与第 1 页跨页重复序号 7、8（内容一致，去重后只计一次）；
- 第 3 页缺序号 17 → 段 (12, 20] 整段待核定（`sequence_gap`）；
- 示例锚点（`demo/calibrate-request.json`）：序号 1 / 12 / 20，
  段 [1, 12] 核验通过，修正时间精确回到 15 分钟整点。
