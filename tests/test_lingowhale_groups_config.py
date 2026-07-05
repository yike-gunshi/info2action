import json

from routes import config as config_route


def _group(name="每日查看", channel_name="Alpha-公众号"):
    return {
        "name": name,
        "group_id": f"{name}-id",
        "channels": [{"channel_id": "c1", "name": channel_name}],
    }


def test_lingowhale_groups_use_remote_metadata_without_local_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config_route, "BASE", str(tmp_path))
    monkeypatch.setattr(config_route.remote_db, "feed_read_from_remote", lambda: True)
    monkeypatch.setattr(config_route.remote_db, "app_state_to_remote", lambda: False)
    monkeypatch.setattr(
        config_route.remote_db,
        "get_lingowhale_groups_metadata_remote",
        lambda: [_group()],
    )
    monkeypatch.setattr(
        config_route.remote_db,
        "lingowhale_group_counts_remote",
        lambda: {"每日查看": 3, "未分组": 2},
    )

    result = config_route.get_lingowhale_groups()

    assert result["metadata_backend"] == "remote_settings"
    assert [(g["name"], g["item_count"]) for g in result["groups"]] == [("每日查看", 3)]
    assert result["channel_map"]["Alpha-公众号"] == "每日查看"
    assert result["channel_map"]["Alpha"] == "每日查看"
    assert result["ungrouped_count"] == 2


def test_lingowhale_groups_fallback_to_local_when_remote_empty(monkeypatch, tmp_path):
    groups_path = tmp_path / "data" / "lingowhale" / "groups.json"
    groups_path.parent.mkdir(parents=True)
    groups_path.write_text(json.dumps([_group("AI-机构", "Beta-RSS")], ensure_ascii=False))

    monkeypatch.setattr(config_route, "BASE", str(tmp_path))
    monkeypatch.setattr(config_route.remote_db, "feed_read_from_remote", lambda: True)
    monkeypatch.setattr(config_route.remote_db, "app_state_to_remote", lambda: False)
    monkeypatch.setattr(config_route.remote_db, "get_lingowhale_groups_metadata_remote", lambda: [])
    monkeypatch.setattr(
        config_route.remote_db,
        "lingowhale_group_counts_remote",
        lambda: {"AI-机构": 7, "独立频道": 5},
    )

    result = config_route.get_lingowhale_groups()

    assert result["metadata_backend"] == "local_file"
    assert [(g["name"], g["item_count"]) for g in result["groups"]] == [("AI-机构", 7)]
    assert result["channel_map"]["Beta"] == "AI-机构"
    assert result["ungrouped_count"] == 5


def test_lingowhale_groups_local_counts_when_remote_disabled(monkeypatch, tmp_path):
    groups_path = tmp_path / "data" / "lingowhale" / "groups.json"
    groups_path.parent.mkdir(parents=True)
    groups_path.write_text(json.dumps([_group("产品", "Gamma-公众号")], ensure_ascii=False))

    class Conn:
        def execute(self, _sql):
            return self

        def fetchall(self):
            return [{"g": "产品", "COUNT(*)": 11}, {"g": "", "COUNT(*)": 4}]

        def close(self):
            pass

    monkeypatch.setattr(config_route, "BASE", str(tmp_path))
    monkeypatch.setattr(config_route.remote_db, "feed_read_from_remote", lambda: False)
    monkeypatch.setattr(config_route.remote_db, "app_state_to_remote", lambda: False)
    monkeypatch.setattr(config_route.db, "get_conn", lambda: Conn())

    result = config_route.get_lingowhale_groups()

    assert result["metadata_backend"] == "local_file"
    assert [(g["name"], g["item_count"]) for g in result["groups"]] == [("产品", 11)]
    assert result["ungrouped_count"] == 4
