"""Token cost, in money.

agit ships no price list. Published rates change weekly and differ per account,
so inventing numbers would mean quietly lying about spend. Instead you tell agit
your rate once and it does the arithmetic everywhere after that:

    agit price --in 0.20 --out 0.60            # per million tokens, USD
    agit price --in 18 --out 54 --currency INR

Rates are stored per provider/model in .agit/config.json. Providers that cannot
cost anything -- the offline scripted planner, a local Ollama -- are free by
definition and need no configuration.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .config import Config

#: Providers that are free because nothing leaves the machine.
FREE_PROVIDERS = ("script", "ollama")

SYMBOL = {"USD": "$", "INR": "₹", "EUR": "€", "GBP": "£"}


@dataclass
class Rate:
    input_per_m: float
    output_per_m: float
    currency: str = "USD"
    source: str = "config"

    @property
    def free(self) -> bool:
        return self.input_per_m == 0 and self.output_per_m == 0

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.input_per_m + tokens_out * self.output_per_m) / 1_000_000.0

    def format(self, tokens_in: int, tokens_out: int) -> str:
        if self.free:
            return "free"
        amount = self.cost(tokens_in, tokens_out)
        symbol = SYMBOL.get(self.currency.upper(), self.currency.upper() + " ")
        if amount and amount < 0.01:
            return "%s%.4f" % (symbol, amount)
        return "%s%.2f" % (symbol, amount)


def key_for(cfg: Config) -> str:
    return "%s/%s" % (cfg.provider, cfg.model or "default")


def rate_for(cfg: Config, project: Optional[Dict[str, Any]] = None) -> Optional[Rate]:
    """The rate to use, or None when the user has not set one."""
    env_in, env_out = os.environ.get("AGIT_PRICE_IN"), os.environ.get("AGIT_PRICE_OUT")
    if env_in and env_out:
        try:
            return Rate(float(env_in), float(env_out),
                        os.environ.get("AGIT_PRICE_CURRENCY", "USD"), "env")
        except ValueError:
            pass
    if cfg.provider in FREE_PROVIDERS:
        return Rate(0.0, 0.0, "USD", "builtin")
    prices = (project or {}).get("prices") or {}
    for key in (key_for(cfg), "%s/default" % cfg.provider):
        entry = prices.get(key)
        if isinstance(entry, dict):
            try:
                return Rate(float(entry.get("in", 0)), float(entry.get("out", 0)),
                            str(entry.get("currency", "USD")), "config")
            except (TypeError, ValueError):
                continue
    return None


def store(project: Dict[str, Any], cfg: Config, input_per_m: float,
          output_per_m: float, currency: str = "USD") -> Dict[str, Any]:
    prices = dict(project.get("prices") or {})
    prices[key_for(cfg)] = {"in": input_per_m, "out": output_per_m,
                            "currency": currency.upper()}
    updated = dict(project)
    updated["prices"] = prices
    return updated


def describe(rate: Optional[Rate], tokens_in: int, tokens_out: int) -> str:
    if rate is None:
        return ""
    return rate.format(tokens_in, tokens_out)
