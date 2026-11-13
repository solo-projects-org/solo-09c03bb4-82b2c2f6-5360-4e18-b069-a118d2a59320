import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "samples" / "demo.json"
REVIEW_PAGE = ROOT / "samples" / "page-006-review.json"
REVIEW_ARTICLE = ROOT / "samples" / "page-006-article.json"
RERUN_IDENTICAL = ROOT / "samples" / "page-006-rerun-identical.json"
RERUN_CHANGED = ROOT / "samples" / "page-006-rerun-changed.json"


@pytest.fixture()
def client(tmp_path):
    app = create_app(
        database_path=tmp_path / "archive.sqlite3",
        sample_path=DEMO,
        seed_demo=True,
    )
    with TestClient(app) as test_client:
        yield test_client


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def setup_review_page(client: TestClient) -> dict:
    response = client.put("/api/pages/page-006/ocr", json=load_json(REVIEW_PAGE))
    assert response.status_code == 200, response.text
    response = client.put(
        "/api/catalog/articles/A-600", json=load_json(REVIEW_ARTICLE)
    )
    assert response.status_code == 200, response.text
    response = client.get("/api/pages/page-006/review")
    assert response.status_code == 200, response.text
    return response.json()


def block_by_no(summary: dict, block_no: int) -> dict:
    return next(
        block for block in summary["blocks"] if block["block_no"] == block_no
    )


def mark(client: TestClient, block_no: int, version: int, reason: str | None = None):
    return client.put(
        f"/api/pages/page-006/review/blocks/{block_no}",
        json={"expected_version": version, "reason": reason},
    )


def test_checklist_reports_text_boxes_and_statuses(client):
    summary = setup_review_page(client)
    assert summary["review_status"] == "pending_review"
    assert summary["blocks_total"] == 4
    assert summary["blocks_cataloged"] == 1
    assert summary["blocks_marked_non_body"] == 0
    assert summary["blocks_pending"] == 3

    header = block_by_no(summary, 1)
    assert header["referenced"] is False
    assert header["review_status"] == "pending_review"
    assert header["review_version"] == 1
    assert header["last_change"] == "ocr_block_added"
    assert header["text"] == "HARBOR GAZETTE PAGE SIX"
    assert header["bbox"] == {
        "x0": 100.0,
        "y0": 90.0,
        "x1": 1485.0,
        "y1": 125.0,
    }
    assert [word["word_no"] for word in header["words"]] == [0, 1, 2, 3]
    assert header["words"][0] == {
        "word_no": 0,
        "text": "HARBOR",
        "bbox": {"x0": 100.0, "y0": 90.0, "x1": 215.0, "y1": 125.0},
    }

    cataloged = block_by_no(summary, 4)
    assert cataloged["referenced"] is True
    assert cataloged["review_status"] == "cataloged"
    assert cataloged["text"] == "Council voted to repair the pier"


