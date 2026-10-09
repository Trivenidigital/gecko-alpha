from gecko.__main__ import register


@register("freshness")
async def freshness_job(settings, ns) -> int:  # replaced in Task 3
    return 0
