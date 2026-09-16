from src.media_assets import image_urls, merge_media, normalize_media


def test_normalize_media_rejects_unsafe_schemes_and_uses_safe_fallbacks():
    assets = normalize_media(
        "data:image/png;base64,abc",
        [
            {
                "type": "video",
                "url": "https://cdn.example/video.mp4",
                "poster_url": "javascript:alert(1)",
                "source_url": "javascript:alert(2)",
            },
            {"type": "embed", "url": "javascript:alert(3)"},
        ],
        source_url="https://example.com/post",
    )

    assert assets == [{
        "type": "video",
        "url": "https://cdn.example/video.mp4",
        "source_url": "https://example.com/post",
    }]


def test_normalize_media_keeps_web_and_app_local_urls():
    assets = normalize_media(
        "/media/cover.jpg",
        [{"type": "image", "url": "http://cdn.example/image.jpg"}],
    )

    assert [asset["url"] for asset in assets] == [
        "http://cdn.example/image.jpg",
        "/media/cover.jpg",
    ]
    assert image_urls(assets) == [
        "http://cdn.example/image.jpg",
        "/media/cover.jpg",
    ]


def test_normalize_media_uses_twitter_cover_as_video_poster_without_duplicate_image():
    assets = normalize_media(
        "/api/media/twitter-poster/2092055394852159836.jpg",
        [{"type": "video", "url": "https://cdn.example/video.mp4"}],
        source_url="https://x.com/example/status/1",
    )

    assert assets == [{
        "type": "video",
        "url": "https://cdn.example/video.mp4",
        "source_url": "https://x.com/example/status/1",
        "poster_url": "/api/media/twitter-poster/2092055394852159836.jpg",
    }]
    assert image_urls(assets) == [
        "/api/media/twitter-poster/2092055394852159836.jpg",
    ]


def test_normalize_media_keeps_explicit_image_when_cover_becomes_video_poster():
    assets = normalize_media(
        "https://media.example/video_posters/item.jpg",
        [
            {"type": "video", "url": "https://cdn.example/video.mp4"},
            {"type": "image", "url": "https://cdn.example/other-source.jpg"},
        ],
    )

    assert assets == [
        {
            "type": "video",
            "url": "https://cdn.example/video.mp4",
            "source_url": "https://cdn.example/video.mp4",
            "poster_url": "https://media.example/video_posters/item.jpg",
        },
        {
            "type": "image",
            "url": "https://cdn.example/other-source.jpg",
            "source_url": "https://cdn.example/other-source.jpg",
        },
    ]


def test_normalize_media_assigns_cover_to_first_playable_asset_missing_a_poster():
    assets = normalize_media(
        "https://cdn.example/embed-cover.jpg",
        [
            {
                "type": "video",
                "url": "https://cdn.example/video.mp4",
                "poster_url": "https://cdn.example/video-poster.jpg",
            },
            {"type": "embed", "url": "https://example.com/embed"},
            {"type": "image", "url": "https://cdn.example/other-source.jpg"},
        ],
    )

    assert assets[0]["poster_url"] == "https://cdn.example/video-poster.jpg"
    assert assets[1]["poster_url"] == "https://cdn.example/embed-cover.jpg"
    assert [asset["url"] for asset in assets if asset["type"] == "image"] == [
        "https://cdn.example/other-source.jpg",
    ]


def test_normalize_media_keeps_distinct_cover_when_video_already_has_poster():
    assets = normalize_media(
        "https://cdn.example/independent-cover.jpg",
        [{
            "type": "video",
            "url": "https://cdn.example/video.mp4",
            "poster_url": "https://cdn.example/video-poster.jpg",
        }],
    )

    assert [asset["url"] for asset in assets if asset["type"] == "image"] == [
        "https://cdn.example/independent-cover.jpg",
    ]


def test_normalize_media_does_not_duplicate_cover_matching_existing_video_poster():
    assets = normalize_media(
        "https://cdn.example/video-poster.jpg",
        [{
            "type": "video",
            "url": "https://cdn.example/video.mp4",
            "poster_url": "https://cdn.example/video-poster.jpg",
        }],
    )

    assert [asset for asset in assets if asset["type"] == "image"] == []


def test_merge_media_filters_unsafe_urls_defensively():
    merged = merge_media([[{
        "type": "embed",
        "url": "https://example.com/embed",
        "source_url": "file:///etc/passwd",
        "poster_url": "javascript:alert(1)",
    }, {
        "type": "video",
        "url": "javascript:alert(2)",
    }]])

    assert merged == [{
        "type": "embed",
        "url": "https://example.com/embed",
        "source_url": "https://example.com/embed",
    }]
