"""HSBC Life Singapore ILP fund price lookups.

Talks directly to the same public JSON API the Fund Center website's own
NAV chart uses (found by watching its network requests — there's no
official/documented API, so this can break if HSBC changes it):

    https://fundprices.insurance.hsbc.com.sg/fund-center-api/hsbc/ts/external
        ?code={security_id}&id_type=sec_id&from_date=YYYY-MM-DD
        &currency_id=BAS&to_date=YYYY-MM-DD&start_value=100&ts_type=nav

It's unauthenticated and returns real currency-denominated NAV history,
confirmed to match values read straight off the chart's own hover tooltip.
Weekends/holidays carry forward the last trading value — OriginalDate on
each point is the real as-of date, EndDate is just the requested date.

Resolving a fund *name* (as Nic would type it) to the `code` this API
needs is the unsolved half: HSBC's own fund search is a Typesense-backed
endpoint that requires an API key not exposed anywhere in the public page
source, as far as this was chased down. So instead of a live search, this
module keeps a small directory of funds Nic has already used, seeded with
the four resolved by hand in his first ILP Funds Update report. Anything
not in the directory has to be added once — the bot asks Nic for the
fund's security code + currency (both visible on the fund's own page on
fundprices.insurance.hsbc.com.sg, in the URL / page details) and remembers
it from then on.

Like CLIENT_DIR in policy_workbook.py, the on-disk copy of the directory
(FUND_DIRECTORY_FILE) is just a cache — on Railway it's wiped on every
redeploy, so a fund added today may need to be re-added after the next
deploy. Nic can avoid that by telling Claude the new fund's code so it can
be added permanently to the seed list in this file's source.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("assistant-bot.fund_price")

API_URL = "https://fundprices.insurance.hsbc.com.sg/fund-center-api/hsbc/ts/external"
REQUEST_TIMEOUT = 30

# Seed directory: fund display name -> (security id, currency). Keys are
# matched case-insensitively and by loose substring, so Nic doesn't have to
# type the exact registered name. Extend this list directly whenever a new
# fund's code gets confirmed, so it survives redeploys without Nic having to
# re-teach the bot.
SEED_FUNDS: dict[str, dict] = {
    "BlackRock World Healthscience (USD)": {"code": "F0GBR04K8L", "currency": "USD"},
    "FundSmith Equity Fund Feeder (EUR)": {"code": "F00000O9HI", "currency": "EUR"},
    "Amundi US Equity Fundamental Growth (USD)": {"code": "F000013QY7", "currency": "USD"},
    "Schroder Asian Growth Fund (SGD)": {"code": "F0HKG062RH", "currency": "SGD"},
}

FUND_DIRECTORY_FILE = Path(__file__).resolve().parent / "fund_directory.json"


class FundPriceError(RuntimeError):
    pass


@dataclass
class ResolvedFund:
    display_name: str
    code: str
    currency: str


@dataclass
class FundPricePoint:
    value: float
    as_of: date  # the real trading date the value is from (carried forward over weekends/holidays)


def _normalize(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _load_directory() -> dict[str, dict]:
    directory = dict(SEED_FUNDS)
    if FUND_DIRECTORY_FILE.exists():
        try:
            saved = json.loads(FUND_DIRECTORY_FILE.read_text())
            if isinstance(saved, dict):
                directory.update(saved)
        except Exception:  # noqa: BLE001
            logger.exception("Could not read %s — continuing with the seed directory only", FUND_DIRECTORY_FILE)
    return directory


def resolve_fund(query: str) -> Optional[ResolvedFund]:
    """Looks up a fund by (fuzzy) name against the known directory. Returns
    None if nothing matches closely enough — the caller should then ask Nic
    for the code/currency directly and call remember_fund() with the answer."""
    query = query.strip()
    if not query:
        return None
    directory = _load_directory()
    norm_query = _normalize(query)

    # Exact/substring match first (either direction) - cheap and avoids any
    # surprising fuzzy mismatches for names typed carefully.
    for name, info in directory.items():
        norm_name = _normalize(name)
        if norm_query == norm_name or norm_query in norm_name or norm_name in norm_query:
            return ResolvedFund(display_name=name, code=info["code"], currency=info["currency"])

    # Loose token-overlap fallback - e.g. "blackrock healthscience" should
    # still find "BlackRock World Healthscience (USD)".
    query_tokens = {t for t in query.lower().split() if len(t) > 2}
    best_name, best_score = None, 0
    for name in directory:
        name_tokens = {t for t in name.lower().replace("(", " ").replace(")", " ").split() if len(t) > 2}
        score = len(query_tokens & name_tokens)
        if score > best_score:
            best_name, best_score = name, score
    if best_name and best_score >= max(1, len(query_tokens) - 1):
        info = directory[best_name]
        return ResolvedFund(display_name=best_name, code=info["code"], currency=info["currency"])
    return None


def remember_fund(display_name: str, code: str, currency: str) -> None:
    """Saves a manually-resolved fund to the on-disk directory cache so it
    doesn't have to be re-entered for the rest of this deployment's life.
    Best-effort — if the filesystem isn't writable this just silently no-ops
    and the fund will need re-adding next time."""
    directory = {}
    if FUND_DIRECTORY_FILE.exists():
        try:
            directory = json.loads(FUND_DIRECTORY_FILE.read_text())
        except Exception:  # noqa: BLE001
            directory = {}
    directory[display_name] = {"code": code.strip(), "currency": currency.strip().upper()}
    try:
        FUND_DIRECTORY_FILE.write_text(json.dumps(directory, indent=2))
    except Exception:  # noqa: BLE001
        logger.exception("Could not persist fund directory to %s", FUND_DIRECTORY_FILE)


def fetch_price(code: str, currency: str, target_date: date) -> FundPricePoint:
    """Fetches the NAV for one security as of target_date. Requests a short
    window ending on target_date (rather than that single day) so weekends
    and holidays resolve to the last real trading value, matching what the
    fund's own NAV chart shows when hovered on that date."""
    from_date = target_date - timedelta(days=12)
    params = {
        "code": code,
        "id_type": "sec_id",
        "from_date": from_date.isoformat(),
        "currency_id": "BAS",
        "to_date": target_date.isoformat(),
        "start_value": "100",
        "ts_type": "nav",
    }
    try:
        resp = requests.get(API_URL, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise FundPriceError(f"Couldn't reach HSBC's fund price API for {code}: {exc}") from exc

    try:
        securities = payload["TimeSeries"]["Security"]
        history = securities[0]["HistoryDetail"]
    except (KeyError, IndexError, TypeError) as exc:
        raise FundPriceError(f"Unexpected response shape from HSBC for {code}: {payload}") from exc

    if not history:
        raise FundPriceError(
            f"No price history returned for {code} in the window ending {target_date:%d/%m/%Y}."
        )

    latest = history[-1]
    try:
        value = float(latest["Value"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FundPriceError(f"Couldn't parse a NAV value for {code}: {latest}") from exc

    as_of_raw = latest.get("OriginalDate") or latest.get("EndDate")
    as_of = target_date
    if as_of_raw:
        try:
            as_of = date.fromisoformat(str(as_of_raw)[:10])
        except ValueError:
            pass

    return FundPricePoint(value=value, as_of=as_of)


def fetch_prices(code: str, currency: str, target_dates: list[date]) -> dict[date, FundPricePoint]:
    """Convenience wrapper for fetching several dates (e.g. commencement,
    1st anniversary, current) for the same fund in one go."""
    return {d: fetch_price(code, currency, d) for d in target_dates}
