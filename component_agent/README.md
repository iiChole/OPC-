# 电子元器件多 Agent 抓取项目

项目正在从原有的多网站查询工具演进为按职责协作的多 Agent
全站商品抓取系统。旧的型号查询入口继续保留，新工作流目前已完成
`WebsiteDecisionAgent`、`CrawlPlanAgent`、`CatalogAgent`、`ProductAgent`
和通用验证/恢复组件；独立的 `ValidationAgent` 尚未实现。

## 目录结构

```text
component_agent/
├── agents/
│   ├── decision.py          # 网站类型判断，不执行爬取
│   ├── crawl_plan.py        # 有限探索并生成全量抓取计划
│   ├── catalog.py           # 全分类/全分页枚举，生成 ProductSeed
│   └── product.py           # 有界并行访问详情页并统一商品字段
├── catalog/
│   ├── models.py            # ProductSeed、checkpoint、handoff 数据契约
│   ├── parser.py            # JSON、嵌入 JSON、HTML 商品列表解析
│   ├── pagination.py        # page、offset、cursor、Next 状态机
│   └── checkpoint.py        # 原子断点与追加式 JSONL 日志
├── product/
│   ├── models.py            # 统一商品、问题和执行结果数据契约
│   ├── parser.py            # JSON、JSON-LD、嵌入状态和 HTML 参数解析
│   └── checkpoint.py        # 商品详情断点和追加式 JSONL 日志
├── planning/
│   ├── models.py            # CrawlPlan、分类、API、分页数据契约
│   └── page_analysis.py     # HTML、嵌入 JSON、Network API 结构分析
├── orchestration/
│   └── validation.py        # 数量/字段验证和一次完整工作流重试
├── adapters/                # 旧查询流程的网站适配器
├── tests/                   # 回归测试
├── agent.py                 # 旧型号查询流程，暂时保留
├── models.py                # 旧查询与浏览器公共模型
├── parser.py                # 旧查询页面解析器
├── storage.py               # 旧查询存储
└── tools.py                 # HTTP、Playwright 和 Network 工具
```

根目录中的 `decision_agent.py`、`crawl_plan_agent.py`、
`catalog_agent.py`、`product_agent.py` 和 `crawl_validation.py`
仅作为向后兼容入口。新代码应分别从
`component_agent.agents`、`component_agent.planning` 和
`component_agent.orchestration` 导入。

## CatalogAgent

CatalogAgent 可以直接接收 `CrawlPlan` 对象，也可以接收
CrawlPlanAgent 输出的 JSON 字典：

```python
from component_agent.agents import CatalogAgent, CrawlPlanAgent

plan = CrawlPlanAgent().run("https://example.com/")
result = CatalogAgent(
    run_state_dir="run_state",
    traversal_mode="auto",
).run(plan.to_dict())
```

计划探索阶段仍是有限探测；CatalogAgent 执行阶段会遍历所有已发现
分类和分页。自动调度规则如下：

- 根分类很多或设置 `prefer_parallel=True` 时使用 BFS。
- 其他情况默认使用 DFS，适合逐层深入的分类树。
- 也可通过 `traversal_mode="dfs"` 或 `"bfs"` 显式指定。
- 分页支持 page、offset、响应 cursor、Next 链接和 Playwright
  Next 按钮。
- ProductSeed 按 SKU、Product ID、详情 URL 哈希的顺序去重。

CatalogAgent 只生成内部 ProductSeed，不访问商品详情，也不发布
`products_final.json`。运行状态保存在：

| 文件 | 说明 |
|---|---|
| `run_state/checkpoints.json` | 分类队列、已完成分类和分页位置 |
| `run_state/product_seeds.jsonl` | CatalogAgent 到 ProductAgent 的交接数据 |
| `run_state/tasks.jsonl` | 分类、分页和 Agent handoff 事件 |
| `run_state/issues.jsonl` | 请求、解析、分页和数量异常 |

请求失败时返回 `replan_required` handoff，目标为可用的
`CrawlPlanAgent`。数量、分页或 API 数据异常时返回
`validation_required` handoff，目标为尚未实现的
`ValidationAgent`，因此 `available` 为 `false`。Agent 只返回
结构化 handoff，不直接调用其他 Agent。

## ProductAgent

ProductAgent 接收 CatalogAgent 生成的 `ProductSeed`，并按照 CrawlPlan
的 `execution_policy.detail_fetch` 访问全部详情页：

