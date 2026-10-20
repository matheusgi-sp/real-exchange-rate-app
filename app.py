"""
Real Exchange Rate Explorer
Computes and plots the real exchange rate (RER) between two countries using
IMF SDMX 3.0 data (CPI and ER dataflows). Built for Streamlit Community Cloud.
"""
from __future__ import annotations

import math
import os
import re
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.subplots import make_subplots

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
BASE_URL = "https://api.imf.org/external/sdmx/3.0"
AGENCY = "IMF.STA"
CPI_FLOW = "CPI"
ER_FLOW = "ER"
START_PERIOD = "1976-01"
START_DATE = pd.Timestamp("1976-01-01")
TIMEOUT = (10, 60)  # (connect, read) seconds
CACHE_TTL = 24 * 60 * 60  # 24 hours

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
ACCEPT = (
    "application/vnd.sdmx.data+json;version=2.0.0, "
    "application/vnd.sdmx.data+json, application/json;q=0.9"
)

COUNTRIES: dict[str, str] = {
    "Argentina": "ARG",
    "Australia": "AUS",
    "Austria": "AUT",
    "Bangladesh": "BGD",
    "Belgium": "BEL",
    "Bolivia": "BOL",
    "Brazil": "BRA",
    "Canada": "CAN",
    "Chile": "CHL",
    "China": "CHN",
    "Colombia": "COL",
    "Costa Rica": "CRI",
    "Czech Republic": "CZE",
    "Denmark": "DNK",
    "Egypt": "EGY",
    "Euro Area": "G163",
    "Finland": "FIN",
    "France": "FRA",
    "Germany": "DEU",
    "Ghana": "GHA",
    "Greece": "GRC",
    "Hong Kong SAR": "HKG",
    "Hungary": "HUN",
    "Iceland": "ISL",
    "India": "IND",
    "Indonesia": "IDN",
    "Ireland": "IRL",
    "Israel": "ISR",
    "Italy": "ITA",
    "Japan": "JPN",
    "Kazakhstan": "KAZ",
    "Kenya": "KEN",
    "Korea": "KOR",
    "Malaysia": "MYS",
    "Mexico": "MEX",
    "Morocco": "MAR",
    "Netherlands": "NLD",
    "New Zealand": "NZL",
    "Nigeria": "NGA",
    "Norway": "NOR",
    "Pakistan": "PAK",
    "Paraguay": "PRY",
    "Peru": "PER",
    "Philippines": "PHL",
    "Poland": "POL",
    "Portugal": "PRT",
    "Qatar": "QAT",
    "Romania": "ROU",
    "Russia": "RUS",
    "Saudi Arabia": "SAU",
    "Singapore": "SGP",
    "South Africa": "ZAF",
    "Spain": "ESP",
    "Sri Lanka": "LKA",
    "Sweden": "SWE",
    "Switzerland": "CHE",
    "Thailand": "THA",
    "Turkey": "TUR",
    "Ukraine": "UKR",
    "United Arab Emirates": "ARE",
    "United Kingdom": "GBR",
    "United States": "USA",
    "Uruguay": "URY",
    "Vietnam": "VNM",
}
COUNTRY_NAMES = sorted(COUNTRIES)

CAPTION = (
    "Real Index above 100 means Country 1's currency is overvalued relative to its "
    "sample average (expected to depreciate); below 100 means undervalued."
)

CSV_COLUMNS = [
    "date",
    "FX1_LCU_per_USD",
    "FX2_LCU_per_USD",
    "S",
    "P1",
    "P2",
    "RER",
    "RER_star",
    "Real_Index",
]


class IMFDataError(Exception):
    """Raised when the IMF API cannot be reached or returns unusable content."""


# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------
_PERIOD_PATTERNS = (
    re.compile(r"^(\d{4})-?M(\d{1,2})$", re.IGNORECASE),  # 2024-M03, 2024M03
    re.compile(r"^(\d{4})-(\d{1,2})(?:-\d{1,2})?(?:T.*)?$"),  # 2024-03, 2024-03-01
)