def test_marking_blocks_completes_the_page(client):
    summary = setup_review_page(client)
    for block_no, reason in [(1, "running header"), (2, "advertisement")]:
        target = block_by_no(summary, block_no)
        response = mark(
            client, block_no, target["review_version"], reason=reason
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["review"]["review_status"] == "marked_non_body"
        assert body["review"]["marked_reason"] == reason
        assert body["review"]["marked_version"] == target["review_version"]

    # The uncataloged body article in block 3 is still unaccounted for.
    response = client.get("/api/pages/page-006/review")
    summary = response.json()
    assert summary["review_status"] == "pending_review"
    assert summary["blocks_marked_non_body"] == 2
    assert summary["blocks_pending"] == 1

    target = block_by_no(summary, 3)
    response = mark(client, 3, target["review_version"], reason="checked, not body")
    assert response.status_code == 200
    assert response.json()["page_review_status"] == "complete"

    final = client.get("/api/pages/page-006/review").json()
    assert final["review_status"] == "complete"
    assert final["blocks_pending"] == 0


def test_cataloged_block_rejects_a_mark(client):
    summary = setup_review_page(client)
    cataloged = block_by_no(summary, 4)
    response = mark(client, 4, cataloged["review_version"])
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "block_now_cataloged"
    assert detail["recheck_required"] is True
    assert client.get("/health").json()["review_marks"] == 0


def test_identical_ocr_rerun_carries_marks_and_keeps_versions(client):
    summary = setup_review_page(client)
    for block_no in (1, 2):
        target = block_by_no(summary, block_no)
        assert mark(client, block_no, target["review_version"]).status_code == 200

    response = client.put(
        "/api/pages/page-006/ocr", json=load_json(RERUN_IDENTICAL)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    carried = {(item["page_id"], item["block_no"]) for item in body["review_marks_carried"]}
    assert carried == {("page-006", 1), ("page-006", 2)}
    assert body["review_marks_reset"] == []

    refreshed = client.get("/api/pages/page-006/review").json()
    for block_no in (1, 2):
        block = block_by_no(refreshed, block_no)
        assert block["review_status"] == "marked_non_body"
        assert block["review_version"] == 1
    # Old optimistic tokens still validate against the identical rerun.
    assert mark(client, 1, 1).status_code == 200


def test_changed_ocr_rerun_resets_only_changed_marks_and_bumps_versions(client):
    summary = setup_review_page(client)
    for block_no in (1, 2):
        target = block_by_no(summary, block_no)
        assert mark(client, block_no, target["review_version"]).status_code == 200

    response = client.put(
        "/api/pages/page-006/ocr", json=load_json(RERUN_CHANGED)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    reset = body["review_marks_reset"]
    assert len(reset) == 1
    assert reset[0]["block_no"] == 1
    assert reset[0]["review_version"] == 2
    carried = {(item["page_id"], item["block_no"]) for item in body["review_marks_carried"]}
    assert carried == {("page-006", 2)}

    refreshed = client.get("/api/pages/page-006/review").json()
    header = block_by_no(refreshed, 1)
    assert header["review_status"] == "pending_review"
    assert header["review_version"] == 2
    assert header["last_change"] == "ocr_content_changed"
    assert header["marked_at"] is None

    advert = block_by_no(refreshed, 2)
    assert advert["review_status"] == "marked_non_body"
    assert advert["review_version"] == 1

    body_article = block_by_no(refreshed, 3)
    assert body_article["review_status"] == "pending_review"
    assert body_article["review_version"] == 2

    # The pre-change token is now stale even for re-marking the header.
    stale = mark(client, 1, 1)
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["code"] == "review_version_stale"
    assert detail["current_version"] == 2
    assert detail["recheck_required"] is True

    # A block changed elsewhere does not invalidate block 2's token.
    assert mark(client, 2, 1).status_code == 200


def test_deleted_then_restored_block_rejects_old_token(client):
    summary = setup_review_page(client)
    target = block_by_no(summary, 1)
    assert mark(client, 1, target["review_version"]).status_code == 200

    payload = load_json(REVIEW_PAGE)
    payload["blocks"] = [
        block for block in payload["blocks"] if block["block_no"] != 1
    ]
    response = client.put("/api/pages/page-006/ocr", json=payload)
    assert response.status_code == 200
    missing = client.put(
        "/api/pages/page-006/review/blocks/1",
        json={"expected_version": 1},
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "block_not_found"

    # Restore the page byte-for-byte: history keeps a fresh monotonic token.
    response = client.put(
        "/api/pages/page-006/ocr", json=load_json(REVIEW_PAGE)
    )
    assert response.status_code == 200
    refreshed = client.get("/api/pages/page-006/review").json()
    header = block_by_no(refreshed, 1)
    assert header["review_version"] == 3
    assert header["review_status"] == "pending_review"

    stale = mark(client, 1, 1)
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "review_version_stale"
    assert mark(client, 1, header["review_version"]).status_code == 200


def test_catalog_citation_revokes_mark_and_uncataloging_forces_recheck(client):
    summary = setup_review_page(client)
    target = block_by_no(summary, 3)
    assert mark(client, 3, target["review_version"]).status_code == 200
    assert client.get("/health").json()["review_marks"] == 1

    # A second article cites the previously marked block: mark is revoked in
    # the same transaction and the token is bumped.
    response = client.put(
        "/api/catalog/articles/A-601",
        json={
            "title": "Ferry Service Note",
            "blocks": [{"page_id": "page-006", "block_no": 3}],
        },
    )
    assert response.status_code == 200, response.text
    revoked = response.json()["revoked_review_marks"]
    assert revoked == [
        {"page_id": "page-006", "block_no": 3, "review_version": 2}
    ]
    assert client.get("/health").json()["review_marks"] == 0

    refreshed = client.get("/api/pages/page-006/review").json()
    cited = block_by_no(refreshed, 3)
    assert cited["review_status"] == "cataloged"
    assert cited["review_version"] == 2
    assert mark(client, 3, 2).status_code == 409

    # Removing the citation leaves the block uncataloged again and forces a
    # fresh review: yet another token bump, no mark resurrected.
    response = client.put(
        "/api/catalog/articles/A-601",
        json={
            "title": "Ferry Service Note",
            "blocks": [{"page_id": "page-006", "block_no": 4}],
        },
    )
    assert response.status_code == 200
    refreshed = client.get("/api/pages/page-006/review").json()
    freed = block_by_no(refreshed, 3)
    assert freed["review_status"] == "pending_review"
    assert freed["review_version"] == 3
    assert mark(client, 3, 2).status_code == 409
    assert mark(client, 3, 3).status_code == 200


def test_stale_token_after_other_block_change_still_validates_other_blocks(client):
    summary = setup_review_page(client)
    block2 = block_by_no(summary, 2)

    # Blocks 1 and 3 change out of band; block 2's token must be unaffected.
    payload = load_json(RERUN_CHANGED)
    response = client.put("/api/pages/page-006/ocr", json=payload)
    assert response.status_code == 200

    assert mark(client, 2, block2["review_version"]).status_code == 200

    # While block 2 keeps its token, changed block 1 rejects the old token.
    stale = mark(client, 1, 1)
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "review_version_stale"


def test_delete_mark_returns_block_to_pending(client):
    summary = setup_review_page(client)
    target = block_by_no(summary, 2)
    assert mark(client, 2, target["review_version"]).status_code == 200

    bad_token = client.delete(
        "/api/pages/page-006/review/blocks/2?expected_version=999"
    )
    assert bad_token.status_code == 409
    assert bad_token.json()["detail"]["code"] == "review_version_stale"

    response = client.delete(
        f"/api/pages/page-006/review/blocks/2?expected_version={target['review_version']}"
    )
    assert response.status_code == 200, response.text
    assert response.json()["review"]["review_status"] == "pending_review"

    again = client.delete(
        f"/api/pages/page-006/review/blocks/2?expected_version={target['review_version']}"
    )
    assert again.status_code == 404
    assert again.json()["detail"]["code"] == "review_mark_not_found"


def test_rejected_ocr_upload_leaves_review_state_untouched(client):
    summary = setup_review_page(client)
    target = block_by_no(summary, 1)
    assert mark(client, 1, target["review_version"]).status_code == 200

    # page-002 block 2 is cataloged by the demo data; omitting it must abort
    # the whole transaction before any review row changes.
    bad_payload = load_json(ROOT / "samples" / "page-002-missing-block.json")
    response = client.put("/api/pages/page-002/ocr", json=bad_payload)
    assert response.status_code == 409

    refreshed = client.get("/api/pages/page-006/review").json()
    header = block_by_no(refreshed, 1)
    assert header["review_status"] == "marked_non_body"
    assert header["review_version"] == 1
    assert client.get("/health").json()["review_marks"] == 1


def test_rejected_catalog_upload_leaves_review_state_untouched(client):
    summary = setup_review_page(client)
    target = block_by_no(summary, 3)
    assert mark(client, 3, target["review_version"]).status_code == 200

    # Replacing an existing article with a missing OCR block is rejected 409;
    # neither catalog nor review marks may change.
    response = client.put(
        "/api/catalog/articles/A-600",
        json={"blocks": [{"page_id": "page-999", "block_no": 1}]},
    )
    assert response.status_code == 409

    refreshed = client.get("/api/pages/page-006/review").json()
    body_block = block_by_no(refreshed, 3)
    assert body_block["review_status"] == "marked_non_body"
    assert body_block["review_version"] == 1
    assert client.get("/health").json()["review_marks"] == 1


def test_review_routes_for_unknown_page_and_block(client):
    assert client.get("/api/pages/unknown/review").status_code == 404
    response = client.put(
        "/api/pages/page-006/review/blocks/9",
        json={"expected_version": 1},
    )
    assert response.status_code == 404
