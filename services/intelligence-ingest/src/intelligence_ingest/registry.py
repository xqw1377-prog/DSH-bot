"""加载并校验 source-registry。禁止启用浏览器硬爬方法。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from intelligence_ingest.isolation import FORBIDDEN_METHODS, IsolationError

EXPECTED_CRYPTO = (
    "HYPEUSDT",
    "ENAUSDT",
    "NEARUSDT",
    "TAOUSDT",
    "LINKUSDT",
    "AAVEUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "XMRUSDT",
    "SUIUSDT",
    "FETUSDT",
    "ORDIUSDT",
    "SOLUSDT",
    "ETHUSDT",
    "XRPUSDT",
    "LTCUSDT",
    "AVAXUSDT",
    "WLDUSDT",
    "BNBUSDT",
    "OPUSDT",
    "TIAUSDT",
    "RENDERUSDT",
    "BTCUSDT",
    "BCHUSDT",
)


@dataclass(frozen=True)
class CryptoAsset:
    symbol: str
    asset: str
    name: str
    website: str
    official_x: str
    founder_x: str
    handle_status: str
    github: str | None = None
    rss: str | None = None
    note: str = ""


@dataclass(frozen=True)
class SourceSpec:
    id: str
    market: str
    tier: str
    method: str
    enabled: bool
    url: str
    assets: list[str] = field(default_factory=list)
    event_types: list[str] = field(default_factory=list)
    allow_path_prefixes: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    min_interval_seconds: int = 1
    note: str = ""


@dataclass(frozen=True)
class SourceRegistry:
    version: int
    us_market: dict[str, Any]
    crypto_assets: list[CryptoAsset]
    sources: list[SourceSpec]
    path: Path

    def asset_by_symbol(self) -> dict[str, CryptoAsset]:
        return {item.symbol: item for item in self.crypto_assets}

    def enabled_sources(self, environ: dict[str, str] | None = None) -> list[SourceSpec]:
        env = environ if environ is not None else os.environ
        ready: list[SourceSpec] = []
        for source in self.sources:
            if not source.enabled:
                continue
            if any(not str(env.get(name) or "").strip() for name in source.requires):
                continue
            ready.append(source)
        return ready


def default_registry_path() -> Path:
    env = os.environ.get("DSH_SOURCE_REGISTRY", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "data" / "source-registry.yaml"


def load_registry(path: Path | None = None) -> SourceRegistry:
    target = path or default_registry_path()
    payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("source-registry must be a mapping")
    assets = [_asset(row) for row in payload.get("crypto_assets") or []]
    symbols = [item.symbol for item in assets]
    if symbols != list(EXPECTED_CRYPTO):
        raise ValueError("crypto_assets must match 6celue DEFAULT_SYMBOLS_ORDER")
    sources = [_source(row) for row in payload.get("sources") or []]
    for source in sources:
        if source.method in FORBIDDEN_METHODS:
            raise IsolationError(f"{source.id} uses forbidden method {source.method}")
        if source.enabled and source.method == "X_FILTERED_STREAM":
            # 允许登记为 enabled，但必须声明官方 API，不能指向网页时间线。
            if "api.x.com" not in source.url and "api.twitter.com" not in source.url:
                raise IsolationError("X source must use official X API URL")
    return SourceRegistry(
        version=int(payload.get("version") or 1),
        us_market=dict(payload.get("us_market") or {}),
        crypto_assets=assets,
        sources=sources,
        path=target,
    )


def expand_derived_sources(registry: SourceRegistry) -> list[SourceSpec]:
    """由币种白名单派生 GitHub Release / 项目 RSS，不另外手写 24 份。"""
    derived: list[SourceSpec] = []
    for asset in registry.crypto_assets:
        if asset.rss:
            derived.append(
                SourceSpec(
                    id=f"rss-{asset.asset.lower()}",
                    market="CRYPTO",
                    tier="PRIMARY",
                    method="RSS",
                    enabled=True,
                    url=asset.rss,
                    assets=[asset.symbol],
                    event_types=["GOVERNANCE", "LISTING"],
                )
            )
        if asset.github:
            derived.append(
                SourceSpec(
                    id=f"github-{asset.asset.lower()}",
                    market="CRYPTO",
                    tier="PRIMARY",
                    method="RSS",
                    enabled=True,
                    url=f"https://github.com/{asset.github}/releases.atom",
                    assets=[asset.symbol],
                    event_types=["GOVERNANCE"],
                )
            )
    return derived


def _asset(row: dict[str, Any]) -> CryptoAsset:
    return CryptoAsset(
        symbol=str(row["symbol"]),
        asset=str(row["asset"]),
        name=str(row["name"]),
        website=str(row.get("website") or ""),
        official_x=str(row.get("official_x") or ""),
        founder_x=str(row.get("founder_x") or ""),
        handle_status=str(row.get("handle_status") or "unknown"),
        github=str(row["github"]) if row.get("github") else None,
        rss=str(row["rss"]) if row.get("rss") else None,
        note=str(row.get("note") or ""),
    )


def _source(row: dict[str, Any]) -> SourceSpec:
    return SourceSpec(
        id=str(row["id"]),
        market=str(row["market"]),
        tier=str(row.get("tier") or "SECONDARY"),
        method=str(row["method"]),
        enabled=bool(row.get("enabled")),
        url=str(row.get("url") or ""),
        assets=list(row.get("assets") or []),
        event_types=list(row.get("event_types") or []),
        allow_path_prefixes=list(row.get("allow_path_prefixes") or []),
        requires=list(row.get("requires") or []),
        min_interval_seconds=int(row.get("min_interval_seconds") or 1),
        note=str(row.get("note") or ""),
    )


def x_filter_rules(registry: SourceRegistry) -> list[str]:
    """官方 Filtered Stream 规则草稿。没有 Bearer 时只生成，不连接。"""
    rules: list[str] = []
    for asset in registry.crypto_assets:
        handles = [item for item in (asset.official_x, asset.founder_x) if item]
        if not handles:
            continue
        clause = " OR ".join(f"from:{handle}" for handle in handles)
        rules.append(f"({clause}) -is:retweet")
    return rules