def period_to_month_start(period: Any) -> pd.Timestamp | None:
    """Convert an SDMX monthly period ('2024-M03', '2024-03', ...) to a month-start date."""
    if period is None:
        return None
    text = str(period).strip()
    for pattern in _PERIOD_PATTERNS:
        match = pattern.match(text)
        if match:
            year, month = int(match.group(1)), int(match.group(2))
            if 1 <= month <= 12:
                return pd.Timestamp(year=year, month=month, day=1)
            return None
    return None


def _component_value(value: Any) -> Any:
    """Return the code of a structure component value (dict or plain value)."""
    if isinstance(value, dict):
        for key in ("id", "value", "name"):
            if value.get(key) not in (None, ""):
                return value[key]
        return None
    return value


def _to_float(raw: Any) -> float | None:
    """Observation arrays carry OBS_VALUE first; return it as a finite float or None."""
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if isinstance(raw, dict):
        raw = raw.get("value")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _empty_series() -> pd.Series:
    s = pd.Series(dtype="float64")
    s.index = pd.DatetimeIndex([], name="date")
    return s


def parse_sdmx_json(payload: Any) -> pd.Series:
    """
    Parse an SDMX-JSON data message into a monthly pd.Series indexed by month-start
    dates. Observation indices are mapped to TIME_PERIOD values via the structure
    section. If several series come back, the longest one is kept.
    """
    if not isinstance(payload, dict):
        raise IMFDataError("Unexpected response format from the IMF API.")

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    structures = data.get("structures") or payload.get("structures")
    if not structures:
        single = data.get("structure") or payload.get("structure")
        structures = [single] if single else []
    datasets = data.get("dataSets") or payload.get("dataSets") or []

    if not datasets:
        return _empty_series()
    if not structures:
        raise IMFDataError("IMF response has no structure section; cannot map time periods.")

    candidates: dict[str, dict[pd.Timestamp, float]] = {}

    for ds_pos, dataset in enumerate(datasets):
        if not isinstance(dataset, dict):
            continue
        s_ref = dataset.get("structure", 0)
        if isinstance(s_ref, int) and 0 <= s_ref < len(structures):
            struct = structures[s_ref]
        else:
            struct = structures[0]
        dims = (struct or {}).get("dimensions") or {}
        series_dims = dims.get("series") or []
        obs_dims = dims.get("observation") or []

        time_loc = None  # (where, position, list_of_period_codes)
        for where, dim_list in (("obs", obs_dims), ("series", series_dims)):
            for pos, dim in enumerate(dim_list):
                if isinstance(dim, dict) and dim.get("id") == "TIME_PERIOD":
                    values = [_component_value(v) for v in (dim.get("values") or [])]
                    time_loc = (where, pos, values)
                    break
            if time_loc:
                break
        if time_loc is None:
            raise IMFDataError("IMF response has no TIME_PERIOD dimension.")
        where, tpos, tvals = time_loc

        def add(series_id: str, idx: int, raw: Any) -> None:
            if not 0 <= idx < len(tvals):
                return
            dt = period_to_month_start(tvals[idx])
            val = _to_float(raw)
            if dt is None or val is None:
                return
            candidates.setdefault(series_id, {})[dt] = val

        # Layout 1: series -> observations (dimensionAtObservation=TIME_PERIOD)
        series = dataset.get("series")
        if isinstance(series, dict):
            for skey, sobj in series.items():
                observations = (sobj or {}).get("observations") or {}
                sparts = str(skey).split(":") if str(skey) else []
                for okey, oval in observations.items():
                    oparts = str(okey).split(":")
                    try:
                        idx = int(oparts[tpos]) if where == "obs" else int(sparts[tpos])
                    except (ValueError, IndexError):
                        continue
                    add(f"{ds_pos}|{skey}", idx, oval)

        # Layout 2: flat observations (dimensionAtObservation=AllDimensions)
        flat = dataset.get("observations")
        if isinstance(flat, dict) and where == "obs":
            for okey, oval in flat.items():
                parts = str(okey).split(":")
                try:
                    idx = int(parts[tpos])
                except (ValueError, IndexError):
                    continue
                sid = ":".join(p for i, p in enumerate(parts) if i != tpos)
                add(f"{ds_pos}|flat|{sid}", idx, oval)

    if not candidates:
        return _empty_series()

    best = max(candidates.values(), key=len)
    s = pd.Series(best, dtype="float64").sort_index()
    s.index = pd.DatetimeIndex(s.index, name="date")
    return s[s.index >= START_DATE]


