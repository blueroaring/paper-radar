"""数据源插件基类与注册表。

新增一个数据源只需要两步（其余全自动）：
    1. 在 sources/ 目录里新建 xxx.py；
    2. 继承 Source，实现 search()，类属性 name/label 写清楚。
sources/__init__.py 会自动发现它；config.json 的 sources.enabled 决定启不启用。
"""

from __future__ import annotations

import abc
from typing import Any

from ..models import Paper


class Source(abc.ABC):
    """所有数据源插件的基类。"""

    name: str = ""             # 机器名，用于配置与日志
    label: str = ""            # 人读名字，用于 UI
    homepage: str = ""
    kinds: tuple[str, ...] = ("journal", "conference", "preprint")
    needs_key: bool = False

    def __init__(self, cfg, fetcher):
        self.cfg = cfg
        self.http = fetcher
        self.section = cfg.get(f"sources.{self.name}", {}) or {}
        self.limit = int(cfg.get("sources.per_source_limit", 20))

    # ------------------------------------------------------------------ #
    @abc.abstractmethod
    def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
    ) -> list[Paper]:
        """按检索式返回候选论文。实现里应自行容错，失败时返回空列表或抛 FetchError。"""

    # ------------------------------------------------------------------ #
    def is_configured(self) -> bool:
        """需要密钥的数据源可以重写它（例如 Semantic Scholar 有 key 时限速更宽）。"""
        return True

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label or self.name,
            "homepage": self.homepage,
            "kinds": list(self.kinds),
            "needs_key": self.needs_key,
            "configured": self.is_configured(),
        }


def year_in_range(year: int | None, year_from: int | None, year_to: int | None) -> bool:
    if year is None:
        return True
    if year_from and year < year_from:
        return False
    if year_to and year > year_to:
        return False
    return True
