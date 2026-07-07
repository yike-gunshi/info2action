FRESHNESS_WARN_HOURS = 24
FRESHNESS_CRIT_HOURS = 72


def classify_platform_freshness(age_hours: float) -> str:
    if age_hours >= FRESHNESS_CRIT_HOURS:
        return "crit"
    if age_hours >= FRESHNESS_WARN_HOURS:
        return "warn"
    return "ok"


def platform_freshness_message(age_hours: float) -> str:
    return f"已 {int(age_hours)}h 未更新"
