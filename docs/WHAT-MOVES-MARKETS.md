# What news actually moves markets, and how News247 decides

This is the research behind News247's importance criteria. Four research passes (2026-10-08) cataloged the biggest US stock moves of 2016–2026 and the news behind each one:

- [macro and policy events](research/macro-policy-events.md)
- [AI, tech and company events](research/ai-tech-events.md)
- [where news breaks](research/news-sources.md)
- [hosting and texting](research/hosting-and-imessage.md)

Every rule below came from those cases. The results are measured with `news247 backtest` against **97 real events** and **85 "sounds important but moved nothing" headlines**.

## Result

| Measure | Before the research | Now |
|---|---|---|
| Events caught from their **first** report, before anyone wrote "stocks plunge" | 31% | **94%** (91/97) |
| Events caught at all, including later coverage | 31% | **95%** |
| Noise that would have been texted (false alarms) | 0% | **0%** (0/85) |

A test (`tests/test_scoring.py::test_backtest_against_market_history`) fails the build if recall drops below 90% or false alarms rise above 2%. That stops future tuning from quietly making the system worse.

**The 6 misses, honestly:**
- Altruist's AI tax tool (small private firm; wealth managers fell only because the sector was already jittery)
- Saudi National Bank's "absolutely not" on Credit Suisse
- Trump's "period of transition" interview
- a payroll revision headline
- Treasury buyback sizing
- the Europe travel ban (Mar 2020)

All six score MEDIUM: they appear on the dashboard and console but don't text you.

**Caveat:**
- Many "first report" headlines are reconstructed in wire style because the original flash text is paywalled. These are marked `approx: true` in [history.yaml](../news247/data/history.yaml).
- Some older move sizes come from memory and are tagged as such in the research files.
- The criteria were tuned on these same events, so live precision will be somewhat lower. Use `news247 stats` and your own experience to keep tuning.

## The criteria (ordered by how much they mattered historically)

### 1. Surprise versus expectation beats "big-sounding"
The largest scheduled-data days all had a sizable miss against the forecast:
- CPI 7.7% vs 7.9% expected → S&P **+5.5%** (Nov 10, 2022)
- core CPI +0.6% vs +0.3% → **−4.3%** (Sep 13, 2022)
- payrolls 172K vs 80K → Nasdaq **−4.2%** (Jun 5, 2026)
- a 2-cut dot plot vs an expected 3 → S&P **−3%** (Dec 18, 2024)

When something is already priced in (the Sep 2026 Fed hike, the Feb 2026 Supreme Court tariff ruling), it barely moves stocks, however historic it sounds.

→ **What News247 does:** it reads "actual vs. est./expected/consensus" numbers in headlines and adds up to +30 points depending on the size of the miss (0.2pp is already big for CPI).

