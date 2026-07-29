# 端到端网站爬取

`FullSiteCrawlCoordinator` 在系统内部完成完整调度，不需要调用方逐个运行
Agent：

```text
WebsiteDecisionAgent
        ↓
robots.txt policy
        ↓
CrawlPlanAgent / SiteAdapter
        ↓
CatalogAgent
        ↓
ProductAgent
        ↓
CrawlResultValidator
```

## 单命令运行

ICGoGo 全量分类任务：

```bash
python -m component_agent \
  --crawl-site https://www.icgoo.net/ \
  --delay 1.0 \
  --no-browser
```

端到端样本运行（只选择 sitemap 的第一个分类，但会抓完该分类的全部分页）：

```bash
python -m component_agent \
  --crawl-site https://www.icgoo.net/ \
  --max-categories 1 \
  --delay 1.0 \
  --no-browser
```

也可以使用独立入口：

```bash
python -m component_agent.crawl_site https://www.icgoo.net/ --max-categories 1
```

## ICGoGo 策略

- 自动读取 `robots.txt` 和声明的 sitemap。
- 从 sitemap 生成 `/catalog/<id>/` 分类任务。
- 使用 `?page=N` 遍历每个分类的全部型号页。
- 将表格中的型号、分类、编码和创建时间转换为统一 ProductSeed。
- ICGoGo robots 禁止 `/search/`、`/partno-detail?...` 和供应商详情路径，
  因此 ProductAgent 自动使用 `catalog_only` 模式，不请求这些路径。
- 所有请求都经过 `RobotsAwareFetchTool`；被禁止的 URL 会在发起网络请求前停止。

## 恢复与输出

默认运行状态写入 `component_agent/run_state/<site>/`：

- `crawl_plan.json`
- `checkpoints.json`
- `product_seeds.jsonl`
- `product_checkpoints.json`
- `product_details.jsonl`
- `tasks.jsonl`
- `issues.jsonl`
- `validation.json`
- `categories.json`
- `products_final.json`
- `crawl_summary.json`

重复运行相同命令会读取 checkpoint，跳过已完成分类和已标准化商品。

`component_agent/data/` 和 `component_agent/run_state/` 均被 Git 忽略，不应提交抓取数据。
