# Multi-Agent

多项目工作区。

## 项目

### ickey — 云汉芯城 (ICkey) 电子元器件爬虫

爬取 [ickey.cn](https://www.ickey.cn/) 电子元器件数据。

**流程:** category → list (API) → detail → merge

```bash
cd ickey
.venv/bin/python3 main.py --step all
```

### szlcs — 立创商城 (SZLCSC) 电子元器件爬虫

爬取 [szlcsc.com](https://www.szlcsc.com/) 电子元器件数据。

**流程:** category → list (SSR) → detail → merge

```bash
cd szlcs
.venv/bin/python3 main.py --step category   # 采集分类
.venv/bin/python3 main.py --step list       # 采集商品列表
.venv/bin/python3 main.py --step detail     # 采集商品详情
.venv/bin/python3 main.py --step merge      # 合并数据
.venv/bin/python3 main.py --step all        # 全流程
```

**数据格式:**

| 文件 | 格式 | 说明 |
|------|------|------|
| `data/categories.json` | JSON | 分类树（56 大类 / 2742 子类） |
| `data/products.jsonl` | JSON Lines | 商品列表（流式追加 + 批量 Flush） |
| `data/product_details.jsonl` | JSON Lines | 商品详情 |
| `data/products_final.json` | JSON | 合并后的完整数据 |

**特点:**

- **反爬虫绕过:** 自动生成 RC4 Cookie 绕过 `list.szlcsc.com` 的 JS 挑战
- **JSONL 流式写入:** 每积累 50 条或间隔 10 秒自动落盘，避免内存丢失
- **信号安全:** 注册 SIGINT/SIGTERM 处理器，异常退出时强制 Flush 缓冲区
- **断点续爬:** 按 `catalog_id` / SKU 记录 checkpoint，中断后自动恢复
- **阿里云 WAF 回退:** `item.szlcsc.com` 受 WAF 保护时，自动使用列表页已有属性数据

### ti — TI.com 电子元器件爬虫

爬取 [ti.com](https://www.ti.com) 电子元器件数据。

**流程:** families → list → detail → merge

```bash
cd ti
python main.py --step families   # 发现产品家族
python main.py --step list       # 采集产品列表
python main.py --step detail     # 采集产品详情
python main.py --step merge      # 合并数据
python main.py --step all        # 全流程
```

**数据格式:**

| 文件 | 格式 | 说明 |
|------|------|------|
| `data/families.json` | JSON | 产品家族（17 大类 + 子分类） |
| `data/products.jsonl` | JSON Lines | 产品列表（SKU、价格、库存） |
| `data/product_details.jsonl` | JSON Lines | 产品详情（attributes、datasheet） |
| `data/products_final.json` | JSON | 合并后的完整数据 |

**特点:**

- **JSON-LD 提取:** 从 category 页面的 JSON-LD ItemList 自动发现子分类，无需硬编码分类树
- **断点续爬:** 列表和详情步骤按 family_id / SKU 记录 checkpoint，中断后自动恢复
- **反爬延迟:** 请求间隔 + 重试退避，避免触发限流
- **详情优先合并:** merge 阶段详情数据覆盖列表数据，保留列表独有的分类字段
