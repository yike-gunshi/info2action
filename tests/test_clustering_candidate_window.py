"""perf-v27 P2: 聚类候选窗口收窄 30→3 天的哨兵测试。

产品决策(目标架构定稿 §0-8):新内容只和近 3 天的簇比对——item 侧时间
邻接判定本就是 ±3 天,30 天 DB 候选窗只是让每条新内容和 10 倍量的簇算
向量距离(生产 pg_stat 实测该负载累计 809min=总耗时第一)。
若有人把窗口调回去,这里会先红——改之前先读定稿文档。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from clustering import pipeline  # noqa: E402

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "config.json")


def test_candidate_window_default_is_3_days():
    assert pipeline._CANDIDATE_WINDOW_DAYS_DEFAULT == 3


def test_config_candidate_window_is_3_days():
    with open(_CONFIG_PATH) as f:
        cfg = json.load(f)
    clustering = cfg["global"]["clustering"]
    assert clustering["candidate_window_days"] == 3
    # item 侧邻接窗口与 DB 候选窗口对齐(两者语义不同但数值应一致)
    assert clustering["temporal_candidate_window_days"] == 3
