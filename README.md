# Newspaper Phrase Locator

报刊档案引文核对服务：把编目的文章块顺序和 OCR 词/字框建成 SQLite 索引，按连续英文词组定位到原图上的每一处位置。一句话即使跨块、跨版面，也能返回它在各版上的字框；命中不会跨文章。

## 启动

```bash
docker compose up --build
```

打开 **http://localhost:8000/docs**（Swagger UI）。服务启动时若数据库为空，会自动装入 `/app/samples/demo.json` 本地样例。

- SQLite 文件：`/data/archive.sqlite3`，保存在 Compose volume `archive-data`
- 健康检查：`GET /health`
- 重建演示数据：`docker compose down -v && docker compose up --build`

也可本地运行：

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest
```

## Swagger 演示

### 1. 跨版检索

在 Swagger 执行：

```text
GET /api/search/phrase?q=the lighthouse keeper climbed
```

返回 A-100 的一处命中。`words` 中 `keeper` 在 `page-001` 的 block 2，`climbed` 在 `page-002` 的 block 1；每个词都带 `page_id`、`block_no`、`bbox`。样例版面图可从 `image_url` 打开（例如 <http://localhost:8000/scans/page-001.svg>）。再试：

```text
GET /api/search/phrase?q=BULLETIN the lighthouse keeper climbed
```

这个短语同时跨 `page-001` 的两个块和版面边界。把 `article_id=A-100` 传入可只看某篇文章。

文章边界测试：

```text
GET /api/search/phrase?q=dawn%20only
```

A-100 以 `dawn` 结束，A-200 以 `only` 开始，虽然二者同在 `page-002` 的 OCR 中，但不会命中。

### 2. 单版更正

在 Swagger 的 `PUT /api/pages/page-002/ocr` 中，请求体已预填
`samples/page-002-corrected.json` 的内容（也可直接复制该文件），执行：

- `page-002` OCR 被替换
- 只重建引用该版面的 A-100、A-200 索引
- 响应中的 `affected_articles` 为 `["A-100", "A-200"]`

然后：

| 查询 | 更正前 | 更正后 |
| --- | --- | --- |
| `the lighthouse keeper climbed` | 1 处，跨页 | 0 |
| `the lighthouse keeper walked` | 0 | 1 处，跨页 |
| `only in the archive edition` | 1 处 | 0 |
| `only in the archive supplement` | 0 | 1 处 |

### 3. 缺块时整版拒绝

把 `samples/page-002-missing-block.json` 作为
`PUT /api/pages/page-002/ocr` 的请求体。它只有 block 1，缺少编目引用的 block 2。响应为 **409**：

```json
{
  "detail": {
    "code": "missing_catalog_blocks",
    "message": "OCR retransmission rejected: ...",
    "page_id": "page-002",
    "missing_blocks": [2],
    "affected_articles": ["A-100", "A-200"],
    "unchanged": true
  }
}
```

整个版面的 OCR 替换和索引重建都不会提交；更正前的查询结果照常可查。

### 4. 版面核查：逐版找出漏编正文

旧报上线前，档案员要逐版找出“OCR 已识别却没有任何文章引用”的有词块，并把页眉、广告等非正文块逐块标记。演示使用独立版面 `page-006`（不影响上面的检索样例）：

1. `PUT /api/pages/page-006/ocr`，请求体用 `samples/page-006-review.json`。
   该版有 4 个有词块：块 1 页眉、块 2 广告、块 3 漏编的正文、块 4 已编目正文。
2. `PUT /api/catalog/articles/A-600`，请求体用 `samples/page-006-article.json`，
   它引用块 4。
3. `GET /api/pages/page-006/review` 取得核查清单。每个块返回：
   - 原文 `text`、块字框 `bbox` 和逐词 `words[].bbox`；
   - `review_status`：`cataloged`（已有编目引用）、`marked_non_body`（已标非正文）、
     `pending_review`（待核查）；
   - 乐观令牌 `review_version` 和变更来源 `last_change`。
   此时块 4 为 `cataloged`，块 1–3 为 `pending_review`，整版 `review_status=pending_review`。
4. 把页眉和广告标为非正文（`expected_version` 取自清单，例如均为 `1`）：
   - `PUT /api/pages/page-006/review/blocks/1`，body `{"expected_version": 1, "reason": "running header"}`
   - `PUT /api/pages/page-006/review/blocks/2`，body `{"expected_version": 1, "reason": "advertisement"}`
5. 块 3 是漏编正文：先不要标记，可观察整版仍为 `pending_review`；演示中再对块 3
   提交标记后，`GET .../review` 的整版状态变为 `complete`——所有有词块都有编目引用或有效标记。
6. 需要撤销标记时：`DELETE /api/pages/page-006/review/blocks/2?expected_version=1`，
   块 2 回到 `pending_review`。

#### 重传 OCR 后自动重查

- 用 `samples/page-006-rerun-identical.json` 重传：块号、词序、原文和字框全部不变，
  响应 `review_marks_carried` 列出沿用的标记，`review_version` 不变，旧令牌仍可提交。
- 用 `samples/page-006-rerun-changed.json` 重传：块 1 字框变化、块 3 原文变化，
  它们的标记失效、令牌 +1 并回到 `pending_review`（`review_marks_reset`）；
  块 2 内容不变，标记沿用（`review_marks_carried`）。
- 用旧令牌提交变化过的块返回 **409 `review_version_stale`**，提示重新拉取清单；
  其它块的变动不影响本块令牌。块被删除后即使原样恢复，令牌也已多次递增，旧提交同样被拒。
- 块一旦被文章引用（`PUT /api/catalog/articles/...`），其标记在同一事务中撤销，
  响应 `revoked_review_marks` 给出被撤销的块和新令牌；后来移出编目时令牌再次递增，
  必须重查，旧标记不会复活。

## API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `PUT` | `/api/pages/{page_id}/ocr` | 替换单版 OCR，原子重建涉及该版的文章索引并协调核查标记 |
| `GET` | `/api/pages/{page_id}` | 查看版面 revision、块与词数 |
| `GET` | `/api/pages/{page_id}/review` | 版面核查清单：有词块原文、字框、引用/核查状态与版本令牌 |
| `PUT` | `/api/pages/{page_id}/review/blocks/{block_no}` | 把一个未引用有词块标为非正文（带 `expected_version`） |
| `DELETE` | `/api/pages/{page_id}/review/blocks/{block_no}?expected_version=...` | 撤销非正文标记，块回到待核查 |
| `PUT` | `/api/catalog/articles/{article_id}` | 新建/替换文章的块引用顺序；引用块会撤销其核查标记 |
| `GET` | `/api/articles/{article_id}` | 查看文章块引用、索引状态与词数 |
| `GET` | `/api/search/phrase?q=...` | 忽略大小写、连续词组的跨块/跨版检索 |
| `GET` | `/api/search?q=...` | 同上，未写入 Swagger 的兼容别名 |
| `GET` | `/health` | 健康检查与行数统计（含 `review_marks`） |

### OCR 输入

```json
{
  "image_url": "/scans/page-002.svg",
  "width": 1600,
  "height": 2200,
  "blocks": [
    {
      "block_no": 1,
      "words": [
        {"text": "walked", "bbox": [100, 140, 220, 175]},
        {"text": "the", "bbox": [230, 140, 295, 175]}
      ]
    }
  ]
}
```

也接受不嵌套块的 OCR 导出：

```json
{
  "words": [
    {"block_no": 1, "text": "walked", "bbox": [100, 140, 220, 175]},
    {"block_no": 1, "text": "the", "bbox": [230, 140, 295, 175]}
  ]
}
```

`bbox` 输入支持：

- `[x0, y0, x1, y1]`
- `{"x0": ..., "y0": ..., "x1": ..., "y1": ...}`
- `{"x": ..., "y": ..., "width": ..., "height": ...}`
- `{"left": ..., "top": ..., "right": ..., "bottom": ...}`

输出统一为像素坐标对象 `{"x0": ..., "y0": ..., "x1": ..., "y1": ...}`。

### 编目输入

`blocks` 的顺序就是文章正文的连续顺序；`page_id + block_no` 指向 OCR 块，不要求物理块号连续：

```json
{
  "title": "The Lighthouse Keeper",
  "blocks": [
    {"page_id": "page-001", "block_no": 1},
    {"page_id": "page-001", "block_no": 2},
    {"page_id": "page-002", "block_no": 1}
  ]
}
```

- OCR 已到位的文章立即建索引。
- 新文章可以先编目、后到 OCR；此时 `status` 为 `pending_ocr`，`indexed_words` 为 0，不会以残缺块序列参与检索。
- 替换已有文章时若引用的 OCR 块缺失，返回 409，旧编目和旧索引保持不变。

### 查询语义

1. 以空白切分查询串和 OCR 词，做 Unicode NFKC + `casefold`，仅忽略大小写。
2. 在**同一文章**内按编目顺序把块展平，块内按 OCR 词序排列。
3. 只匹配连续词元；可以跨块、跨版面，但文章边界不可跨越。
4. 每处命中单独返回，包含文章编号、标题和每个词的位置。

## 一致性与事务设计

- SQLite 开启 WAL 和外键。
- 单版 OCR 重传使用一个 `BEGIN IMMEDIATE` 事务：
  1. 读取该版被哪些文章引用；
  2. 若请求缺少其中任何块，立即回滚整版；
  3. 替换该版 OCR 数据；
  4. 逐块比较旧/新指纹（块号、词序、原文、逐词字框）：完全相同的块沿用核查标记与
     令牌；有差异或新增的块令牌递增并清除标记；删除的块保留历史并换发新令牌；
  5. 按编目顺序重建且仅重建这些文章的整篇索引（跨版位置一并更新）；
  6. 一次性提交。失败时 OCR、索引、核查状态全部不变。
- 编目文章替换同样在单事务内完成：引用数 0→1 的块令牌递增、标记撤销；1→0（移出
  编目）的块令牌递增并回到待核查；文章索引随后重建，任一步失败整体回滚。
- 标记提交（`PUT`/`DELETE .../review/...`）在写事务内重新解析目标块：块已删除或无词
  返回 404；块已被文章引用返回 409 `block_now_cataloged`；`expected_version` 与当前
  `review_version` 不一致返回 409 `review_version_stale`。令牌按块单调递增、永不复用，
  因此“变更后恢复原样”仍会拒绝旧提交；其它块的变化不影响本块令牌。
- 查询使用单个 deferred read transaction，候选命中、词框和文章标题来自同一个快照。并发时只能看到提交前完整结果或提交后完整结果。
- 未引用该版面的文章索引行不会删除或重写。

## 目录

```text
app/
  main.py          FastAPI 路由、启动装载样例
  repository.py    SQLite schema、事务、重建、检索与版面核查
  schemas.py       请求/响应模型与 Swagger 示例
samples/
  demo.json                    本地演示语料
  page-002-corrected.json      单版更正请求体
  page-002-missing-block.json  缺块拒绝请求体
  page-006-review.json         版面核查演示版面（页眉/广告/漏编正文/已编目块）
  page-006-article.json        核查演示的编目文章
  page-006-rerun-identical.json 重跑 OCR 内容不变，标记沿用
  page-006-rerun-changed.json   重跑 OCR 内容变化，标记失效、令牌递增
static/scans/
  page-001.svg, page-002.svg, page-006.svg  由样例字框生成的演示版面图
tests/
  test_api.py      检索、更正、回滚、输入形式
  test_atomic.py   写事务暂停时的读快照与核查状态原子性
  test_review.py   核查清单、标记、重传沿用/失效、乐观令牌与编目撤销
```
