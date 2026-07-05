"""v13.0: URL 归一化单元测试"""
import hashlib
import os
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))

from utils.url_normalize import normalize_url


# ---------- Twitter ----------

class TestTwitter:
    def test_x_com_status(self):
        r = normalize_url("https://x.com/jack/status/123456789")
        assert r.platform == "twitter"
        assert r.item_id == "123456789"
        assert r.raw_id == "123456789"
        assert "x.com" in r.canonical_url
        assert "123456789" in r.canonical_url

    def test_twitter_com_status(self):
        r = normalize_url("https://twitter.com/jack/status/123456789")
        assert r.platform == "twitter"
        assert r.item_id == "123456789"

    def test_x_and_twitter_same_id(self):
        """FEATURE-SPEC R8.1:x.com / twitter.com 归一到同一 item_id"""
        a = normalize_url("https://x.com/u/status/999")
        b = normalize_url("https://twitter.com/u/status/999")
        assert a.item_id == b.item_id == "999"

    def test_case_insensitive_host(self):
        """大小写 host (`Twitter.com` / `X.COM`) 归一"""
        r = normalize_url("https://Twitter.COM/jack/status/123")
        assert r.platform == "twitter"
        assert r.item_id == "123"

    def test_i_status_no_user(self):
        """无 user handle 的 /i/status/ 形态"""
        r = normalize_url("https://x.com/i/status/55555")
        assert r.platform == "twitter"
        assert r.item_id == "55555"

    def test_with_trailing_params(self):
        r = normalize_url("https://x.com/u/status/111?s=20&t=abc")
        assert r.item_id == "111"


# ---------- YouTube ----------

class TestYouTube:
    def test_watch_v(self):
        r = normalize_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert r.platform == "youtube"
        assert r.item_id == "yt_dQw4w9WgXcQ"
        assert r.raw_id == "dQw4w9WgXcQ"

    def test_youtu_be_short(self):
        r = normalize_url("https://youtu.be/dQw4w9WgXcQ")
        assert r.item_id == "yt_dQw4w9WgXcQ"

    def test_shorts(self):
        r = normalize_url("https://youtube.com/shorts/dQw4w9WgXcQ")
        assert r.item_id == "yt_dQw4w9WgXcQ"

    def test_embed(self):
        r = normalize_url("https://youtube.com/embed/dQw4w9WgXcQ")
        assert r.item_id == "yt_dQw4w9WgXcQ"

    def test_four_forms_same_id(self):
        """FEATURE-SPEC R3.3 / R8.2:4 种 URL 归一到同一 yt_{video_id}"""
        ids = {
            normalize_url("https://www.youtube.com/watch?v=abc123DEFxy").item_id,
            normalize_url("https://youtu.be/abc123DEFxy").item_id,
            normalize_url("https://youtube.com/shorts/abc123DEFxy").item_id,
            normalize_url("https://youtube.com/embed/abc123DEFxy").item_id,
        }
        assert ids == {"yt_abc123DEFxy"}

    def test_with_t_param(self):
        r = normalize_url("https://www.youtube.com/watch?v=abc123DEFxy&t=45s")
        assert r.item_id == "yt_abc123DEFxy"

    def test_with_leading_params(self):
        """v=... 不是第一个参数也要能匹配"""
        r = normalize_url("https://www.youtube.com/watch?feature=share&v=abc123DEFxy")
        assert r.item_id == "yt_abc123DEFxy"

    def test_case_sensitive_video_id(self):
        """FEATURE-SPEC 硬约束:YouTube video_id 大小写敏感,不 lower-case"""
        upper = normalize_url("https://youtu.be/ABCDEFGHIJK")
        lower = normalize_url("https://youtu.be/abcdefghijk")
        assert upper.item_id == "yt_ABCDEFGHIJK"
        assert lower.item_id == "yt_abcdefghijk"
        assert upper.item_id != lower.item_id

    def test_m_youtube_com(self):
        """移动版 m.youtube.com 也要识别"""
        r = normalize_url("https://m.youtube.com/watch?v=abc123DEFxy")
        assert r.item_id == "yt_abc123DEFxy"


# ---------- manual (其他链接) ----------

class TestManual:
    def test_blog_url_md5(self):
        url = "https://example.com/article/foo"
        r = normalize_url(url)
        assert r.platform == "manual"
        assert r.item_id == hashlib.md5(url.encode()).hexdigest()
        assert r.canonical_url == url
        assert r.raw_id is None

    def test_empty_string(self):
        r = normalize_url("")
        assert r.platform == "manual"
        # md5 空字符串
        assert r.item_id == hashlib.md5(b"").hexdigest()

    def test_none(self):
        # 不抛异常,走 manual
        r = normalize_url(None)  # type: ignore[arg-type]
        assert r.platform == "manual"

    def test_bilibili_url_manual(self):
        """B 站链接走 manual(本 feature 非目标)"""
        r = normalize_url("https://www.bilibili.com/video/BV1xx411c7mD")
        assert r.platform == "manual"

    def test_weixin_url_manual(self):
        r = normalize_url("https://mp.weixin.qq.com/s/abc123")
        assert r.platform == "manual"

    def test_leading_trailing_whitespace(self):
        """前后空白 strip 后仍匹配 Twitter"""
        r = normalize_url("  https://x.com/u/status/777  ")
        assert r.platform == "twitter"
        assert r.item_id == "777"


# ---------- 边界 / 异常 ----------

class TestEdgeCases:
    def test_youtube_invalid_short_id(self):
        """video_id 少于 11 位 → 识别失败走 manual"""
        r = normalize_url("https://youtu.be/abc")
        assert r.platform == "manual"

    def test_youtube_extra_path(self):
        """/channel/ 不是视频形态 → manual"""
        r = normalize_url("https://youtube.com/channel/UC_abc")
        assert r.platform == "manual"

    def test_twitter_not_status(self):
        """只是 profile 页 → manual"""
        r = normalize_url("https://x.com/jack")
        assert r.platform == "manual"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