```python
from component_agent.agents import ProductAgent

detail_result = ProductAgent(
    run_state_dir="run_state",
    max_concurrency=4,
    request_interval_seconds=0.25,
).run(
    result.product_seeds,
    plan,
)
```

- 同时接受 `ProductSeed`/`CrawlPlan` 对象和序列化字典。
- 使用线程池并行抓取，实际并发取 Agent 配置与 CrawlPlan 配置中的较小值，
  并硬限制为最多 16。
- 使用全局请求启动间隔控制请求速率，默认并发为 4、间隔为 0.25 秒。
- 支持 `auto`、`requests` 和 `playwright` transport；ProductSeed 中的
  `extra.detail_request` 可覆盖单个商品的 URL、headers 和 transport。
- requests 遇到反爬或 JavaScript 页面时回退浏览器；若未安装 Playwright
  但系统存在 Chromium/Chrome，则使用受限的 headless `--dump-dom` 后备。
- 统一基础字段；规格参数写入 `attributes`，网站特有字段写入
  `extra.site_fields`，详情来源和目录来源分别保存在 `source_url` 与
  `catalog_source_url`。
- 字段缺失只记录 issue，不丢弃商品。请求、HTTP 或解析失败也会写入一条
  `fetch_status="failed"` 的回退商品，并保证 title 非空、`attributes` 和
  `extra` 为字典。
- worker 只负责抓取与解析，JSONL、checkpoint、tasks 和 issues 均由主线程
  串行写入，因此并行结果不会互相覆盖。

ProductAgent 运行状态保存在：

| 文件 | 说明 |
|---|---|
| `run_state/product_checkpoints.json` | 已成功和待重试的 ProductSeed 去重键 |
| `run_state/product_details.jsonl` | 追加式详情结果；同一商品以最后一条为最新状态 |
| `run_state/tasks.jsonl` | ProductAgent 启动、每个详情完成和结束事件 |
| `run_state/issues.jsonl` | 请求失败、解析失败和字段缺失记录 |

恢复运行时，成功商品会跳过，失败商品默认重试。一次失败后成功的商品会在
JSONL 中留下可审计历史，但读取结果时只返回该去重键的最新记录。


## 安装

```bash
python -m pip install -r component_agent/requirements.txt
python -m playwright install chromium
```

## 使用

```bash
python -m component_agent \
  --input '查询: STM32F103C8T6. 需要: 价格, 库存, 封装, 厂商'
```

或者：

```bash
python -m component_agent \
  --query STM32F103C8T6 \
  --fields price,stock,package,manufacturer \
  --sites ickey,szlcsc,ti
```

不传 `--fields` 时默认提取全部支持字段。常用参数：

- `--max-results 10`：每个站点最多进入详情阶段的商品数。
- `--no-browser`：禁用 Playwright 回退。
- `--output-dir PATH`：指定数据目录。
- `--delay 0.35`：请求间隔，降低站点压力。

## 执行流程

1. 解析查询词和字段；未指定字段则使用完整字段集合。
2. 从站点注册表选择供应商搜索入口。
3. requests 获取目录/搜索页，检查响应的页面类型。
4. 对 JSON API、Next.js SSR、JSON-LD、静态 HTML 使用对应 parser；JS 页面或未解决的 challenge 回退到 Playwright。
5. 按查询词相关度筛选目录商品，再访问每个详情 URL。
6. 详情数据优先，详情失败时保留目录数据。
7. 验证所需字段，把 HTTP、反爬、工具缺失、空结果和字段缺失写入问题文件。

## 数据格式

每个查询写入 `component_agent/data/<query>/`：

| 文件 | 格式 | 说明 |
|---|---|---|
| `catalogs.json` | JSON | 页面类型、传输方式、HTTP 状态与商品数 |
| `products.jsonl` | JSON Lines | 目录/搜索结果 |
| `product_details.jsonl` | JSON Lines | 成功解析的详情 |
| `products_final.json` | JSON | 合并后的结果、问题和诊断信息 |
| `issues.jsonl` | JSON Lines | 可重试性明确的问题报告 |

统一商品字段包括 `model`、`price`、`stock`、`package`、`manufacturer`、`sku`、`title`、`description`、`moq`、`attributes`、`datasheet_url`、`image_url`、来源 URL 和抓取时间。

价格和库存是时效数据。运行时应遵守各站点条款、robots 约束和合理请求频率。
