from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import sync_local_assets_to_supabase as asset_sync  # noqa: E402


def test_collect_referenced_assets_from_cover_and_nested_media():
    rows = [
        {
            "id": "item-1",
            "platform": "lingowhale",
            "cover_url": "/images/lingowhale/a.webp",
            "media_json": [
                {"url": "/images/lingowhale/a.webp"},
                {"nested": {"thumb": "/images/twitter/b.jpg?width=120"}},
                {"ignored": "https://example.com/image.jpg"},
            ],
        },
        {
            "id": "item-2",
            "platform": "twitter",
            "cover_url": "/api/media/twitter-poster/123.jpg",
            "media_json": '["/images/twitter/b.jpg", "/images/../escape.jpg"]',
        },
    ]

    assets = asset_sync.collect_referenced_assets(rows)

    assert sorted(assets) == ["images/lingowhale/a.webp", "images/twitter/b.jpg"]
    assert assets["images/lingowhale/a.webp"].ref_count == 2
    assert assets["images/lingowhale/a.webp"].fields == {"cover_url": 1, "media_json": 1}
    assert assets["images/twitter/b.jpg"].item_ids == {"item-1", "item-2"}
    assert assets["images/twitter/b.jpg"].platforms == {"lingowhale": 1, "twitter": 1}


def test_summarize_assets_reports_existing_missing_and_upload_bytes(tmp_path):
    data_dir = tmp_path / "data"
    image_path = data_dir / "images" / "twitter" / "a.jpg"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"abc")

    rows = [
        {"id": "i1", "platform": "twitter", "cover_url": "/images/twitter/a.jpg", "media_json": None},
        {"id": "i2", "platform": "xhs", "cover_url": "/images/xhs/missing.webp", "media_json": None},
        {"id": "i3", "platform": "bilibili", "cover_url": "/images/bilibili/existing.png", "media_json": None},
    ]
    existing_path = data_dir / "images" / "bilibili" / "existing.png"
    existing_path.parent.mkdir(parents=True)
    existing_path.write_bytes(b"existing")
    assets = asset_sync.collect_referenced_assets(rows)

    summary = asset_sync.summarize_assets(
        assets,
        local_data_dir=data_dir,
        existing_paths={"images/bilibili/existing.png"},
    )

    assert summary["selected_assets"] == 3
    assert summary["local_present"] == 2
    assert summary["local_missing"] == 1
    assert summary["remote_metadata_existing"] == 1
    assert summary["upload_candidates"] == 1
    assert summary["upload_bytes"] == 3
    assert summary["by_extension"] == {".jpg": 1, ".png": 1, ".webp": 1}
    assert summary["by_platform"] == {"bilibili": 1, "twitter": 1, "xhs": 1}
    assert summary["missing_assets"] == [
        {
            "object_path": "images/xhs/missing.webp",
            "referenced_by": ["i2"],
            "platforms": {"xhs": 1},
        }
    ]


def test_normalize_image_reference_rejects_non_images_and_traversal():
    assert asset_sync.normalize_image_reference("/images/twitter/a.jpg") == (
        "images/twitter/a.jpg",
        "images/twitter/a.jpg",
    )
    assert asset_sync.normalize_image_reference("/images/twitter/a.jpg?name=x") == (
        "images/twitter/a.jpg",
        "images/twitter/a.jpg",
    )
    assert asset_sync.normalize_image_reference("/api/media/twitter-poster/1.jpg") is None
    assert asset_sync.normalize_image_reference("https://example.com/a.jpg") is None
    assert asset_sync.normalize_image_reference("/images/../feed.db") is None