# ----------------------------------------------------------------------------
# IMF API access (cached)
# ----------------------------------------------------------------------------
def _headers() -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": ACCEPT,
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }
    # Optional: if the IMF ever requires a subscription key, set IMF_SUBSCRIPTION_KEY
    # as a root-level Streamlit secret (exposed as an environment variable).
    key = os.environ.get("IMF_SUBSCRIPTION_KEY")
    if key:
        headers["Ocp-Apim-Subscription-Key"] = key
    return headers


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def fetch_series(flow: str, key: str) -> pd.Series:
    """
    Fetch one monthly series from the IMF SDMX 3.0 data endpoint:
      {BASE}/data/dataflow/IMF.STA/{flow}/+/{key}
    ('+' = latest dataflow version). Returns an empty Series if the IMF reports no data.
    Raises IMFDataError if the API cannot be reached or returns unusable content.
    """
    url = f"{BASE_URL}/data/dataflow/{AGENCY}/{flow}/+/{key}"
    common = {"dimensionAtObservation": "TIME_PERIOD", "attributes": "dsd", "measures": "all"}
    attempts = [
        {**common, "c[TIME_PERIOD]": f"ge:{START_PERIOD}"},
        common,
        {},
    ]
    errors: list[str] = []
    got_answer = False

    for params in attempts:
        try:
            resp = requests.get(url, params=params, headers=_headers(), timeout=TIMEOUT)
        except requests.RequestException as exc:
            errors.append(f"network error ({exc.__class__.__name__})")
            continue
        if resp.status_code == 404:  # SDMX "no results found"
            got_answer = True
            continue
        if resp.status_code != 200:
            errors.append(f"HTTP {resp.status_code}")
            continue
        try:
            payload = resp.json()
        except ValueError:
            errors.append("non-JSON response")
            continue
        try:
            series = parse_sdmx_json(payload)
        except IMFDataError as exc:
            errors.append(str(exc))
            continue
        got_answer = True
        if not series.empty:
            return series

    if got_answer:
        return _empty_series()
    detail = "; ".join(errors) if errors else "unknown error"
    raise IMFDataError(f"Could not retrieve {flow} series '{key}' from the IMF API ({detail}).")


def get_cpi(iso3: str) -> tuple[pd.Series, str]:
    key = f"{iso3}.CPI._T.IX.M"
    return fetch_series(CPI_FLOW, key), f"{CPI_FLOW}: {key}"


def get_fx(iso3: str) -> tuple[pd.Series, str]:
    """Local-currency units per USD. USA is fixed at 1.0 (no IMF call)."""
    if iso3 == "USA":
        last = pd.Timestamp.today().normalize().replace(day=1)
        idx = pd.date_range(START_DATE, last, freq="MS", name="date")
        return pd.Series(1.0, index=idx, dtype="float64"), "Fixed at 1.0 (USD per USD)"

    key = f"{iso3}.XDC_USD.PA_RT.M"
    series = fetch_series(ER_FLOW, key)
    if not series.empty:
        return series, f"{ER_FLOW}: {key}"

    # Fallback: some publications only carry USD per local currency; invert it.
    alt_key = f"{iso3}.USD_XDC.PA_RT.M"
    alt = fetch_series(ER_FLOW, alt_key)
    alt = alt[alt > 0]
    if not alt.empty:
        return 1.0 / alt, f"{ER_FLOW}: 1 / {alt_key} (inverted)"
    return series, f"{ER_FLOW}: {key}"


