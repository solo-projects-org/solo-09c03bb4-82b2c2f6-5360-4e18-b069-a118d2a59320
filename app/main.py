from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Path as ApiPath, Query
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .repository import (
    DomainError,
    clear_block_review,
    get_article_info,
    get_page_info,
    get_page_review,
    health_counts,
    initialize_database,
    mark_block_non_body,
    search_phrase,
    seed_demo_if_empty,
    upsert_catalog_article,
    upsert_page_ocr,
)
from .schemas import (
    ArticleInfo,
    BlockReviewInput,
    BlockReviewMutationResponse,
    CatalogArticleInput,
    HealthResponse,
    OCRPageInput,
    OCRUpdateResponse,
    PageInfo,
    PageReviewSummary,
    PhraseSearchResponse,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SAMPLE_PATH = PROJECT_ROOT / "samples" / "demo.json"
SCANS_DIR = PROJECT_ROOT / "static" / "scans"

DESCRIPTION = """
按 OCR 分词检索报刊文章中的连续英文词组，并返回命中处每个词的版面与字框。
Phrase search over OCR tokens, with per-word page and image-box locations.

### 数据语义

* 编目先给出文章的有序块引用 `page_id + block_no`；OCR 按版面提供块内的词和字框。
* 查询词组必须是 **连续 OCR 词元**，忽略大小写；可跨块、可跨版面，但不会跨文章。
* 每处命中返回 `article_id`，以及词组中每个词的 `page_id`、`block_no` 和 `bbox`。
* `bbox` 输出为图像像素坐标 `x0, y0, x1, y1`；输入也接受 `[x0,y0,x1,y1]` 或 `{x,y,width,height}`。

### 本地样例（启动后自动装入空数据库）

1. `GET /api/search/phrase?q=the lighthouse keeper climbed`
   返回 A-100；`words` 先落在 `page-001`，再跨到 `page-002`。
2. `PUT /api/pages/page-002/ocr`
   Swagger 已预填 `samples/page-002-corrected.json` 的更正内容；执行后：
   * 旧查询 `the lighthouse keeper climbed` 无结果
   * 新查询 `the lighthouse keeper walked` 命中，仍跨两个版面
   * `only in the archive edition` 变为无结果，`only in the archive supplement` 命中
3. 只提交 `page-002` 的块 1（缺少编目引用的块 2）时，整个更正返回 409，
   数据库与更正前索引都不变，旧查询仍可执行。

单版 OCR 重传会在一个 SQLite `BEGIN IMMEDIATE` 事务中替换该版 OCR，并只重建
引用该版面的文章索引。搜索使用同一读快照，因此并发查询只会看到更正前或更正后的
完整结果，不会看到半重建状态。

### 版面核查（漏编正文）

* `GET /api/pages/{page_id}/review` 按版列出所有**有词块**：原文、逐词字框与块
  字框、核查状态（`cataloged` / `marked_non_body` / `pending_review`）和乐观令牌
  `review_version`。
* 档案员把页眉、广告等非正文块用 `PUT .../review/blocks/{block_no}` 标记；当该版
  每个有词块都有编目引用或有效标记时，`review_status` 为 `complete`。
* 同一张扫描图重跑 OCR 时，仅当块号、词序、原文和字框全部不变才沿用标记
  （响应中的 `review_marks_carried`）；任一变化都会使标记失效并提升版本号
  （`review_marks_reset`），该块回到待核查。
* 块一旦被文章引用，其标记在同一事务内撤销；后来移出编目也会提升版本号，必须重查。
* 提交标记时若目标块曾变更、删除或被引用——即使内容恢复原样——令牌已失效，返回
  **409** 并提示重新拉取清单；其它块的变动不影响本块提交。
"""


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def create_app(
    database_path: str | os.PathLike[str] | None = None,
    sample_path: str | os.PathLike[str] | None = None,
    seed_demo: bool | None = None,
) -> FastAPI:
    db_path = str(
        database_path
        or os.getenv("DATABASE_PATH", "data/archive.sqlite3")
    )
    demo_file = Path(
        sample_path
        or os.getenv("DEMO_DATA_PATH", str(DEFAULT_SAMPLE_PATH))
    )
    should_seed = _env_bool("SEED_DEMO", True) if seed_demo is None else seed_demo

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        initialize_database(db_path)
        if should_seed:
            seed_demo_if_empty(db_path, demo_file)
        yield

    app = FastAPI(
        title="Newspaper Phrase Locator",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        openapi_tags=[
            {"name": "pages", "description": "OCR page ingestion and inspection"},
            {"name": "catalog", "description": "Article block order from cataloging"},
            {
                "name": "review",
                "description": "Layout review: uncataloged OCR word-block verification",
            },
            {"name": "search", "description": "Case-insensitive OCR phrase search"},
            {"name": "ops", "description": "Health and documentation"},
        ],
    )
    if SCANS_DIR.is_dir():
        app.mount("/scans", StaticFiles(directory=SCANS_DIR), name="scans")

    app.state.database_path = db_path
    app.state.sample_path = str(demo_file)

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.payload()})

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    @app.get(
        "/health",
        response_model=HealthResponse,
        tags=["ops"],
        summary="Health and row counts",
    )
    def health() -> dict:
        return health_counts(db_path)

    @app.put(
        "/api/pages/{page_id}/ocr",
        response_model=OCRUpdateResponse,
        tags=["pages"],
        summary="Replace one page's OCR and rebuild only affected article indexes",
    )
    def put_page_ocr(
        payload: OCRPageInput,
        page_id: str = ApiPath(
            ...,
            min_length=1,
            max_length=128,
            examples=["page-002"],
            description="Layout page identifier shared by catalog and OCR",
        ),
    ) -> dict:
        return upsert_page_ocr(db_path, page_id, payload)

    @app.get(
        "/api/pages/{page_id}",
        response_model=PageInfo,
        tags=["pages"],
        summary="Inspect a page's OCR revision and blocks",
    )
    def get_page(page_id: str = ApiPath(..., min_length=1, examples=["page-002"])) -> dict:
        return get_page_info(db_path, page_id)

    @app.get(
        "/api/pages/{page_id}/review",
        response_model=PageReviewSummary,
        tags=["review"],
        summary="List word blocks with citation and verification status",
    )
    def get_page_review_route(
        page_id: str = ApiPath(..., min_length=1, examples=["page-006"]),
    ) -> dict:
        return get_page_review(db_path, page_id)

    @app.put(
        "/api/pages/{page_id}/review/blocks/{block_no}",
        response_model=BlockReviewMutationResponse,
        tags=["review"],
        summary="Mark one uncataloged word block as non-body text",
    )
    def put_block_review(
        payload: BlockReviewInput,
        page_id: str = ApiPath(..., min_length=1, max_length=128, examples=["page-006"]),
        block_no: int = ApiPath(..., ge=0, examples=[1]),
    ) -> dict:
        return mark_block_non_body(db_path, page_id, block_no, payload)

    @app.delete(
        "/api/pages/{page_id}/review/blocks/{block_no}",
        response_model=BlockReviewMutationResponse,
        tags=["review"],
        summary="Remove a non-body mark so the block must be re-checked",
    )
    def delete_block_review(
        page_id: str = ApiPath(..., min_length=1, max_length=128, examples=["page-006"]),
        block_no: int = ApiPath(..., ge=0, examples=[1]),
        expected_version: int = Query(
            ...,
            ge=1,
            description="Optimistic token from the current review checklist",
        ),
    ) -> dict:
        return clear_block_review(db_path, page_id, block_no, expected_version)

    @app.put(
        "/api/catalog/articles/{article_id}",
        response_model=ArticleInfo,
        tags=["catalog"],
        summary="Create or replace an article's ordered block references",
    )
    def put_article(
        payload: CatalogArticleInput,
        article_id: str = ApiPath(
            ...,
            min_length=1,
            max_length=128,
            examples=["A-100"],
        ),
    ) -> dict:
        return upsert_catalog_article(db_path, article_id, payload)

    @app.get(
        "/api/articles/{article_id}",
        response_model=ArticleInfo,
        tags=["catalog"],
        summary="Inspect catalog blocks and indexed word count",
    )
    def get_article(
        article_id: str = ApiPath(..., min_length=1, examples=["A-100"])
    ) -> dict:
        return get_article_info(db_path, article_id)

    @app.get(
        "/api/search/phrase",
        response_model=PhraseSearchResponse,
        tags=["search"],
        summary="Find case-insensitive consecutive OCR token phrases",
    )
    @app.get(
        "/api/search",
        response_model=PhraseSearchResponse,
        tags=["search"],
        include_in_schema=False,
    )
    def search(
        q: str = Query(
            ...,
            min_length=1,
            max_length=1024,
            examples=["the lighthouse keeper climbed"],
            description=(
                "Whitespace-separated OCR tokens. Matching is consecutive, "
                "case-insensitive, and may cross page/block boundaries within "
                "one article."
            ),
        ),
        article_id: str | None = Query(
            default=None,
            max_length=128,
            description="Optional article filter for citation lookup",
        ),
        limit: int = Query(
            default=50,
            ge=1,
            le=200,
            description="Maximum number of phrase occurrences returned",
        ),
    ) -> dict:
        return search_phrase(db_path, q, article_id=article_id, limit=limit)

    return app


app = create_app()
