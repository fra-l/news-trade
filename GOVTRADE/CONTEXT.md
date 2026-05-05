# gov-trade: Domain Context

This file gives Claude Code the background knowledge needed to make good
decisions about this pipeline. Read it before starting `TASKS.md`.

---

## Why Government Contract Data Is a Signal

US federal agencies are legally required to publish contract awards above $10,000
on USASpending.gov, typically within a few days of signing. This creates a
window where:

1. The contract is public record
2. The stock market has not yet priced it in — especially for smaller companies
3. No analyst has written about it yet

For a company with $200M annual revenue that just won a $50M DoD IT contract,
that is a 25% revenue uplift. This is material. The market will eventually
price it in. The question is whether we act before or after.

The edge is strongest when:
- The company is small or mid-cap (under $5B market cap)
- The award is large relative to revenue or market cap
- The awarding agency is new for this company (new customer relationship)
- The contract is multi-year (recurring revenue implication)

The edge is weakest or nonexistent when:
- The company is a large-cap defense contractor (Lockheed, Raytheon, L3Harris)
  — awards are expected, priced in, heavily covered
- The award is a small task order on an existing contract vehicle
- The company already announced the contract via press release before USASpending
  published it

---

## USASpending.gov API

Base URL: `https://api.usaspending.gov/api/v2/`

Key endpoints:
- `awards/` — search/filter contract awards
- `awards/{award_id}/` — detail for a specific award
- `references/agency/` — agency metadata

Award type codes:
- `A` = BPA Call
- `B` = Purchase Order
- `C` = Delivery Order
- `D` = Definitive Contract

For our purposes, `C` (Delivery Order) and `D` (Definitive Contract) are the
most signal-rich. Delivery Orders on existing IDIQs (Indefinite Delivery
Indefinite Quantity) contracts are interesting for expansions but less novel
than new Definitive Contracts.

The API is free, unauthenticated, and rate-limited generously. No API key
is needed. Data is typically 1–3 days behind real-time (agencies have a
publication lag), but this is still ahead of most media coverage.

---

## Entity Resolution: Why It Is Hard

USASpending uses the recipient's legal entity name, not their stock ticker or
even their common name. Examples:

| USASpending name | Actual ticker |
|---|---|
| PALANTIR TECHNOLOGIES INC. | PLTR |
| BOOZ ALLEN HAMILTON HOLDING CORPORATION | BAH |
| LEIDOS, INC. | LDOS |
| SCIENCE APPLICATIONS INTERNATIONAL CORP | SAIC |
| CACI INTERNATIONAL INC | CACI |
| PERSPECTA INC. | PRSP (acquired — now part of PERATON, private) |
| PARSONS CORPORATION | PSN |

Complicating factors:
- Subsidiaries award contracts under subsidiary names, not parent ticker
  (e.g., "BOEING DEFENSE, SPACE & SECURITY" → BA)
- Companies get acquired and names change
- Some recipients are private (no ticker exists) — these must be discarded
- Typos and inconsistent formatting in the data source

The resolution service should be conservative: it is better to discard an
award than to trade on a wrong ticker.

---

## Materiality: How to Think About It

A useful mental model: would a sell-side analyst write a note about this?

Rough thresholds (use these to calibrate the heuristic provider):

| Award value as % of market cap | Materiality |
|---|---|
| < 0.5% | Negligible — ignore |
| 0.5% – 2% | Low — monitor only |
| 2% – 10% | Medium — weak signal |
| 10% – 25% | High — strong signal |
| > 25% | Very high — very strong signal |

Agency quality also matters. A DoD contract for an IT services company is
more signal-rich than an HHS contract for the same company — unless the
company's primary business is healthcare IT, in which case HHS is core.

The LLM prompt should ask the model to consider both dimensions, not just
the dollar amount.

---

## Risk Considerations

Government contractors often have lumpy revenue — big contracts followed by
quiet periods. This means:
- A single large award does not mean sustained growth
- Execution risk is real (contracts can be protested, modified, or cancelled)
- The market may already know about an upcoming award from RFP filings

The `RiskManagerAgent` (reused from `news-trade`) will apply position limits
and drawdown checks, but the `MaterialityScorerAgent` should also flag
obvious red flags in its output (e.g., award is a recompete where the
company was the incumbent — less surprising).

---

## Relevant Sectors

Government contracting is concentrated in:

1. **Defense IT & C4ISR** — Leidos, SAIC, Booz Allen, CACI, Peraton
2. **Aerospace & weapons systems** — Lockheed, Raytheon, Northrop, Boeing, L3Harris
3. **Health IT** — Maximus, Evolent Health, ICF International
4. **Professional services** — Accenture Federal, Deloitte (private)
5. **Construction/engineering** — AECOM, Parsons, Jacobs Engineering
6. **Cybersecurity** — Palantir, ManTech (private), KEYW (acquired)

For Phase 1, focus on sectors 1, 3, and 5 — these have the most publicly
traded small/mid-cap players with meaningful signal opportunities.

---

## Academic Evidence

The following findings from the literature should inform design decisions:

- Contract awards for small-cap defense/IT companies show statistically
  significant abnormal returns of 1–3% in the 2–5 days post-award
  (Dhaliwal et al., 2016, *Journal of Accounting Research*)
- The signal is stronger for new contract vehicles than task orders on
  existing IDIQs
- The alpha window is typically 4–72 hours — speed of entity resolution
  and materiality scoring matters
- Crowding risk: if this signal becomes widely known, it decays. Log
  signal-to-price-move latency from day one to track edge degradation

---

## What Success Looks Like After Paper Trading

Run paper trading for at minimum 60 days before drawing conclusions.
Measure:

- **Win rate** — % of trades where price moved in predicted direction within 5 days
- **Average return per signal** — net of simulated spread and commission
- **Signal-to-move latency** — hours between award publication and price move
- **Resolution accuracy** — % of entity resolutions that were correct
  (validate by manually checking a sample)
- **False positive rate** — awards that generated signals but had no price
  movement (likely already-known awards)

A Sharpe ratio > 1.0 on paper trading over 60 days is the bar to clear
before considering any live capital.