# ----------------------------------------------------------------------------
# Economics
# ----------------------------------------------------------------------------
def build_frame(fx1: pd.Series, fx2: pd.Series, p1: pd.Series, p2: pd.Series) -> pd.DataFrame:
    """Align the four series on common months, drop gaps/non-positive values, compute S and RER."""
    df = pd.concat(
        {"FX1_LCU_per_USD": fx1, "FX2_LCU_per_USD": fx2, "P1": p1, "P2": p2},
        axis=1,
        join="inner",
    )
    df = df.dropna()
    df = df[(df > 0).all(axis=1)]
    df = df[df.index >= START_DATE].sort_index()
    df.index.name = "date"
    # S(t) = Country 2 currency per 1 unit of Country 1 currency
    df["S"] = df["FX2_LCU_per_USD"] / df["FX1_LCU_per_USD"]
    # RER(t) = S(t) * P1(t) / P2(t)
    df["RER"] = df["S"] * df["P1"] / df["P2"]
    return df


# ----------------------------------------------------------------------------
# UI helpers
# ----------------------------------------------------------------------------
def fmt_month(d: pd.Timestamp) -> str:
    return d.strftime("%Y-%m")


def coverage_table(items: list[tuple[str, str, pd.Series, str]]) -> pd.DataFrame:
    rows = []
    for label, country, series, source in items:
        fixed = source.startswith("Fixed")
        if fixed:
            first, last, n = "n/a (fixed)", "n/a (fixed)", "—"
        elif series is None or series.empty:
            first, last, n = "no data", "no data", 0
        else:
            first, last, n = fmt_month(series.index.min()), fmt_month(series.index.max()), len(series)
        rows.append(
            {
                "Series": label,
                "Country": country,
                "Source / key": source,
                "First month": first,
                "Last month": last,
                "Observations": str(n),
            }
        )
    return pd.DataFrame(rows)


def render_coverage(table: pd.DataFrame) -> None:
    st.subheader("Data coverage")
    st.dataframe(table, hide_index=True)


