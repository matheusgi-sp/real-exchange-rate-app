# Real Exchange Rate Explorer

A Streamlit app that computes and plots the real exchange rate (RER) between any two of ~60 economies using monthly IMF data, with the real rate normalized to its average over a user-selected window.

## Formulas

- **FX_i(t)**: IMF local-currency units per USD (period average) for country i. For the United States, FX = 1 every month.
- **Nominal rate**: `S(t) = FX_2(t) / FX_1(t)` = units of Country 2 currency per 1 unit of Country 1 currency.
  Example: Country 1 = United Kingdom, Country 2 = United States gives USD per GBP (about 1.2–1.35 recently).
- **Real exchange rate**: `RER(t) = S(t) × P1(t) / P2(t)`, where P1 and P2 are the monthly CPI indices.
- **Benchmark**: `RER*` = arithmetic mean of RER(t) over the valid months in the selected window. It is recomputed whenever countries or dates change.
- **Real Index**: `Real Index(t) = 100 × RER(t) / RER*`. By construction it averages exactly 100 over the window.

A Real Index above 100 means Country 1's currency is overvalued relative to its sample average (expected to depreciate). Below 100 means it is undervalued.

"Valid months" are months where S, P1 and P2 all exist and are positive. Gaps are dropped. Nothing is interpolated or forward-filled.

## Data source

IMF SDMX 3.0 API (no key required): `https://api.imf.org/external/sdmx/3.0`

Query format: `{BASE}/data/dataflow/IMF.STA/{DATAFLOW}/+/{KEY}?c[TIME_PERIOD]=ge:1976-01&dimensionAtObservation=TIME_PERIOD&attributes=dsd&measures=all`

| Series | Dataflow | Key |
|---|---|---|
| CPI index, monthly | `CPI` | `{ISO3}.CPI._T.IX.M` |
| LCU per USD, period average, monthly | `ER` | `{ISO3}.XDC_USD.PA_RT.M` |

- The Euro Area uses code `G163`.
- If `XDC_USD` is empty for a country, the app tries `USD_XDC` and inverts it. The coverage table shows which key was used.
- Responses are requested as SDMX-JSON and cached for 24 hours.
- Optional: if the IMF starts requiring a subscription key, add `IMF_SUBSCRIPTION_KEY = "..."` as a root-level secret in Streamlit Cloud.

## How to use

1. Pick Country 1 and Country 2 in the sidebar.
2. Pick start and end months. Only months with complete data for both countries are offered.
3. Choose a chart mode:
   - **Real**: the Real Index with a reference line at 100.
   - **Nominal**: S(t) in actual currency units.
   - **Both**: Real Index on the left axis and S(t) on the right axis.
4. Read the metrics: RER*, number of valid months, first and last valid month, and the mean Real Index (always 100.00).
5. Download the CSV of all valid observations in the window. It contains `date, FX1_LCU_per_USD, FX2_LCU_per_USD, S, P1, P2, RER, RER_star, Real_Index` at full precision.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy

Push to a public GitHub repo. In Streamlit Community Cloud, create an app pointing to `app.py` and choose Python 3.12.

## Limitations

- **Coverage varies.** Many countries start well after 1976 or have gaps. Months with gaps are dropped, so the window can be shorter than expected.
- **API availability.** The IMF API can be slow, rate-limited, temporarily down, or restructured (for example, through dataflow version changes). The app always asks for the latest dataflow version (`+`) and shows clear errors, but it cannot work without the API.
- **Index base.** The RER level depends on the CPI base years, so only its movements and the Real Index are meaningful. The level of RER* itself is not.
- **Currency history.** Redenominations, euro adoption, and pegs follow the IMF's own conversions. Check them for countries such as Argentina, Brazil, Turkey, and the euro-area members before 1999.
- **Interpretation.** The sample average is a crude equilibrium benchmark. Persistent deviations, for example from Balassa–Samuelson effects or terms-of-trade shifts, are common.
