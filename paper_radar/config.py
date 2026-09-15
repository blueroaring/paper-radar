"""分层配置。

优先级（后者覆盖前者）：
    config.example.json（仓库内置默认值，**唯一**的业务参数来源）
      ← config.json（本地配置，已 gitignore）
      ← 环境变量 PAPER_RADAR_<段>__<键>（也支持常见的扁平别名，如 PAPER_RADAR_LLM_API_KEY）

这样做的好处：代码里没有硬编码的研究方向 / 分区表 / 提示词，
想改行为改 JSON 即可；密钥永远只落在 gitignore 的文件或环境变量里。
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_PATH = ROOT / "config.example.json"
CONFIG_PATH = ROOT / "config.json"
SECRETS_PATH = ROOT / "secrets.json"

# 环境变量扁平别名 → 配置路径。写法更顺手，也方便 CI / 计划任务注入。
ENV_ALIASES: dict[str, str] = {
    "PAPER_RADAR_LLM_API_KEY": "llm.api_key",
    "PAPER_RADAR_LLM_BASE_URL": "llm.base_url",
    "PAPER_RADAR_LLM_MODEL": "llm.model",
    "PAPER_RADAR_LLM_PROVIDER": "llm.provider",
    "PAPER_RADAR_ZOTERO_API_KEY": "zotero.api_key",
    "PAPER_RADAR_ZOTERO_USER_ID": "zotero.user_id",
    "PAPER_RADAR_SMTP_HOST": "mail.smtp.host",
    "PAPER_RADAR_SMTP_PORT": "mail.smtp.port",
    "PAPER_RADAR_SMTP_USER": "mail.smtp.username",
    "PAPER_RADAR_SMTP_PASSWORD": "mail.smtp.password",
    "PAPER_RADAR_MAIL_TO": "mail.to",
    "PAPER_RADAR_HOST": "app.host",
    "PAPER_RADAR_PORT": "app.port",
}


def _deep_merge(base: Any, override: Any) -> Any:
    """递归合并：dict 逐键合并，其它类型直接覆盖（list 也整体覆盖，避免语义歧义）。"""
    if isinstance(base, dict) and isinstance(override, dict):
        out = copy.deepcopy(base)
        for key, value in override.items():
            out[key] = _deep_merge(out[key], override[key]) if key in out else copy.deepcopy(value)
        return out
    return copy.deepcopy(override)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh) or {}


def _coerce(text: str) -> Any:
    """把环境变量字符串还原成合适的类型。"""
    text = text.strip()
    if text.startswith("[") or text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if text.isdigit():
        return int(text)
    return text


class Config:
    """点号访问的配置对象，同时负责持久化本地覆盖项。"""

    def __init__(self, data: dict, overrides: dict | None = None):
        self._data = data
        self._overrides = overrides or {}

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        path = Path(path) if path else (CONFIG_PATH if CONFIG_PATH.exists() else None)
        data = _read_json(EXAMPLE_PATH)
        if not data:
            data = _read_json(Path(__file__).resolve().parent / "config.example.json")
        overrides: dict = {}
        secrets = _read_json(SECRETS_PATH)
        if path and path.exists():
            overrides = _read_json(path)
            data = _deep_merge(data, overrides)
        if secrets:
            data = _deep_merge(data, secrets)
        cls._apply_env(data)
        cfg = cls(data, overrides)
        cfg._resolve_secret_files()
        return cfg

    @staticmethod
    def _apply_env(data: dict) -> None:
        # 1) 通用规则：PAPER_RADAR_A__B__C  对应 a.b.c
        prefix = "PAPER_RADAR_"
        for name, value in os.environ.items():
            if not name.startswith(prefix) or name in ENV_ALIASES:
                continue
            dotted = name[len(prefix):].lower().replace("__", ".")
            if "." in dotted:
                _set_path(data, dotted, _coerce(value))
        # 2) 扁平别名
        for name, dotted in ENV_ALIASES.items():
            value = os.environ.get(name)
            if value:
                _set_path(data, dotted, _coerce(value))

    def _resolve_secret_files(self) -> None:
        """支持 api_key_file：从文件读取密钥（与既有 ~/.dsh/zotero-key 习惯兼容）。"""
        key = self.get("zotero.api_key", "")
        if not key:
            file_hint = self.get("zotero.api_key_file", "")
            if file_hint:
                path = Path(os.path.expandvars(os.path.expanduser(file_hint)))
                if path.exists():
                    self._data["zotero"]["api_key"] = path.read_text(encoding="utf-8").strip()
        for section, key_name, file_name in (
            ("llm", "api_key", "api_key_file"),
            ("mail.smtp", "password", "password_file"),
        ):
            if self.get(f"{section}.{key_name}"):
                continue
            hint = self.get(f"{section}.{file_name}", "")
            if hint:
                path = Path(os.path.expandvars(os.path.expanduser(hint)))
                if path.exists():
                    _set_path(self._data, f"{section}.{key_name}", path.read_text(encoding="utf-8").strip())

    # ------------------------------------------------------------------ #
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        _set_path(self._data, dotted, value)

    def as_dict(self, redact: bool = True) -> dict:
        data = copy.deepcopy(self._data)
        if redact:
            _redact(data)
        return data

    # 便捷访问 --------------------------------------------------------- #
    @property
    def root(self) -> Path:
        return ROOT

    @property
    def data_dir(self) -> Path:
        d = Path(self.get("app.data_dir", "data"))
        if not d.is_absolute():
            d = ROOT / d
        d.mkdir(parents=True, exist_ok=True)
        return d

    def db_path(self) -> Path:
        return self.data_dir / self.get("app.db_name", "paper_radar.db")

    def reports_dir(self) -> Path:
        d = self.data_dir / "reports"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # 持久化 ----------------------------------------------------------- #
    def save(self, patch: dict | None = None) -> Path:
        """把（非密钥的）覆盖项写到 config.json。patch 为 None 时落盘当前态。"""
        if patch:
            self._overrides = _deep_merge(self._overrides, patch)
            self._data = _deep_merge(self._data, patch)
        safe = copy.deepcopy(self._overrides)
        _strip_secrets(safe)
        with CONFIG_PATH.open("w", encoding="utf-8") as fh:
            json.dump(safe, fh, ensure_ascii=False, indent=2)
        return CONFIG_PATH


def _set_path(data: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def save_secrets(patch: dict) -> Path:
    """把密钥写进 secrets.json（已 gitignore）。config.json 永远不含密钥。"""
    current = _read_json(SECRETS_PATH)
    merged = _deep_merge(current, patch or {})
    with SECRETS_PATH.open("w", encoding="utf-8") as fh:
        json.dump(merged, fh, ensure_ascii=False, indent=2)
    # 收紧权限（Windows 上 chmod 语义有限，但仍然值得调用）
    try:
        SECRETS_PATH.chmod(0o600)
    except OSError:
        pass
    get_config(reload=True)
    return SECRETS_PATH


SECRET_PATHS = ("llm.api_key", "zotero.api_key", "mail.smtp.password", "sources.semantic_scholar.api_key")
MASK = "***"


def _strip_secrets(data: dict) -> None:
    for dotted in SECRET_PATHS:
        _delete_path(data, dotted)


def _delete_path(data: dict, dotted: str) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        node = node.get(part)
        if not isinstance(node, dict):
            return
    node.pop(parts[-1], None)


def _redact(data: dict) -> None:
    for dotted in SECRET_PATHS:
        parts = dotted.split(".")
        node = data
        ok = True
        for part in parts[:-1]:
            node = node.get(part)
            if not isinstance(node, dict):
                ok = False
                break
        if ok and node.get(parts[-1]):
            node[parts[-1]] = MASK


CONFIG: Config | None = None


def get_config(reload: bool = False) -> Config:
    """进程内单例；reload=True 时重新读盘（UI 改配置后调用）。"""
    global CONFIG
    if CONFIG is None or reload:
        CONFIG = Config.load()
    return CONFIG