def make_chart(w: pd.DataFrame, mode: str, c1: str, c2: str) -> go.Figure:
    nominal_label = f"{c2} currency per 1 {c1} currency"
    x = w.index
    reference = go.Scatter(
        x=[x[0], x[-1]],
        y=[100, 100],
        mode="lines",
        name="100 = window average",
        line=dict(color="gray", dash="dash", width=1),
        hoverinfo="skip",
    )
    real = go.Scatter(
        x=x,
        y=w["Real_Index"],
        mode="lines",
        name="Real Index",
        line=dict(color="#1f77b4", width=2),
        hovertemplate="Real Index: %{y:.2f}<extra></extra>",
    )
    nominal = go.Scatter(
        x=x,
        y=w["S"],
        mode="lines",
        name=f"S(t): {nominal_label}",
        line=dict(color="#d62728", width=2),
        hovertemplate="S: %{y:.6g}<extra></extra>",
    )

    if mode == "Real":
        fig = go.Figure([real, reference])
        fig.update_yaxes(title_text="Real Index (window average = 100)")
        title = f"Real exchange rate index: {c1} vs {c2}"
    elif mode == "Nominal":
        fig = go.Figure([nominal])
        fig.update_yaxes(title_text=nominal_label)
        title = f"Nominal exchange rate S(t): {nominal_label}"
    else:
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_trace(real, secondary_y=False)
        fig.add_trace(reference, secondary_y=False)
        fig.add_trace(nominal, secondary_y=True)
        fig.update_yaxes(title_text="Real Index (window average = 100)", secondary_y=False)
        fig.update_yaxes(title_text=nominal_label, secondary_y=True, showgrid=False)
        title = f"Real Index and nominal S(t): {c1} vs {c2}"

    fig.update_xaxes(title_text="Month")
    fig.update_layout(
        title=title,
        height=540,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="top", y=-0.18, xanchor="left", x=0),
        margin=dict(l=60, r=60, t=60, b=60),
    )
    return fig


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------
def main() -> None:
    st.set_page_config(page_title="Real Exchange Rate Explorer", page_icon="💱", layout="wide")
    st.title("Real Exchange Rate Explorer")
    st.markdown(
        "**RER(t) = S(t) × P1(t) / P2(t)**, where S(t) is units of Country 2 currency per "
        "1 unit of Country 1 currency and P1, P2 are monthly CPI indices. "
        "Real Index(t) = 100 × RER(t) / RER*, with RER* the mean RER over the selected window. "
        "Data: IMF SDMX 3.0 API (CPI and ER dataflows)."
    )

    with st.sidebar:
        st.header("Settings")
        c1 = st.selectbox("Country 1", COUNTRY_NAMES, index=COUNTRY_NAMES.index("United Kingdom"))
        c2 = st.selectbox("Country 2", COUNTRY_NAMES, index=COUNTRY_NAMES.index("United States"))
        date_box = st.container()
        mode = st.radio("Chart mode", ["Real", "Nominal", "Both"], index=0)

    if c1 == c2:
        st.warning("Country 1 and Country 2 are the same. Please choose two different countries.")
        return

    iso1, iso2 = COUNTRIES[c1], COUNTRIES[c2]

    try:
        with st.spinner("Downloading IMF data (cached for 24 hours)..."):
            fx1, fx1_src = get_fx(iso1)
            fx2, fx2_src = get_fx(iso2)
            p1, p1_src = get_cpi(iso1)
            p2, p2_src = get_cpi(iso2)
    except IMFDataError as exc:
        st.error(f"IMF API problem: {exc} The API may be temporarily unavailable; please try again later.")
        return

    coverage = coverage_table(
        [
            ("FX1 (LCU per USD)", c1, fx1, fx1_src),
            ("FX2 (LCU per USD)", c2, fx2, fx2_src),
            ("P1 (CPI)", c1, p1, p1_src),
            ("P2 (CPI)", c2, p2, p2_src),
        ]
    )

    missing = []
    if fx1.empty:
        missing.append(f"IMF returned no exchange-rate data for {c1}.")
    if fx2.empty:
        missing.append(f"IMF returned no exchange-rate data for {c2}.")
    if p1.empty:
        missing.append(f"IMF returned no CPI data for {c1}.")
    if p2.empty:
        missing.append(f"IMF returned no CPI data for {c2}.")
    if missing:
        for msg in missing:
            st.error(msg)
        render_coverage(coverage)
        return

    df = build_frame(fx1, fx2, p1, p2)
    if df.empty:
        st.warning(
            f"No overlapping months where exchange rates and CPI are available and positive "
            f"for both {c1} and {c2}."
        )
        render_coverage(coverage)
        return

    months = list(df.index)
    with date_box:
        start = st.selectbox(
            "Start month", months, index=0, format_func=fmt_month, key=f"start_{iso1}_{iso2}"
        )
        end = st.selectbox(
            "End month", months, index=len(months) - 1, format_func=fmt_month, key=f"end_{iso1}_{iso2}"
        )
        st.caption(
            f"Overlapping data: {fmt_month(months[0])} to {fmt_month(months[-1])} "
            f"({len(months)} valid months; months with gaps are excluded)."
        )

    if start > end:
        st.warning("Start month is after end month. Please adjust the date range.")
        return

    window = df.loc[start:end].copy()
    if window.empty:
        st.warning("There are no valid months in the selected window.")
        render_coverage(coverage)
        return

    rer_star = float(window["RER"].mean())
    window["RER_star"] = rer_star
    window["Real_Index"] = 100.0 * window["RER"] / rer_star

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("RER*", f"{rer_star:.6g}")
    m2.metric("Valid months", f"{len(window)}")
    m3.metric("First / last valid month", f"{fmt_month(window.index[0])} / {fmt_month(window.index[-1])}")
    m4.metric("Mean Real Index", f"{window['Real_Index'].mean():.2f}")

    st.plotly_chart(make_chart(window, mode, c1, c2))
    st.caption(f"{CAPTION} (Country 1 = {c1}; Country 2 = {c2}.)")

    render_coverage(coverage)

    out = window.reset_index()
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    csv_bytes = out[CSV_COLUMNS].to_csv(index=False).encode("utf-8")  # full float precision
    st.download_button(
        "Download CSV (valid observations in window)",
        data=csv_bytes,
        file_name=f"rer_{iso1}_{iso2}_{fmt_month(window.index[0])}_{fmt_month(window.index[-1])}.csv",
        mime="text/csv",
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # never show a raw traceback
        st.error(
            f"Something went wrong while building the page: {exc.__class__.__name__}: {exc}. "
            "Please try another selection or reload later."
        )
