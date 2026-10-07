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
    "AB SICAV I – Sustainable US Thematic Portfolio Class A SGDH Acc": {"code": "F00000ME01", "currency": "SGD"},
    "AB SICAV I-Sustainable Global Thematic Portfolio A": {"code": "F0GBR04I90", "currency": "USD"},
    "AB SICAV I-Sustainable Global Thematic Portfolio A SGD Hedged": {"code": "F00000MDDS", "currency": "SGD"},
    "abrdn Pacific Equity Fund - SGD": {"code": "F0HKG062IY", "currency": "SGD"},
    "AB SICAV I - Emerging Markets Multi-Asset Portfolio AD USD Inc": {"code": "F00000PQ4C", "currency": "USD"},
    "Franklin Income Fund A(Mdis)USD": {"code": "F0GBR04ARS", "currency": "USD"},
    "PIMCO GIS Income Fund Administrative USD Income": {"code": "F00000P78B", "currency": "USD"},
    "BlackRock Global Funds - Global Allocation Fund A2": {"code": "F0GBR04AMK", "currency": "USD"},
    "HSBC Life Fortress Fund A": {"code": "F0HKG0705R", "currency": "SGD"},
    "AB FCP I - Emerging Markets Debt Portfolio A2 SGD H Acc": {"code": "F00000ME1O", "currency": "SGD"},
    "Allianz Global Investors Fund - Allianz China A Shares AT USD": {"code": "F000014B63", "currency": "USD"},
    "AB FCP I - Global High Yield Portfolio A2 SGD H Acc": {"code": "F00000ME1H", "currency": "SGD"},
    "Franklin U.S. Opportunities Fund A(acc)SGD-H1": {"code": "F00000N27C", "currency": "SGD"},
    "AXA World Funds - Global Inflation Bonds A Capitalisation SGD (Hedged)": {"code": "F00001080S", "currency": "SGD"},
    "Architas Multi-Asset Balanced Retail Class R (SGD) Unhedged Units Accumulation": {"code": "F0000106MO", "currency": "SGD"},
    "AB FCP I - American Income Portfolio AT SGD H Inc": {"code": "F00000ME1E", "currency": "SGD"},
    "AB FCP I - American Income Portfolio AT Inc": {"code": "F0GBR05XC9", "currency": "USD"},
    "AB SICAV I - International Health Care Portfolio A Acc": {"code": "F0GBR04I8Q", "currency": "USD"},
    "AB SICAV I - International Health Care Portfolio Class A SGD Shares": {"code": "F00001DC8S", "currency": "SGD"},
    "AB SICAV I - Low Volatility Equity Portfolio A USD Acc": {"code": "F00000PA64", "currency": "USD"},
    "United Singapore Bond Fund – Class A SGD Acc": {"code": "F0HKG062UI", "currency": "SGD"},
    "United SGD Fund - Class A SGD Acc": {"code": "F0HKG062HX", "currency": "SGD"},
    "United Emerging Markets Bond Fund - Class A SGD Dist": {"code": "F0HKG062UC", "currency": "SGD"},
    "United Asian Bond Fund - Class SGD Dist": {"code": "F0HKG062UX", "currency": "SGD"},
    "Templeton Shariah Global Equity Fund A(acc)SGD": {"code": "F00000PRDS", "currency": "SGD"},
    "Templeton Latin America Fund A(acc)SGD": {"code": "F000000L32", "currency": "SGD"},
    "Templeton China Fund A(acc)SGD": {"code": "F000000L2S", "currency": "SGD"},
    "Schroder Singapore Trust USD A Acc": {"code": "F00000YNWG", "currency": "USD"},
    "Schroder Singapore Trust SGD A Dis": {"code": "F0HKG062S4", "currency": "SGD"},
    "Schroder Singapore Trust SGD A Acc": {"code": "F00000YNWF", "currency": "SGD"},
    "Schroder Singapore Fixed Income Fund Class A Acc": {"code": "F000005QI8", "currency": "SGD"},
    "Schroder Multi-Asset Revolution 70": {"code": "F00000MKAW", "currency": "SGD"},
    "Schroder Multi-Asset Revolution 50": {"code": "F00000MKAV", "currency": "SGD"},
    "Schroder Multi-Asset Revolution 30 A SGD Accumulation": {"code": "F00000MKAU", "currency": "SGD"},
    "Schroder International Selection Fund Taiwanese Equity A Accumulation USD": {"code": "F000000OYA", "currency": "USD"},
    "Schroder International Selection Fund QEP Global Quality A Accumulation USD": {"code": "F000000PCL", "currency": "USD"},
    "Schroder International Selection Fund Global Equity Alpha A Accumulation USD": {"code": "F0GBR05ZSJ", "currency": "USD"},
    "Schroder International Selection Fund Global Emerging Market Opportunities A Accumulation USD": {"code": "F0000002PO", "currency": "USD"},
    "JPMorgan Investment Funds - Global Income Fund A (mth) SGD (hedged)": {"code": "F00000PTC5", "currency": "SGD"},
    "JPMorgan Funds - India Fund A (acc) USD": {"code": "F0GBR05VW6", "currency": "USD"},
    "JPMorgan Funds - Greater China Fund A (acc) USD": {"code": "F0GBR05VWV", "currency": "USD"},
    "JPMorgan Funds - ASEAN Equity Fund A (acc) USD": {"code": "F0000040T7", "currency": "USD"},
    "JPMorgan Funds - ASEAN Equity Fund A (acc) SGD": {"code": "F00000JOA6", "currency": "SGD"},
    "Janus Henderson Horizon Pan European Absolute Return Fund A2 HSGD": {"code": "F00000T8E7", "currency": "SGD"},
    "Janus Henderson Horizon Japan Opportunities Fund A2 USD": {"code": "F0GBR04DDC", "currency": "USD"},
    "Janus Henderson Continental European Fund A2 EUR": {"code": "F0GBR05ZSF", "currency": "EUR"},
    "Invesco Funds - Invesco Emerging Markets ex-China Equity Fund A Annual Distribution USD": {"code": "F000010P22", "currency": "USD"},
    "Invesco Funds - Invesco Asia Consumer Demand Fund A Accumulation USD": {"code": "F000000R1D", "currency": "USD"},
    "HSBC Portfolios - World Selection 5 AMFLXHSGD": {"code": "F00001GLNT", "currency": "SGD"},
    "HSBC Portfolios - World Selection 5 ACHSGD": {"code": "F00000TQL3", "currency": "SGD"},
    "HSBC Portfolios - World Selection 5 AC": {"code": "F00000462D", "currency": "USD"},
    "HSBC Portfolios - World Selection 4 ACHSGD": {"code": "F000010VX9", "currency": "SGD"},
    "HSBC Portfolios - World Selection 4 AC": {"code": "F000004627", "currency": "USD"},
    "HSBC Portfolios - World Selection 3 ACHSGD": {"code": "F00000TPRX", "currency": "SGD"},
    "HSBC Portfolios - World Selection 3 AC": {"code": "F000004621", "currency": "USD"},
    "HSBC Portfolios - World Selection 2 ACHSGD": {"code": "F000010VX8", "currency": "SGD"},
    "HSBC Portfolios - World Selection 2 AC": {"code": "F00000461V", "currency": "USD"},
    "HSBC Portfolios - World Selection 1 ACHSGD": {"code": "F00000TQKY", "currency": "SGD"},
    "HSBC Portfolios - World Selection 1 AC": {"code": "F00000461P", "currency": "USD"},
    "HSBC Insurance Singapore Bond Fund": {"code": "F0HKG07088", "currency": "SGD"},
    "HSBC Insurance Premium Balanced Fund": {"code": "F0HKG07087", "currency": "SGD"},
    "HSBC Insurance India Equity Fund USD": {"code": "F00000XQSL", "currency": "USD"},
    "HSBC Insurance India Equity Fund": {"code": "F0HKG0706N", "currency": "SGD"},
    "HSBC Insurance Global Sustainable Equity Portfolio Fund USD": {"code": "F00000XQSP", "currency": "USD"},
    "HSBC Insurance Global Sustainable Equity Portfolio Fund": {"code": "F00000WSIA", "currency": "SGD"},
    "HSBC Insurance Global Multi-Asset Fund": {"code": "F00000WSI8", "currency": "SGD"},
    "HSBC Insurance Global High Income Bond Fund USD": {"code": "F00000XQSR", "currency": "USD"},
    "HSBC Insurance Global High Income Bond Fund": {"code": "F00000WSI5", "currency": "SGD"},
    "HSBC Insurance Global Equity Volatility Focused Fund USD": {"code": "F00000XQSQ", "currency": "USD"},
    "HSBC Insurance Global Equity Volatility Focused Fund": {"code": "F00000WSI3", "currency": "SGD"},
    "HSBC Insurance Global Equity Fund": {"code": "F000002JV7", "currency": "SGD"},
    "HSBC Insurance Global Emerging Markets Equity Fund USD": {"code": "F00000XQSO", "currency": "USD"},
    "HSBC Insurance Global Emerging Markets Equity Fund": {"code": "F00000WSI7", "currency": "SGD"},
    "HSBC Insurance Global Emerging Markets Bond Fund USD": {"code": "F00000XQSN", "currency": "USD"},
    "HSBC Insurance Global Emerging Markets Bond Fund": {"code": "F00000PJ5K", "currency": "SGD"},
    "HSBC Insurance Global Bond Fund": {"code": "F000002JV8", "currency": "SGD"},
    "HSBC Insurance Europe Dynamic Equity Fund USD": {"code": "F00000XQSM", "currency": "USD"},
    "HSBC Insurance Europe Dynamic Equity Fund": {"code": "F00000WSI6", "currency": "SGD"},
    "HSBC Insurance Ethical Global Sukuk Fund": {"code": "F0HKG0708L", "currency": "SGD"},
    "HSBC Insurance Ethical Global Equity Fund": {"code": "F0HKG0708K", "currency": "SGD"},
    "HSBC Insurance Emerging Markets Equity Fund": {"code": "F0HKG0714A", "currency": "SGD"},
    "Franklin Alternative Strategies Fund A(acc)SGD-H1": {"code": "F00000UMRK", "currency": "SGD"},
    "Franklin India Fund A(acc)SGD": {"code": "F00000JTUS", "currency": "SGD"},
    "Franklin Income Fund A(Mdis)SGD-H1": {"code": "F000000L30", "currency": "SGD"},
    "Franklin Global Sukuk Fund A(Mdis)SGD": {"code": "F00000PZA1", "currency": "SGD"},
    "Franklin Biotechnology Discovery Fund A(acc)USD": {"code": "F0GBR04V6U", "currency": "USD"},
    "Franklin Biotechnology Discovery Fund A(acc)SGD": {"code": "F000000L2X", "currency": "SGD"},
    "First Sentier Bridge Fund Class A (H Dist)": {"code": "F0HKG062MY", "currency": "SGD"},
    "Fidelity Funds - Global Financial Services Fund A-Acc-SGD": {"code": "F00000WVHP", "currency": "SGD"},
    "Ascend Asia Trust Global Equity Fund Class A SGD (Hedged)": {"code": "F000017086", "currency": "SGD"},
    "Ascend Asia Trust Global Multi Asset Growth Fund Class A USD": {"code": "F00001E447", "currency": "USD"},
    "Ascend Asia Trust Global Multi Asset Income Fund Class A USD": {"code": "F00001E44C", "currency": "USD"},
    "Capital Group New Perspective Fund (LUX) Bh-SGD": {"code": "F00000WDSF", "currency": "SGD"},
    "Capital Group New Perspective Fund (LUX) B": {"code": "F00000WDS1", "currency": "USD"},
    "Capital Group Global High Income Opportunities (LUX) Bfdmh-SGD": {"code": "F00000ZOBE", "currency": "SGD"},
    "Capital Group Global High Income Opportunities (LUX) Bfdm": {"code": "F00000YYP0", "currency": "USD"},
    "BlackRock Global Funds - World Technology Fund A2": {"code": "F0GBR04AMX", "currency": "USD"},
    "BlackRock Global Funds - World Mining Fund A2 SGD Hedged": {"code": "F000000RN7", "currency": "SGD"},
    "BlackRock Global Funds - World Healthscience Fund A2": {"code": "F0GBR04K8L", "currency": "USD"},
    "BlackRock Global Funds - World Gold Fund A2 SGD Hedged": {"code": "F000002GJV", "currency": "SGD"},
    "BlackRock Global Funds - World Gold Fund A2": {"code": "F0GBR04AR8", "currency": "USD"},
    "BlackRock Global Funds - World Energy Fund A2 SGD Hedged": {"code": "F000002GK3", "currency": "SGD"},
    "BlackRock Global Funds - Latin American Fund A2 SGD Hedged": {"code": "F00000LYEB", "currency": "SGD"},
    "Allianz Global Investors Fund - Allianz Global Artificial Intelligence AT H2 SGD": {"code": "F00000ZVQS", "currency": "SGD"},
    "Allianz Global Investors Fund - Allianz China A Shares AT SGD": {"code": "F000014B62", "currency": "SGD"},
    "abrdn Pacific Equity Fund - USD": {"code": "F0HKG062IZ", "currency": "USD"},
    "HSBC Life Asian Growth": {"code": "F0HKG0705K", "currency": "SGD"},
    "HSBC Life Total Return Multi-Asset Advantage": {"code": "F00000Z3UG", "currency": "SGD"},
    "HSBC Life Singapore Equity Fund": {"code": "F0HKG070RH", "currency": "SGD"},
    "HSBC Life Singapore Bond Fund": {"code": "F00000PGIM", "currency": "SGD"},
    "HSBC Life Singapore Balanced Fund": {"code": "F00000HH52", "currency": "SGD"},
    "HSBC Life Short Duration Bond": {"code": "F00000Z3UK", "currency": "SGD"},
    "HSBC Life Shariah Global Equity": {"code": "F00000Z3UJ", "currency": "SGD"},
    "HSBC Life Pacific Equity Fund": {"code": "F0HKG070RG", "currency": "SGD"},
    "HSBC Life Fortress Fund B": {"code": "F0HKG0705Q", "currency": "SGD"},
    "HSBC Life World Healthscience Fund": {"code": "F00000Z3UI", "currency": "SGD"},
    "HSBC Life India Opportunities Fund": {"code": "F0HKG070J6", "currency": "SGD"},
    "HSBC Life Greater China Fund": {"code": "F0HKG070J5", "currency": "SGD"},
    "HSBC Life Global Secure Fund": {"code": "F0HKG0705N", "currency": "SGD"},
    "HSBC Life Global Perspective Fund": {"code": "F0HKG070RE", "currency": "SGD"},
    "HSBC Life Global High Growth Fund": {"code": "F0HKG0705L", "currency": "SGD"},
    "HSBC Life Global Growth Fund": {"code": "F0HKG0705P", "currency": "SGD"},
    "HSBC Life Global Defensive Fund": {"code": "F0HKG0705M", "currency": "SGD"},
    "HSBC Life Global Balanced Fund": {"code": "F0HKG0705O", "currency": "SGD"},
    "HSBC Life FlexConcept Fund (USD)": {"code": "F0000149WP", "currency": "USD"},
    "HSBC Life Emerging Markets Opportunities Fund": {"code": "F00000OWOO", "currency": "SGD"},
    "HSBC Life Asian Income Fund": {"code": "F00000P9WI", "currency": "SGD"},
    "HSBC Life Asian Balanced": {"code": "F00000HH51", "currency": "SGD"},
    "HSBC Insurance World Selection 5 Fund USD": {"code": "F00000XQSW", "currency": "USD"},
    "HSBC Insurance World Selection 5 Fund": {"code": "F00000HL7A", "currency": "SGD"},
    "HSBC Insurance World Selection 4 Fund": {"code": "F000011FXM", "currency": "SGD"},
    "HSBC Insurance World Selection 4 Fd USD": {"code": "F000011FXO", "currency": "USD"},
    "HSBC Insurance World Selection 3 Fund USD": {"code": "F00000XQSV", "currency": "USD"},
    "HSBC Insurance World Selection 3 Fund": {"code": "F00000HL79", "currency": "SGD"},
    "HSBC Insurance World selection 2 Fund": {"code": "F000011FXL", "currency": "SGD"},
    "HSBC Insurance World Selection 2 Fd USD": {"code": "F000011FXN", "currency": "USD"},
    "HSBC Insurance World Selection 1 Fund USD": {"code": "F00000XQSU", "currency": "USD"},
    "HSBC Insurance World Selection 1 Fund": {"code": "F00000HL78", "currency": "SGD"},
    "HSBC Insurance US Opportunities Equity Fund": {"code": "F00000WSI9", "currency": "SGD"},
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