### 2. Who said it: authorities that can act on the whole market
The President (Truth Social/X), the Fed, Treasury, the FDIC, foreign governments (China's commerce ministry, PBOC/BOJ) and OPEC can move every stock at once.

→ **What News247 does:** +12 "market-wide authority" whenever one of them is the actor. For example, "FED CUTS RATES TO ZERO" is recognized even in all-caps wire style.

### 3. Action, not talk
"effective immediately", "hereby", "authorized", "ordered", "signs order". The Apr 9, 2025 post ("I have authorized a 90 day PAUSE… effective immediately") produced the S&P's **best day since 2008 (+9.5%)**.

### 4. Reversals produce the biggest *up* days
Examples:
- the tariff pause
- the Geneva truce (+3.3%)
- the Apr 2026 Iran ceasefire (+2.5%, oil −13%)
- the Greenland climb-down
- Hassett's "Powell 100% safe" (+5%)

A reversal of a shock that is already priced in moves more than the original shock did.

→ **What News247 does:** "pause", "90-day", "truce", "slash tariffs", "ceasefire", "deal reached", "won't impose" are all weighted.

### 5. Magnitude: parsed numbers
- tariff % (100% scores more than 25%, which scores more than 10%)
- dollars at stake ($300B, $100B)
- basis points (50bp or more)
- percentage moves ("jump 359%")

→ **What News247 does:** each is detected and adds points.

### 6. Systemic and liquidity words
receivership, closed by regulators, bank run, deposits, systemic risk, backstop, limit down, circuit breaker, carry trade, "downgrades US", "yield curve inverts". Examples: SVB, Signature, Credit Suisse, Mar 2020, the Aug 2024 yen unwind, Fitch 2023.

### 7. Threats to Fed independence
"Mr. Too Late", talk of firing the chair, DOJ subpoenas against the chair. These hit the dollar, gold and long yields first, and stocks when the threat targets the chair. The `fed_independence` theme detects them.

### 8. Energy chokepoints
Hormuz, Gulf tanker attacks, OPEC price wars. Oil moves 7–25% within hours, and stocks follow through oil. Wars that don't threaten supply often see dip-buying (Ukraine day 1, Hamas Oct 2023).

### 9. AI news: who announced it and which industry it threatens
In 2026 an AI lab's product post can wipe out a sector:
- Anthropic's Cowork plugins (Thomson Reuters **−16%**, RELX −14%)
- Claude Code Security (cyber stocks −8 to −11%)
- Anthropic's COBOL post (IBM **−13%**, worst day since 2000)
- OpenAI Presence (HubSpot −13%)
- Google's Project Genie (Unity −24%)

What mattered was a **product that does a named job** (legal review, code security, customer support, game worlds) aimed at **seat or fee-based incumbents**. Generic model updates mattered less, though in 2026's fragile regime even "Introducing Claude Sonnet 4.6" moved software stocks 2–5%.

→ **What News247 does:**
- `ai_disrupts_software`, `ai_tool_disrupts_industry` and `frontier_model_release` map each launch to the exposed tickers.
- Posts on the labs' own channels are texted **instantly** (VIP rule).

### 10. AI-lab money: scoops, not press releases
Reports about AI labs' finances move their public proxies (ORCL, CRWV, NVDA, AMD, AVGO, SoftBank) by 3–8%:
- FT, Oct 8, 2026: OpenAI revenue at ~$50B vs ~$70B assumed. Nasdaq-100 −1.4%.
- WSJ, Apr 28, 2026: OpenAI missed its targets.
- WSJ, Jan 2026: Nvidia's $100B OpenAI deal stalled.
- FT, Dec 2025: Blue Owl pulled out of an Oracle data center.

→ **What News247 does:** `ai_lab_financials`, `ai_capex_retreat` and `chip_competition`, plus scoop markers ("FT says", "told investors", "people familiar").

### 11. Company shocks
- "suspends guidance/dividend" (UNH −22%, INTC −26%)
- "auditor resigns" (SMCI −33%)
- "CEO steps down… suspends outlook"
- trial readouts missing a *stated* target (Novo −20% on 22.7% vs 25%)
- mega-cap earnings (META ±20–25%, NVDA +24%, MSFT −10%)

### 12. Credibility gate
On Apr 7, 2025 a squawk misread a TV interview as a "90-day pause". The S&P swung 8% in 10 minutes before the White House called it fake.

→ **What News247 does:**
- Alerts based on a single squawk/social post are labelled **⚠️ UNCONFIRMED**.
- You get a short **✅ CONFIRMED** text as soon as a real outlet or the primary source reports the same story.
- Posts from official accounts that look like crypto/airdrop scams (OpenAI's newsroom account was hijacked in 2024) never get the instant treatment.

## What reliably does *not* move stocks (kept out of your texts)
- Event schedules ("to present at", "announces date for earnings release", "to host call")
- Law-firm class-action ads, listicles ("3 stocks to buy"), "here's why", opinion, podcasts and webinars
- Routine product updates, minor partnerships with no dollar figure, executive hires
- Fed speakers repeating "data dependent"; auction sizes; Beige Book
- Historic-sounding but expected events (an expected hike, a widely anticipated ruling)

Some events matter for oil, gold or the dollar but not stocks. Examples: the Warsh nomination (silver −30%, S&P −0.4%) and Khamenei's death (oil +13%, S&P flat on day 1). News247 still texts those, because you'd want to know.

## Per-category backtest (first-report recall)

| Category | Caught | Category | Caught |
|---|---|---|---|
| AI compute deals | 4/4 | Fed decisions / emergency / leaks / speeches | 9/9 |
| AI disrupts software & search | 10/11 | Fed independence / chair | 3/3 |
| AI-lab financials & financing | 4/4 | Tariffs: announcement, escalation, pause, retaliation, threat, deal | 7/7 |
| AI model launches / policy / licensing / demo errors | 5/5 | President's social posts | 6/6 (5/5 posts + 1 Musk feud) |
| Cheap frontier model (DeepSeek) | 1/1 | Data surprises (CPI, jobs) | 5/6 |
| Chip export controls / competition | 2/2 | Bank failures / systemic | 2/3 |
| Mega-cap earnings | 4/4 | Geopolitics & oil shocks / reversals | 8/8 |
| Guidance cuts, auditor exits, probes, outages, trials | 8/8 | Pandemic, elections, currency, downgrades | 8/9 |

Run `news247 backtest -v` to see every event, its first headline, and the exact scoring reasons.
