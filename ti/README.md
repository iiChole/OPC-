# TI.com 电子元器件爬虫

从 [TI.com](https://www.ti.com) 爬取电子元器件产品数据，涵盖产品家族发现、列表采集、详情采集、数据合并的完整流水线。

## 项目结构

```
ti/
├── main.py                  # 主入口，编排全流程
├── merge.py                 # 数据合并模块
├── parsers/                 # 页面解析器
├── spiders/                 # 爬虫
│   ├── family_spider.py     # Step 1: 产品家族发现
│   ├── list_spider.py       # Step 2: 产品列表采集
│   └── detail_spider.py     # Step 3: 产品详情采集
├── storage/                 # 数据存储（JSONL / JSON）
├── utils/                   # 工具模块
│   ├── session.py           # HTTP Session 管理（重试、反爬）
│   ├── headers.py           # 请求头配置
│   └── logger.py            # 日志
└── data/                    # 数据输出目录
```

## 数据采集流程

```
families → list → detail → merge
```

| 步骤 | 说明 | 输入 | 输出 |
|------|------|------|------|
| `families` | 从分类导航页发现所有产品家族（destinationId） | TI.com 侧边栏分类 | `data/families.json` |
| `list` | 逐家族采集产品列表（SKU、价格、库存等） | `data/families.json` | `data/products.jsonl` |
| `detail` | 逐产品采集详细参数（attributes、datasheet 等） | `data/products.jsonl` | `data/product_details.jsonl` |
| `merge` | 合并列表 + 详情数据，输出最终结果 | 以上两个 JSONL | `data/products_final.json` |

## 安装依赖

```bash
pip install requests
```

## 用法

### 全流程采集

```bash
python main.py --step all
```

### 分步执行

```bash
python main.py --step families     # Step 1: 发现产品家族
python main.py --step list         # Step 2: 采集产品列表
python main.py --step detail       # Step 3: 采集产品详情
python main.py --step merge        # Step 4: 合并数据
```

### 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--step` | `all` | 执行步骤：`all` / `families` / `list` / `detail` / `merge` |
| `--retries` | `3` | 请求失败重试次数 |
| `--timeout` | `60` | 请求超时秒数 |

### 断点续爬

列表和详情步骤支持断点续爬。中断后重新运行相同命令，自动跳过已处理的产品。

```
data/checkpoint_list.txt    # 列表爬取进度
data/checkpoint_detail.txt  # 详情爬取进度
```

## 输出数据

最终输出 `data/products_final.json`，每个产品包含：

- **基础信息**：`sku`、`title`、`description`、`status`
- **分类信息**：`category`、`subcategory`、`family_id`、`family_name`
- **技术参数**：`attributes`（键值对字典）
- **库存与价格**：`rating`（库存评级）
- **文档**：`datasheet_pdf`、`datasheet_html`、`fbd_urls`（功能框图）
- **功能安全**：`functional_safety`
- **可订购型号**：`opn_list`
