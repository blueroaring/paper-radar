"""数据源注册表：自动发现 sources/ 下的插件。"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

from .base import Source, year_in_range

__all__ = ["Source", "year_in_range", "discover", "build_sources", "available_sources"]

_REGISTRY: dict[str, type[Source]] | None = None
_SKIP = {"base", "__init__"}


def discover() -> dict[str, type[Source]]:
    """扫描本包，找出所有 Source 子类（按 name 索引）。"""
    global _REGISTRY
    if _REGISTRY is not None:
        return _REGISTRY
    found: dict[str, type[Source]] = {}
    package = importlib.import_module(__name__)
    for mod in pkgutil.iter_modules(package.__path__):
        if mod.name in _SKIP or mod.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{mod.name}")
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, Source)
                and value is not Source
                and getattr(value, "name", "")
            ):
                found[value.name] = value
    _REGISTRY = found
    return found


def build_sources(cfg, fetcher, only: list[str] | None = None) -> list[Source]:
    """实例化启用的数据源。only 非空时只建这些（用于 UI 临时指定）。"""
    wanted = only if only else list(cfg.get("sources.enabled", []) or [])
    registry = discover()
    out: list[Source] = []
    for name in wanted:
        cls = registry.get(name)
        if not cls:
            continue
        try:
            instance = cls(cfg, fetcher)
        except Exception:  # 单个插件构造失败不应该拖垮整次检索
            continue
        out.append(instance)
    return out


def available_sources(cfg) -> list[dict[str, Any]]:
    enabled = set(cfg.get("sources.enabled", []) or [])
    out = []
    for name, cls in sorted(discover().items()):
        out.append(
            {
                "name": name,
                "label": getattr(cls, "label", name),
                "homepage": getattr(cls, "homepage", ""),
                "needs_key": getattr(cls, "needs_key", False),
                "enabled": name in enabled,
                "configured": True,
            }
        )
    return out
