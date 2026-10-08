# Market-moving macro, policy and systemic news in US stocks, 2016 to Oct 8, 2026

**Research limits.** WebFetch is egress-blocked (I tried it once on Wikipedia and got EGRESS_BLOCKED). After about 30 searches I hit the shared limit of 200 WebSearch calls per turn, so 2024 and earlier was not re-checked online this session. Each event therefore has a verification tag:
- **[V]** I confirmed it this session through WebSearch. The URL is cited inline.
- **[M]** It comes from my training knowledge (cutoff around mid-2026) and was not re-checked this session. The figures are widely published and I'm confident in most, but check them against closing data before using them as backtest labels.
- **Headline confidence** has three levels. *verbatim* means a confirmed quote. *near-verbatim* means the wording is from memory and may differ by a word or two. *reconstructed* means a typical wire-style headline written for testing, not the original text.

**Date-alignment trap.** CNBC live-blog URLs usually carry the date of the *evening before* the session they cover. For example, `cnbc.com/2026/06/04/...` covers the June 5 jobs-report selloff, and `/2026/04/07/` covers the April 8 ceasefire rally. Key the backtest on the session date, not the URL date.

---

## 1. Event table

S&P = S&P 500, NDX = Nasdaq Composite. Moves are close-to-close unless marked intraday (ID). "Sched" is Y for scheduled events and N for unscheduled ones. "Surprise" says how the news compared with expectations.

| # | Date (session) | Category | Move | First source | Sched | Surprise | Tag |
|---|---|---|---|---|---|---|---|
| 1 | 2016-06-24 | election (referendum) / sovereign | S&P −3.6%, Dow −611, NDX −4.1%; GBP −8% to a 31-yr low | Vote count overnight; BBC called Leave about 23:40 ET Jun 23 | Y (vote) | Outcome against consensus (markets priced Remain) | M |
| 2 | 2018-02-05 | flash_crash / vol-ETP (Volmageddon) | Dow −1,175 (−4.6%), ID −1,597 about 15:10; S&P −4.1%; VIX +116% to 37.3; XIV ETN terminated | Feb 2 jobs report (wages +2.9% y/y) set it up; the crash came from ETP rebalancing | N | Wage surprise plus a structural squeeze | M |
| 3 | 2018-03-22 | tariff_announcement | Dow −724 (−2.9%), S&P −2.5% | Trump signs Section 301 memo (about $50–60B of China goods) | N | Size larger than expected | M |
| 4 | 2018-10-10 | rates / fed_rhetoric | Dow −832 (−3.2%), S&P −3.3%, NDX −4.1% | Lagged effect of Powell's Oct 3 "a long way from neutral" remark and a jump in yields | N | Hawkish surprise | M |
| 5 | 2018-12-04 | president_social_post (tariff) | Dow −799 (−3.1%) | Trump tweet "I am a Tariff Man"; yield-curve inversion the same day | N | Reversal of the G20 truce mood | M |
| 6 | 2018-12-19 | fed_decision (hawkish) | S&P −1.5% after being up about 1%; Dow −352 | FOMC 14:00 hike; Powell said balance-sheet runoff is on "automatic pilot" | Y | Press-conference tone | M |
| 7 | 2018-12-24 | fed_independence / liquidity scare | Dow −653 (−2.9%), S&P −2.7% | Bloomberg (Dec 21) reports Trump discussed firing Powell; Mnuchin's Dec 23 statement on calls to bank CEOs about liquidity | N | Unforced "liquidity" signal | M |
| 8 | 2018-12-26 | reversal | Dow +1,086 (+4.98%, first 1,000-point gain), S&P +4.96% | Hassett says Powell's job is "100%" safe; holiday rebound | N | Relief | M |
| 9 | 2019-01-04 | fed_pivot + jobs | S&P +3.4%, Dow +747 | Powell at AEA panel: "we will be patient"; jobs +312K | Y (panel) | Dovish pivot | M |
| 10 | 2019-05-13 | tariff_escalation | Dow −617 (−2.4%), S&P −2.4%, NDX −3.4% | China retaliation after Trump's May 5 Sunday tweets raising tariffs from 10% to 25% | N | Breakdown of talks | M |
| 11 | 2019-08-01 | president_social_post (tariff) | S&P went from about +1% to −0.9% within minutes | Trump tweets at about 13:26 ET: 10% on the remaining $300B of China goods | N | Total surprise; came the day after the FOMC | M |
| 12 | 2019-08-05 | currency (yuan through 7) | Dow −767 (−2.9%), S&P −3.0%, NDX −3.5% | PBOC fixing lets CNY pass 7 (about 21:15 ET Aug 4); Treasury names China a currency manipulator that evening | N | Policy weapon via FX | M |
| 13 | 2019-08-14 | yield_curve_inversion | Dow −800 (−3.05%), S&P −2.9% | 2s10s inverts for the first time since 2007 | N | Recession signal | M |
| 14 | 2019-08-23 | president_social_post + retaliation | Dow −623 (−2.4%), S&P −2.6%, NDX −3.0% | China's $75B retaliation, then Trump tweets about 10:57 and 11:59 ET ("hereby ordered") | N | Escalation stacked on escalation | M |
| 15 | 2020-02-24 | pandemic | Dow −1,032 (−3.6%), S&P −3.4% | Weekend outbreak in Italy and Korea | N | Contagion outside China | M |
| 16 | 2020-03-03 | fed_emergency_cut (intermeeting) | S&P −2.8%, Dow −786, despite the cut | Fed statement 10:00 ET: cut 50bp to 1.00–1.25% | N | Emergency read as a panic signal | M |
| 17 | 2020-03-09 | oil_shock + pandemic + circuit_breaker | S&P −7.6%, Dow −2,014 (−7.8%); Level-1 halt about 09:34; WTI −25% | OPEC+ collapse (Mar 6), Saudi price war (Mar 7–8) | N | Supply shock on top of a demand shock | M |
| 18 | 2020-03-11 | pandemic (WHO declaration) | Dow −1,465 (−5.9%), bear market | WHO's Tedros declares a pandemic about 12:30 ET | N | Official designation | M |
| 19 | 2020-03-12 | pandemic / travel ban + circuit_breaker | S&P −9.5%, Dow −2,353 (−10.0%), worst since 1987; Level-1 halt about 09:35 | Trump Oval Office address at 21:00 ET Mar 11 (Europe travel suspension); NY Fed $1.5T repo at noon failed to stop the fall | N | Scope shock | M |
| 20 | 2020-03-13 | govt_emergency_declaration | S&P +9.3%, Dow +1,985 | Trump declares a national emergency in the Rose Garden about 15:30 ET | N | Relief | M |
| 21 | 2020-03-16 | fed_emergency_cut (Sunday) + circuit_breaker | S&P −12.0%, Dow −2,997 (−12.9%), NDX −12.3%; halted at the open; VIX closed 82.69 (record) | Fed Sunday statement about 17:00 ET Mar 15: rates to 0–0.25%, $700B QE; futures limit-down; Trump said the crisis could last "July, August" | N | Sunday bazooka read as panic | M |
| 22 | 2020-03-24 | fiscal + fed (unlimited QE) | Dow +2,113 (+11.4%, best since 1933), S&P +9.4% | Fed 08:00 ET Mar 23 "in the amounts needed" plus progress on the CARES deal (Mar 23 itself closed S&P −2.9%, the low) | N | Policy backstop | M |
| 23 | 2020-04-20 | oil_shock (negative WTI) | WTI May contract −$37.63; S&P −1.8% | Expiry squeeze in futures | N | First-ever negative price | M |
| 24 | 2020-11-09 | pandemic (vaccine) | Dow +835 (+2.95%, ID about +1,600); S&P +1.2% (ID +3.9%); NDX −1.5% (rotation); CCL +39%, ZM −17% | Pfizer/BioNTech press release about 06:45 ET | N | 90% efficacy vs about 50–60% hoped for | M |
| 25 | 2021-11-26 | pandemic (variant) | Dow −905 (−2.5%), S&P −2.3%, WTI −13% | WHO names Omicron; thin holiday session | N | New variant | M |
| 26 | 2022-01-05 | fed_minutes (QT) | NDX −3.3%, S&P −1.9% | December FOMC minutes 14:00 ET signal faster runoff | Y | Hawkish surprise in the minutes | M |
| 27 | 2022-02-24 | war_escalation (reversal) | NDX opened about −3.5% and **closed +3.3%**; S&P +1.5%; Brent above $105 ID | Putin speech about 22:00 ET Feb 23 | N | Priced in, then a reversal | M |
| 28 | 2022-05-05 | fed_decision reversal | Dow −1,063 (−3.1%), S&P −3.6%, NDX −5.0% (after +3% on May 4) | May 4 FOMC: Powell rules out 75bp, then markets doubt it | Y | Next-day repricing | M |
| 29 | 2022-06-10 | cpi_surprise | S&P −2.9%, Dow −880 | BLS 08:30: May CPI 8.6% vs 8.3% expected | Y | +0.3pp miss | M |
| 30 | 2022-06-13 | cpi follow-through + fed leak | S&P −3.9% (bear market confirmed), NDX −4.7%, Dow −876 | WSJ (Timiraos) Jun 13: Fed likely to consider 75bp; Celsius freezes withdrawals | N | Leak of a bigger hike | M |
| 31 | 2022-08-26 | fed_speech (Jackson Hole) | S&P −3.4%, Dow −1,008, NDX −3.9% | Powell 10:00 ET, short hawkish speech | Y | Tone harsher than hoped | M |
| 32 | 2022-09-13 | cpi_surprise | S&P −4.3%, Dow −1,276 (−3.9%), NDX −5.2%; worst since Jun 2020 | BLS 08:30: CPI 8.3% vs 8.1%; core +0.6% m/m vs +0.3% | Y | Core miss | M |
| 33 | 2022-11-10 | cpi_surprise (soft) | S&P +5.54%, NDX +7.35%, Dow +1,201 (+3.7%); 10y −28bp; DXY −2% | BLS 08:30: CPI 7.7% vs 7.9%; core +0.3% vs +0.5% | Y | Downside miss | M |
| 34 | 2023-03-09/10 | bank_failure (SVB) | SIVB −60% Mar 9; S&P −1.8% / −1.4%; KRE −8% | SVB 8-K about 16:30 ET Mar 8 ($1.8B loss, $2.25B raise); FDIC receivership about late morning Mar 10 | N | Run on deposits | M |
| 35 | 2023-03-13 | bank_failure (Signature) + systemic backstop | S&P only −0.15%, but FRC −62%, KRE −12%; 2y yield −61bp (largest since 1987) | Joint Treasury/Fed/FDIC statement about 18:15 ET Sun Mar 12; BTFP | N | Contagion offset by backstop | M |
| 36 | 2023-03-15 | bank stress (Credit Suisse) | CS −24%; S&P −0.7%; European banks −7% | Saudi National Bank chair on Bloomberg TV: "absolutely not" (no more capital) | N | Funding refusal | M |
| 37 | 2023-03-20 | forced merger / AT1 wipeout | UBS buys CS for CHF 3B; CHF 16B of AT1 written to zero; S&P +0.9% | Swiss government, SNB and FINMA Sunday evening | N | Resolution | M |
| 38 | 2023-08-02 | sovereign downgrade | S&P −1.4%, NDX −2.2% | Fitch about 17:00 ET Aug 1: US cut to AA+ | N | Moderate | M |
| 39 | 2023-11-14 | cpi_surprise (soft) | S&P +1.9%, Russell 2000 +5.4% | BLS 08:30: CPI 3.2% vs 3.3% | Y | Small miss, large relief | M |
| 40 | 2024-08-02 | jobs_report (recession trigger) | S&P −1.8%, NDX −2.4% (correction) | BLS 08:30: +114K vs about 175K; unemployment 4.3% triggers the Sahm rule | Y | Big miss | M |
| 41 | 2024-08-05 | currency (yen carry unwind) | Nikkei −12.4%; S&P −3.0%, NDX −3.4%, Dow −1,034; VIX ID 65.73 (pre-market), closed 38.6 | BOJ hike Jul 31 plus the Aug 2 US jobs report; overnight Tokyo crash | N | Positioning unwind | M |
| 42 | 2024-09-18 | fed_decision (50bp) | Day: S&P −0.3%. Next day S&P +1.7%, Dow +522 (records) | FOMC 14:00 ET: cut 50bp; Bowman dissents | Y | About a 60/40 coin flip after WSJ/FT reporting | M |
| 43 | 2024-11-06 | election | Dow +1,508 (+3.6%), S&P +2.5%, Russell 2000 +5.8%, TSLA +15% | AP race calls before dawn ET | Y | Clean sweep | M |
| 44 | 2024-12-18 | fed_decision (hawkish cut) | Dow −1,123 (−2.6%, 10th straight loss), S&P −2.95% (about 3%), VIX +74% to 27.62 | FOMC 14:00 ET: cut to 4.25–4.50%; dot plot shows only 2 cuts in 2025; Hammack dissents | Y | Dot plot vs expected 3 cuts | V ([CNBC](https://www.cnbc.com/2024/12/18/fed-rate-decision-december-2024-.html)) |
| 45 | 2025-03-10 | president remarks (recession) | NDX −4.0%, S&P −2.7%, Dow −890; TSLA −15% | Trump on Fox (Sun Mar 9) declines to rule out a recession: "period of transition" | N | Authority admits pain | M |
| 46 | 2025-04-03 | tariff_announcement (Liberation Day) | S&P −4.84% to 5,396.52; Dow −1,679 (−3.98%); NDX about −6%; AAPL −9%, NKE −14% | Rose Garden after the close Apr 2 (about 16:00+ ET): 10% baseline plus reciprocal rates | Y (date known) / N (size) | Rates far above consensus | V ([CNBC](https://www.cnbc.com/2025/04/02/stock-market-today-live-updates-trump-tariffs.html), [Yahoo](https://finance.yahoo.com/news/live/stock-market-today-dow-plunges-1700-points-nasdaq-sp-500-pummeled-in-biggest-rout-since-2020-200415736.html)) |
| 47 | 2025-04-04 | tariff_retaliation | S&P −5.97%, Dow −2,231 (−5.5%) to 38,314.86, NDX −5.82% (bear market); 2-day S&P about −10% | China MOFCOM pre-market: 34% on all US goods from Apr 10; Powell speech at 11:25 ET | N | First major retaliation | V ([CNN](https://www.cnn.com/2025/04/04/investing/stock-market-dow-tariffs), [Investing/Reuters](https://www.investing.com/news/stock-market-news/wall-street-futures-lose-ground-after-china-retaliates-against-us-tariffs-3967354)) |
| 48 | 2025-04-07 | **false headline** (tariff_pause rumor) | S&P swung from −4.7% to +3.4% in about 10 minutes ($2.4T, 10:08–10:18 ET); closed −0.23% | X accounts (Hammer Capital / "Walter Bloomberg") at about 10:11 ET, misreading a Hassett interview on Fox; CNBC banner; Reuters later withdrew its story; White House: "fake news" | N | Rumor | V ([NPR](https://www.npr.org/2025/04/07/nx-s1-5355055/tariffs-markets-x-social-media), [TechCrunch](https://techcrunch.com/2025/04/07/how-one-tweet-wreaked-havoc-on-the-stock-market/), [Fortune](https://www.fortune.com/2025/04/07/trump-tariff-pause-walter-bloomberg-fake-report-debunked-markets)) |
| 49 | 2025-04-09 09:37 | president_social_post ("great time to buy") | Precursor; the rally came at 13:18 | Truth Social | N | — | V time ([Bloomberg](https://www.bloomberg.com/news/articles/2025-04-09/trump-said-wednesday-was-a-great-time-to-buy-he-was-right)) |
| 50 | 2025-04-09 13:18 | tariff_pause | S&P +9.52% (best since 2008), NDX +12.2%, Dow +2,963 (+7.9%); VIX about −36% | Truth Social post | N | Full reversal of policy | V ([Yahoo](https://finance.yahoo.com/news/live/stock-market-today-dow-explodes-3000-points-higher-sp-500-has-best-day-since-2008-as-trump-pauses-most-reciprocal-tariffs-133616395.html), [Forbes](https://www.forbes.com/sites/dereksaul/2025/04/09/stocks-shoot-to-one-of-biggest-gains-everas-trump-announces-90-day-tariff-pause/)) |
| 51 | 2025-04-10 | tariff clarification (China 145%) | S&P −3.5%, NDX −4.3%, Dow −1,015 | White House says China's total rate is 145% | N | Giveback | M |
| 52 | 2025-04-21 | fed_independence (president attacks chair) | Dow −971 (−2.4%), S&P −2.4%, NDX −2.5%; dollar at its lowest since 2022; gold above $3,400 | Truth Social: "Mr. Too Late," "major loser" | N | Threat to the chair | V ([NBC](https://www.nbcnews.com/business/economy/trump-taunts-jerome-powell-waiting-long-cut-rates-rcna202123), [AOL](https://www.aol.com/stocks-slide-trump-escalates-criticism-154900228.html)) |
| 53 | 2025-04-22 | reversal | S&P +2.5% | Bessent "de-escalation" remarks; Trump says he has "no intention" of firing Powell | N | Relief | M |
| 54 | 2025-05-12 | trade_deal / truce (Geneva) | S&P +3.3% to 5,844.19, NDX +4.4%, Dow +2.8% (about +1,160) | US–China joint statement (pre-market ET): US tariff 145% to 30%, China 125% to 10%, 90 days | N | Cut much larger than expected | V ([CBS](https://www.cbsnews.com/news/stocks-up-china-tariffs-pause-agreement-may-12-2025/), [CNN](https://www.cnn.com/2025/05/12/business/us-china-trade-deal-announcement-intl-hnk)) |
| 55 | 2025-06-13 | war_escalation / oil_shock | S&P −1.1%, Dow about −1.8% (about −770), WTI +7% (ID +13–14%) | Israel strikes Iran overnight (about 20:00 ET Jun 12) | N | Direct state-on-state strike | V ([Yahoo](https://finance.yahoo.com/news/live/stock-market-today-dow-sp-500-nasdaq-dive-oil-surges-as-israel-and-iran-trade-strikes-200145242.html), [CNBC](https://www.cnbc.com/2025/06/12/stock-market-today-live-updates.html)) |
| 56 | 2025-06-23/24 | war de-escalation (ceasefire) | Jun 23: WTI −6.8% to about $69, stocks up despite Iran's strike on Al Udeid. Jun 24: rally continues | Iran gives advance warning, then Trump's ceasefire post in the evening | N | "Sell the rumor" in reverse | V ([CNN](https://www.cnn.com/2025/06/23/investing/stock-market-dow-oil-iran)) |
| 57 | 2025-07-16 | fed_independence (report of chair firing) | S&P ID −0.7%, DXY ID −0.8%, then closed S&P +0.3% after denial | Bloomberg/CBS report (official says firing is close), then Trump: "highly unlikely" | N | Rumor plus denial | V ([CNN](https://www.cnn.com/2025/07/16/investing/markets-trump-powell-fed), [CNBC](https://www.cnbc.com/2025/07/16/trump-powell-fed-fire.html)) |
| 58 | 2025-08-01 | jobs_report + statistics-agency firing | S&P about −1.6%, NDX about −2.2% (index figures M); +73K payrolls with −258K revisions (largest two-month cut since Apr 2020) | BLS 08:30; Trump fires BLS chief McEntarfer that afternoon; new tariff rates the same day | Y + N | Revision shock | V for data and firing ([CNBC](https://www.cnbc.com/2025/08/01/trump-erika-mcentarfer-jobs-report-fired.html)); M for index % |
| 59 | 2025-08-22 | fed_speech (dovish Jackson Hole) | S&P +1.5%, Dow +846 (record), Russell 2000 +3.9% | Powell 10:00 ET: "may warrant adjusting our policy stance" | Y | Dovish | M |
| 60 | 2025-10-10 | tariff_escalation (president_social_post) | S&P −2.71%, NDX −3.56%, Dow −879 (−1.9%); worst since April; MCHI −5%; crypto liquidation that evening | Truth Social shortly before 11:00 ET ("massive increase of Tariffs"); 100% tariff post after the close | N | Breaks a calm, record-high market | V ([CNN](https://www.cnn.com/2025/10/10/investing/us-stock-market), [WashTimes/AP](https://www.washingtontimes.com/news/2025/oct/10/wall-street-tumbles-worst-day-since-april-donald-trump-threatens/)) |
| 61 | 2025-10-13 | de-escalation post | S&P about +1.6%, NDX about +2.2% | Sunday Truth Social: "Don't worry about China, it will all be fine!" | N | Reversal | M |
| 62 | 2026-01-12 | fed_independence (DOJ probe of chair) | "Sell America": DXY −0.36%, gold record above $4,600; stocks and Treasuries dipped, then steadied | Powell video statement Sunday night Jan 11 | N | First criminal threat against a sitting chair | V ([CNBC](https://www.cnbc.com/2026/01/12/sell-america-trade-trump-powell-investigation.html), [Bloomberg](https://www.bloomberg.com/news/articles/2026-01-12/powell-says-justice-department-served-fed-with-subpoenas)) |
| 63 | 2026-01-20 | tariff_threat (geopolitical: Greenland) | S&P −2.06% to 6,796.86 (worst since Oct), NDX −2.39%, Dow −871 (−1.76%), VIX +27% to 20.14; dollar and Treasuries also sold | Truth Social over the holiday weekend: 10% on 8 NATO allies from Feb 1, rising to 25% on Jun 1 | N | Tariffs aimed at allies | V ([CNBC](https://www.cnbc.com/2026/01/19/stock-market-today-live-updates.html), [WaPo](https://www.washingtonpost.com/business/2026/01/20/stocks-trump-tariffs-greenland/), [CNN](https://www.cnn.com/2026/01/20/investing/stock-market-us-europe-tensions-greenland)) |
| 64 | 2026-01-21/22 | tariff_pause / framework | Relief rally over both days | Davos speech rules out force; Trump–Rutte "framework"; tariffs dropped | N | Reversal | V qualitative ([Yahoo](https://finance.yahoo.com/news/stock-market-today-jan-21-225305049.html), [Nasdaq](https://www.nasdaq.com/articles/stock-market-today-jan-22-markets-surge-again-today-after-greenland-tariffs-are-dropped)) |
| 65 | 2026-01-30 | fed_chair nomination | Equities mild (S&P −0.4%, ID −1.1%; NDX −0.9%); silver −26% to −31% (record), gold −9%, DXY +0.8% | Trump announces Kevin Warsh | N | Hawkish, pro-independence pick | V ([NBC](https://www.nbcnews.com/business/markets/silver-gold-trump-kevin-warsh-rcna256777), [Fortune](https://fortune.com/2026/01/31/what-happened-gold-silver-dollar-markets-kevin-warsh-fed-reaction)) |
| 66 | 2026-02-20 | court ruling (tariffs struck down) | S&P +0.69%, NDX +0.9%, Dow +231; e-commerce names up | Supreme Court rules IEEPA does not authorize tariffs; 10% Section 122 tariff announced within hours | Semi (decision day unknown) | Largely priced in | V ([Yahoo](https://finance.yahoo.com/news/stock-market-today-feb-20-221129656.html), [Troutman](https://www.troutman.com/insights/supreme-court-strikes-down-ieepa-tariffs-trump-responds-with-section-122-global-surcharge/)) |
| 67 | 2026-03-02 | war_escalation (US/Israel strike Iran; Khamenei killed; Hormuz closed) | Oil Sunday night up to +13%; Brent closed +6.7% at $77.74. S&P +0.04%, Dow −73 (ID −600). XOM/CVX about +4%, defense stocks up | Weekend strikes from Feb 28; Trump announced Khamenei's death | N | Large but partly hedged | V ([CNBC](https://www.cnbc.com/2026/03/01/stock-market-today-live-update.html), [CNN](https://www.cnn.com/2026/03/02/investing/oil-us-stock-market-iran)) |
| 68 | 2026-03-04 | oil_shock | Dow −785 (−1.61%), S&P −0.56%; oil above $80 | War spreading | N | — | V ([CNBC](https://www.cnbc.com/2026/03/04/stock-market-today-live-updates-iran-war.html)) |
| 69 | 2026-03-12 | oil_shock (ship attacks) | WTI +about 10% to about $96; S&P −1.52%, Dow −1.56% | Iran attacks ships in the Gulf; IEA calls it the "largest supply disruption" ever | N | — | V ([Yahoo](https://finance.yahoo.com/news/stock-market-today-march-12-211426220.html)) |
| 70 | 2026-03-26/27 | war / diplomacy failure | Mar 26: S&P −1.74%, NDX −2.38% (correction). Mar 27: Dow in correction at 45,167; Brent $112.57 | Iran rejects US ceasefire proposal sent via Pakistan | N | — | V ([AP via WDBJ](https://www.wdbj7.com/2026/03/26/us-stocks-suffer-worst-day-since-start-iran-conflict-nasdaq-sinks-10-below-its-record/), [CNN](https://www.cnn.com/2026/03/27/investing/us-stocks-iran)) |
| 71 | 2026-04-08 | ceasefire (war de-escalation) | Dow +1,325 (+2.85%, best since Apr 2025), S&P +2.51%, NDX +2.80%; Brent −13.3% to $94.75 (biggest drop since Apr 2020) | Trump announces a 2-week US–Iran ceasefire on the evening of Apr 7 | N | Peace surprise | V ([CNBC](https://www.cnbc.com/2026/04/07/stock-market-today-live-updates.html), [CNN](https://www.cnn.com/2026/04/07/markets/us-stocks-oil-trump-iran-ceasefire)) |
| 72 | 2026-06-05 | jobs_report (hot, so rate-hike fear) | S&P −2.64% to 7,383.74 (worst of 2026), NDX −4.18% (worst since Apr 2025), Dow −695; SOX worst since Mar 2020; NVDA about −6%; December hike odds 42.7% | BLS 08:30: May payrolls +172K vs 80K | Y | 2x consensus | V ([CNN](https://www.cnn.com/2026/06/05/markets/stock-market-sell-off-fed), [CNBC](https://www.cnbc.com/2026/06/05/the-jobs-report-doubled-expectations-why-the-stock-market-doesnt-like-it.html)) |
| 73 | 2026-06-17 | fed_decision (first under Warsh, hawkish hold) | S&P −1.21%, NDX −1.34%, Dow −507; bond yields surged | FOMC 14:00 ET: hold, signals hikes are increasingly likely | Y | Hawkish guidance | V ([CNBC](https://www.cnbc.com/2026/06/16/stock-market-today-live-updates.html)) |
| 74 | 2026-07-29 | fed_decision (hold, 3 hawkish dissents) | Dow about −1,000 (−1.9%), S&P −1%; 30y yield highest since 2007 | FOMC 14:00 ET: hold at 3.50–3.75%; Hammack, Kashkari and Logan dissent for a hike | Y | Dissent count | V ([CNN](https://www.cnn.com/2026/07/29/economy/fed-rate-decision-july), [Yahoo](https://finance.yahoo.com/economy/live/fed-meeting-live-federal-reserve-july-interest-rate-decision-141813444.html)); close figures vary by source |
| 75 | 2026-08-19 | treasury_intervention (buybacks) | 30y −about 10bp from 5.337% (highest since 2007); Dow +about 230 when the news crossed | Treasury announcement: long-end buybacks doubled from $2B to $4B | N | "Bessent put" | V ([CNBC](https://www.cnbc.com/2026/08/19/treasury-announces-upscaled-buyback-operation-for-longer-term-debt-sending-yields-lower.html)) |
| 76 | 2026-09-16 | fed_decision (hike, first since 2023) | Muted or mixed equity reaction; sources disagree on direction | FOMC 14:00 ET: +25bp to 3.75–4.00%, 12–0; 92% priced in | Y | Expected, so little effect | V ([CNBC](https://www.cnbc.com/2026/09/16/fed-rate-decision-september-2026.html), [Chase](https://www.chase.com/personal/investments/learning-and-insights/article/federal-reserve-raises-rates-officials-signal-one-more-hike-in-2026)) |

Other 2026 context I found but did not fully pin down:
- April 29, 2026: Brent +6% to $118.03 on Trump's threat to continue the Hormuz blockade.
- Brent later touched $126, a four-year high (date unclear).
- May 7, 2026: the Court of International Trade struck down the Section 122 tariff, 2–1.
- July 24, 2026: Section 122 lapsed and was replaced by 10%/12.5% Section 301 tariffs on about 60 economies. I found no index reaction ([Gibson Dunn](https://www.gibsondunn.com/section-122-global-tariffs-invalidated-by-the-court-of-international-trade-ruling-and-next-steps/)).
- September 1, 2026: tankers were struck in Hormuz, Brent passed $95, and S&P fell −0.7% ([Yahoo](https://finance.yahoo.com/markets/live/stock-market-today-tuesday-september-1-dow-sp-500-nasdaq-080617884.html)).
- September 9, 2026: a $6B buyback failed to calm yields; 10y 4.853%, 30y 5.309% ([CNBC](https://www.cnbc.com/2026/09/09/treasury-department-to-buy-back-6-billion-in-longer-term-debt-triple-the-normal-level.html)).
- Around October 7, 2026: Dow −500+ and the 30y at a reported 5.35% ([Schaeffer's](https://www.schaeffersresearch.com/content/ezines/2026/10/07/dow-drops-over-500-points-eyes-worst-day-in-3-weeks), unverified).
- CME FedWatch currently shows about 70% odds of a December 2026 hike.

---

## 2. YAML (backtest seed)

`headline_conf` takes the values verbatim, near_verbatim or reconstructed. `verify` is V or M, as defined above.

```yaml
- date: 2016-06-24
  time_et: "23:40 (Jun 23, BBC call)"
  category: election_referendum
  move: "S&P 500 -3.6%, Dow -611, Nasdaq -4.1%; GBP -8% to 31-yr low"
  first_source: "UK count results; BBC projection"
  headlines: ["UK VOTES TO LEAVE EU", "BBC PROJECTS LEAVE WIN IN EU REFERENDUM", "POUND PLUNGES MOST ON RECORD"]
  headline_conf: reconstructed
  tickers: [SPY, QQQ, EWU, FXB]
  scheduled: true
  verify: M
- date: 2018-02-05
  time_et: "15:00-15:10 (intraday collapse)"
  category: flash_crash_vol_etp
  move: "Dow -1,175 (-4.6%), intraday -1,597; S&P -4.1%; VIX +116% to 37.3; XIV terminated"
  first_source: "Price action; Credit Suisse XIV termination notice Feb 6"
  headlines: ["DOW PLUNGES 1,500 POINTS IN MINUTES", "VIX SURGES MOST ON RECORD", "CREDIT SUISSE TO REDEEM XIV ETN AFTER LOSING ALMOST ALL VALUE"]
  headline_conf: reconstructed
  tickers: [SPY, VXX, SVXY, DIA]
  scheduled: false
  verify: M
- date: 2018-03-22
  time_et: "~12:30"
  category: tariff_announcement
  move: "Dow -724 (-2.9%), S&P -2.5%"
  first_source: "White House Section 301 memorandum signing"
  headlines: ["TRUMP SIGNS ORDER TO IMPOSE TARIFFS ON UP TO $60 BILLION OF CHINESE GOODS"]
  headline_conf: reconstructed
  tickers: [SPY, FXI, BA, CAT]
  scheduled: false
  verify: M
- date: 2018-12-04
  time_et: "~10:00 (tweet)"
  category: president_social_post
  move: "Dow -799 (-3.1%), S&P -3.2%"
  first_source: "Trump on Twitter"
  headlines: ["I am a Tariff Man. When people or countries come in to raid the great wealth of our Nation, I want them to pay for the privilege of doing so."]
  headline_conf: near_verbatim
  tickers: [SPY, FXI]
  scheduled: false
  verify: M
- date: 2018-12-19
  time_et: "14:00 / 14:30 presser"
  category: fed_decision_hawkish
  move: "S&P -1.5% (reversed from about +1%), Dow -352"
  first_source: "FOMC statement + Powell press conference"
  headlines: ["FED RAISES RATES TO 2.25%-2.5%, SEES TWO HIKES IN 2019", "POWELL: BALANCE SHEET RUNOFF ON 'AUTOMATIC PILOT'"]
  headline_conf: reconstructed
  tickers: [SPY, TLT]
  scheduled: true
  verify: M
- date: 2018-12-24
  time_et: "pre-market (Mnuchin statement Sun Dec 23)"
  category: liquidity_scare_fed_independence
  move: "Dow -653 (-2.9%), S&P -2.7%"
  first_source: "Mnuchin Twitter statement; Bloomberg report (Dec 21) that Trump discussed firing Powell"
  headlines: ["MNUCHIN CALLS BANK CEOS, SAYS BANKS HAVE AMPLE LIQUIDITY", "TRUMP HAS DISCUSSED FIRING POWELL: BLOOMBERG"]
  headline_conf: reconstructed
  tickers: [SPY, XLF]
  scheduled: false
  verify: M
- date: 2019-01-04
  time_et: "10:15 (AEA panel)"
  category: fed_pivot
  move: "S&P +3.4%, Dow +747"
  first_source: "Powell at AEA panel with Yellen and Bernanke"
  headlines: ["POWELL: FED WILL BE PATIENT AS WE WATCH TO SEE HOW ECONOMY EVOLVES", "POWELL: WOULDN'T HESITATE TO CHANGE BALANCE SHEET POLICY"]
  headline_conf: near_verbatim
  tickers: [SPY, QQQ]
  scheduled: true
  verify: M
- date: 2019-05-13
  time_et: "~11:00 (China MOF); Trump tweets May 5"
  category: tariff_escalation
  move: "Dow -617 (-2.4%), S&P -2.4%, Nasdaq -3.4%"
  first_source: "Trump Twitter (May 5); China Ministry of Finance (May 13)"
  headlines: ["The 10% will go up to 25% on Friday.", "CHINA TO RAISE TARIFFS ON $60 BILLION OF US GOODS"]
  headline_conf: near_verbatim
  tickers: [SPY, FXI, AAPL, CAT]
  scheduled: false
  verify: M
- date: 2019-08-01
  time_et: "13:26"
  category: president_social_post
  move: "S&P from about +1% to -0.9% close"
  first_source: "Trump on Twitter"
  headlines: ["the U.S. will start, on September 1st, putting a small additional Tariff of 10% on the remaining 300 Billion Dollars of goods and products coming from China into our Country."]
  headline_conf: near_verbatim
  tickers: [SPY, FXI, AAPL]
  scheduled: false
  verify: M
- date: 2019-08-05
  time_et: "21:15 Aug 4 (PBOC fix)"
  category: currency_shock
  move: "Dow -767 (-2.9%), S&P -3.0%, Nasdaq -3.5%"
  first_source: "PBOC fixing / CNH market; US Treasury manipulator designation that evening"
  headlines: ["YUAN WEAKENS PAST 7 PER DOLLAR FOR FIRST TIME SINCE 2008", "US TREASURY DESIGNATES CHINA A CURRENCY MANIPULATOR"]
  headline_conf: reconstructed
  tickers: [SPY, FXI, CYB]
  scheduled: false
  verify: M
- date: 2019-08-14
  time_et: "~07:00"
  category: yield_curve_inversion
  move: "Dow -800 (-3.05%), S&P -2.9%"
  first_source: "Treasury market"
  headlines: ["2-YEAR/10-YEAR TREASURY YIELD CURVE INVERTS FOR FIRST TIME SINCE 2007"]
  headline_conf: reconstructed
  tickers: [SPY, TLT, XLF]
  scheduled: false
  verify: M
- date: 2019-08-23
  time_et: "10:57 and 11:59"
  category: president_social_post
  move: "Dow -623 (-2.4%), S&P -2.6%, Nasdaq -3.0%"
  first_source: "Trump on Twitter (after China's $75B retaliation and Powell's Jackson Hole speech)"
  headlines: ["My only question is, who is our bigger enemy, Jay Powell or Chairman Xi?", "Our great American companies are hereby ordered to immediately start looking for an alternative to China"]
  headline_conf: near_verbatim
  tickers: [SPY, QQQ, FXI]
  scheduled: false
  verify: M
- date: 2020-02-24
  time_et: "pre-market"
  category: pandemic
  move: "Dow -1,032 (-3.6%), S&P -3.4%"
  first_source: "Weekend Italy/Korea outbreak reports"
  headlines: ["ITALY LOCKS DOWN TOWNS AS CORONAVIRUS CASES JUMP", "DOW PLUNGES 1,000 POINTS ON CORONAVIRUS FEARS"]
  headline_conf: reconstructed
  tickers: [SPY, QQQ]
  scheduled: false
  verify: M
- date: 2020-03-03
  time_et: "10:00"
  category: fed_emergency_cut
  move: "S&P -2.8%, Dow -786 despite cut"
  first_source: "Federal Reserve press release (intermeeting)"
  headlines: ["The Federal Reserve decided today to lower the target range for the federal funds rate by 1/2 percentage point, to 1 to 1-1/4 percent.", "FED CUTS RATES BY HALF POINT IN EMERGENCY MOVE"]
  headline_conf: near_verbatim
  tickers: [SPY, TLT]
  scheduled: false
  verify: M
- date: 2020-03-09
  time_et: "09:34 (Level 1 halt); OPEC+ collapse Mar 6, Saudi cuts Mar 7-8"
  category: oil_shock_circuit_breaker
  move: "S&P -7.6%, Dow -2,014 (-7.8%); WTI -25%; Level-1 halt"
  first_source: "Saudi Aramco OSP cut; NYSE halt"
  headlines: ["SAUDI ARABIA SLASHES OIL PRICES, LAUNCHES PRICE WAR", "OIL PLUNGES MOST SINCE 1991", "NYSE HALTS TRADING AFTER S&P 500 DROPS 7%"]
  headline_conf: reconstructed
  tickers: [SPY, USO, XLE, XOM]
  scheduled: false
  verify: M
- date: 2020-03-11
  time_et: "~12:30"
  category: pandemic_declaration
  move: "Dow -1,465 (-5.9%) into bear market"
  first_source: "WHO Director-General press briefing"
  headlines: ["WHO DECLARES CORONAVIRUS OUTBREAK A PANDEMIC"]
  headline_conf: reconstructed
  tickers: [SPY]
  scheduled: false
  verify: M
- date: 2020-03-12
  time_et: "21:00 Mar 11 (Oval Office); 09:35 halt"
  category: pandemic_travel_ban_circuit_breaker
  move: "S&P -9.5%, Dow -2,353 (-10%), worst since 1987"
  first_source: "Presidential address; NYSE halt; NY Fed $1.5T repo at noon"
  headlines: ["TRUMP SUSPENDS TRAVEL FROM EUROPE FOR 30 DAYS", "NY FED TO OFFER $1.5 TRILLION IN REPO", "MARKET-WIDE CIRCUIT BREAKER TRIGGERED"]
  headline_conf: reconstructed
  tickers: [SPY, JETS, DAL]
  scheduled: false
  verify: M
- date: 2020-03-16
  time_et: "~17:00 Sun Mar 15 (Fed); 09:30 halt"
  category: fed_emergency_cut_circuit_breaker
  move: "S&P -12.0%, Dow -2,997 (-12.9%), Nasdaq -12.3%; VIX closes 82.69"
  first_source: "Federal Reserve Sunday press release"
  headlines: ["FED CUTS RATES TO ZERO, LAUNCHES $700 BILLION QE PROGRAM", "the Committee decided to lower the target range for the federal funds rate to 0 to 1/4 percent", "S&P FUTURES LIMIT DOWN"]
  headline_conf: near_verbatim
  tickers: [SPY, QQQ, TLT, VIX]
  scheduled: false
  verify: M
- date: 2020-03-24
  time_et: "08:00 Mar 23 (Fed)"
  category: fed_unlimited_qe_fiscal
  move: "Dow +2,113 (+11.4%), S&P +9.4% (Mar 23 itself -2.9%)"
  first_source: "Federal Reserve statement; Senate CARES negotiations"
  headlines: ["FED ANNOUNCES UNLIMITED BOND BUYING 'IN THE AMOUNTS NEEDED'", "FED TO BUY CORPORATE BONDS FOR FIRST TIME"]
  headline_conf: near_verbatim
  tickers: [SPY, LQD, HYG]
  scheduled: false
  verify: M
- date: 2020-04-20
  time_et: "14:30 settlement"
  category: oil_shock
  move: "WTI May -$37.63; S&P -1.8%"
  first_source: "NYMEX settlement"
  headlines: ["US OIL PRICES TURN NEGATIVE FOR FIRST TIME IN HISTORY"]
  headline_conf: reconstructed
  tickers: [USO, XLE]
  scheduled: false
  verify: M
- date: 2020-11-09
  time_et: "06:45"
  category: pandemic_vaccine
  move: "Dow +835 (+2.95%, intraday about +1,600); S&P +1.2%; Nasdaq -1.5%; CCL +39%, ZM -17%"
  first_source: "Pfizer/BioNTech press release"
  headlines: ["Pfizer and BioNTech Announce Vaccine Candidate Against COVID-19 Achieved Success in First Interim Analysis from Phase 3 Study", "PFIZER: COVID VACCINE MORE THAN 90% EFFECTIVE"]
  headline_conf: near_verbatim
  tickers: [PFE, BNTX, SPY, IWM, JETS, CCL, ZM]
  scheduled: false
  verify: M
- date: 2021-11-26
  time_et: "pre-market"
  category: pandemic_variant
  move: "Dow -905 (-2.5%), S&P -2.3%, WTI -13%"
  first_source: "South Africa health officials / WHO designation"
  headlines: ["WHO NAMES NEW COVID VARIANT OMICRON, CALLS IT 'VARIANT OF CONCERN'"]
  headline_conf: reconstructed
  tickers: [SPY, USO, JETS]
  scheduled: false
  verify: M
- date: 2022-02-24
  time_et: "~22:00 Feb 23"
  category: war_escalation
  move: "Nasdaq opened about -3.5%, CLOSED +3.3%; S&P +1.5%; Brent above $105 intraday"
  first_source: "Putin televised address"
  headlines: ["PUTIN ANNOUNCES 'SPECIAL MILITARY OPERATION' IN UKRAINE", "RUSSIA INVADES UKRAINE"]
  headline_conf: reconstructed
  tickers: [SPY, USO, XLE, RSX]
  scheduled: false
  verify: M
- date: 2022-05-05
  time_et: "open (day after FOMC)"
  category: fed_decision_reversal
  move: "Dow -1,063 (-3.1%), S&P -3.6%, Nasdaq -5.0%"
  first_source: "Market repricing of the May 4 FOMC"
  headlines: ["POWELL: 75 BASIS-POINT INCREASE NOT SOMETHING COMMITTEE IS ACTIVELY CONSIDERING"]
  headline_conf: near_verbatim
  tickers: [SPY, QQQ]
  scheduled: true
  verify: M
- date: 2022-06-10
  time_et: "08:30"
  category: cpi_surprise
  move: "S&P -2.9%, Dow -880"
  first_source: "BLS CPI release"
  headlines: ["US MAY CPI RISES 8.6% Y/Y; EST. 8.3%", "INFLATION HITS NEW 40-YEAR HIGH"]
  headline_conf: reconstructed
  tickers: [SPY, QQQ, TLT]
  scheduled: true
  verify: M
- date: 2022-06-13
  time_et: "afternoon (WSJ)"
  category: fed_leak_bear_market
  move: "S&P -3.9% (bear market), Nasdaq -4.7%, Dow -876"
  first_source: "WSJ (Nick Timiraos) report"
  headlines: ["Fed Likely to Consider 75-Basis-Point Rate Rise This Week", "S&P 500 CLOSES IN BEAR MARKET"]
  headline_conf: near_verbatim
  tickers: [SPY, QQQ, TLT]
  scheduled: false
  verify: M
- date: 2022-08-26
  time_et: "10:00"
  category: fed_speech_hawkish
  move: "S&P -3.4%, Dow -1,008, Nasdaq -3.9%"
  first_source: "Powell, Jackson Hole"
  headlines: ["POWELL: REDUCING INFLATION LIKELY TO REQUIRE A SUSTAINED PERIOD OF BELOW-TREND GROWTH", "These are the unfortunate costs of reducing inflation."]
  headline_conf: near_verbatim
  tickers: [SPY, QQQ]
  scheduled: true
  verify: M
- date: 2022-09-13
  time_et: "08:30"
  category: cpi_surprise
  move: "S&P -4.3%, Dow -1,276 (-3.9%), Nasdaq -5.2%"
  first_source: "BLS CPI release"
  headlines: ["US AUG CPI RISES 8.3% Y/Y; EST. 8.1%", "US AUG CORE CPI RISES 0.6% M/M; EST. 0.3%"]
  headline_conf: reconstructed
  tickers: [SPY, QQQ, TLT]
  scheduled: true
  verify: M
- date: 2022-11-10
  time_et: "08:30"
  category: cpi_surprise_soft
  move: "S&P +5.54%, Nasdaq +7.35%, Dow +1,201 (+3.7%); 10y -28bp"
  first_source: "BLS CPI release"
  headlines: ["US OCT CPI RISES 7.7% Y/Y; EST. 7.9%", "US OCT CORE CPI RISES 0.3% M/M; EST. 0.5%"]
  headline_conf: reconstructed
  tickers: [SPY, QQQ, TLT, IWM]
  scheduled: true
  verify: M
- date: 2023-03-10
  time_et: "16:30 Mar 8 (SVB 8-K); late morning Mar 10 (closure)"
  category: bank_failure
  move: "SIVB -60% Mar 9; S&P -1.8% Mar 9 / -1.4% Mar 10; KRE -8%"
  first_source: "SVB investor letter; CA DFPI / FDIC press release"
  headlines: ["SVB FINANCIAL SEEKS TO RAISE $2.25 BILLION AFTER $1.8 BILLION LOSS ON SECURITIES SALE", "SILICON VALLEY BANK CLOSED BY REGULATORS; FDIC NAMED RECEIVER", "SVB SHARES HALTED"]
  headline_conf: reconstructed
  tickers: [SIVB, KRE, XLF, SPY]
  scheduled: false
  verify: M
- date: 2023-03-13
  time_et: "~18:15 Sun Mar 12"
  category: bank_failure_systemic_backstop
  move: "FRC -62%, KRE -12%, S&P -0.15%; 2y -61bp"
  first_source: "Joint Statement by Treasury, Federal Reserve, and FDIC"
  headlines: ["Joint Statement by Treasury, Federal Reserve, and FDIC", "REGULATORS CLOSE SIGNATURE BANK, INVOKE SYSTEMIC RISK EXCEPTION", "FED CREATES BANK TERM FUNDING PROGRAM"]
  headline_conf: near_verbatim
  tickers: [FRC, PACW, WAL, KRE, SPY, TLT]
  scheduled: false
  verify: M
- date: 2023-03-15
  time_et: "~04:00-06:00"
  category: bank_stress
  move: "CS -24%; S&P -0.7%; European banks about -7%"
  first_source: "Saudi National Bank chairman on Bloomberg TV"
  headlines: ["SAUDI NATIONAL BANK SAYS 'ABSOLUTELY NOT' TO MORE CREDIT SUISSE FUNDING", "CREDIT SUISSE SHARES HALTED AFTER RECORD PLUNGE"]
  headline_conf: near_verbatim
  tickers: [CS, UBS, XLF, SPY]
  scheduled: false
  verify: M
- date: 2023-05-01
  time_et: "~03:30"
  category: bank_failure
  move: "S&P -0.04% (resolution expected)"
  first_source: "FDIC press release"
  headlines: ["JPMORGAN TO ACQUIRE FIRST REPUBLIC AFTER FDIC SEIZURE"]
  headline_conf: reconstructed
  tickers: [FRC, JPM, KRE]
  scheduled: false
  verify: M
- date: 2023-08-02
  time_et: "~17:00 Aug 1"
  category: sovereign_downgrade
  move: "S&P -1.4%, Nasdaq -2.2%"
  first_source: "Fitch Ratings release"
  headlines: ["FITCH DOWNGRADES US TO 'AA+' FROM 'AAA'"]
  headline_conf: reconstructed
  tickers: [SPY, TLT]
  scheduled: false
  verify: M
- date: 2024-08-02
  time_et: "08:30"
  category: jobs_report
  move: "S&P -1.8%, Nasdaq -2.4% (correction)"
  first_source: "BLS Employment Situation"
  headlines: ["US JULY NONFARM PAYROLLS +114K; EST. +175K", "US UNEMPLOYMENT RATE RISES TO 4.3%, TRIGGERING SAHM RULE"]
  headline_conf: reconstructed
  tickers: [SPY, QQQ, TLT]
  scheduled: true
  verify: M
- date: 2024-08-05
  time_et: "overnight Tokyo session (~20:00-02:00 ET)"
  category: currency_yen_carry_unwind
  move: "Nikkei -12.4%; S&P -3.0%, Nasdaq -3.4%, Dow -1,034; VIX intraday 65.73"
  first_source: "Tokyo market; BOJ hike Jul 31"
  headlines: ["NIKKEI PLUNGES 12% IN WORST DAY SINCE 1987", "VIX JUMPS ABOVE 65", "YEN CARRY TRADE UNWIND ROILS GLOBAL MARKETS"]
  headline_conf: reconstructed
  tickers: [EWJ, FXY, SPY, QQQ, NVDA]
  scheduled: false
  verify: M
- date: 2024-09-18
  time_et: "14:00"
  category: fed_decision_50bp
  move: "S&P -0.3% on day; +1.7% to record next day"
  first_source: "FOMC statement"
  headlines: ["FED CUTS RATES BY HALF POINT TO 4.75%-5%", "BOWMAN DISSENTS IN FAVOR OF QUARTER-POINT CUT"]
  headline_conf: reconstructed
  tickers: [SPY, IWM, TLT]
  scheduled: true
  verify: M
- date: 2024-11-06
  time_et: "~05:30 (AP call)"
  category: election
  move: "Dow +1,508 (+3.6%), S&P +2.5%, Russell 2000 +5.8%, TSLA +15%"
  first_source: "AP race call"
  headlines: ["AP: TRUMP WINS PRESIDENCY"]
  headline_conf: reconstructed
  tickers: [SPY, IWM, TSLA, XLF, DJT]
  scheduled: true
  verify: M
- date: 2024-12-18
  time_et: "14:00"
  category: fed_decision_hawkish
  move: "Dow -1,123 (-2.6%), S&P -2.95% (~3%), VIX +74% to 27.62"
  first_source: "FOMC statement + SEP dot plot"
  headlines: ["FED CUTS RATES BY QUARTER POINT, SIGNALS ONLY TWO CUTS IN 2025", "FED SEES 2025 PCE INFLATION AT 2.5%"]
  headline_conf: reconstructed
  tickers: [SPY, QQQ, IWM, TLT]
  scheduled: true
  verify: V
- date: 2025-03-10
  time_et: "Sun Mar 9 broadcast"
  category: president_remarks_recession
  move: "Nasdaq -4.0%, S&P -2.7%, Dow -890; TSLA -15%"
  first_source: "Trump interview, Fox 'Sunday Morning Futures'"
  headlines: ["There is a period of transition, because what we're doing is very big.", "TRUMP DOESN'T RULE OUT RECESSION"]
  headline_conf: near_verbatim
  tickers: [QQQ, SPY, TSLA]
  scheduled: false
  verify: M
- date: 2025-04-03
  time_et: "16:00+ Apr 2 (Rose Garden, after close)"
  category: tariff_announcement
  move: "S&P -4.84%, Dow -1,679 (-3.98%), Nasdaq ~-6%; AAPL -9%, NKE -14%"
  first_source: "White House Rose Garden event + tariff chart"
  headlines: ["TRUMP IMPOSES 10% BASELINE TARIFF ON ALL IMPORTS, HIGHER 'RECIPROCAL' RATES", "TRUMP: 34% TARIFF ON CHINA, 20% ON EU", "Tariff 'Obliteration Day' for Wall Street Follows Trump's 'Liberation Day'"]
  headline_conf: reconstructed
  tickers: [SPY, QQQ, AAPL, NKE, FXI]
  scheduled: true
  verify: V
- date: 2025-04-04
  time_et: "~05:00-06:00 (MOFCOM); 11:25 (Powell)"
  category: tariff_retaliation
  move: "S&P -5.97%, Dow -2,231 (-5.5%), Nasdaq -5.82% (bear market)"
  first_source: "China Ministry of Commerce / Customs Tariff Commission"
  headlines: ["CHINA TO IMPOSE 34% TARIFF ON ALL US GOODS FROM APRIL 10", "POWELL: TARIFFS SIGNIFICANTLY LARGER THAN EXPECTED"]
  headline_conf: reconstructed
  tickers: [SPY, QQQ, AAPL, FXI]
  scheduled: false
  verify: V
- date: 2025-04-07
  time_et: "10:11"
  category: false_headline_tariff_pause
  move: "S&P from -4.7% to +3.4% in ~10 min ($2.4T); closed -0.23%"
  first_source: "X accounts (Hammer Capital, 'Walter Bloomberg'); CNBC chyron; Reuters (withdrawn)"
  headlines: ["HASSETT: TRUMP IS CONSIDERING A 90-DAY PAUSE IN TARIFFS FOR ALL COUNTRIES EXCEPT CHINA"]
  headline_conf: verbatim
  tickers: [SPY, QQQ]
  scheduled: false
  verify: V
  label_note: "MOVED the market but was FALSE - test source-credibility gating"
- date: 2025-04-09
  time_et: "09:37"
  category: president_social_post
  move: "Precursor to the +9.5% day"
  first_source: "Truth Social"
  headlines: ["BE COOL! Everything is going to work out well. The USA will be bigger and better than ever before!", "THIS IS A GREAT TIME TO BUY!!! DJT"]
  headline_conf: near_verbatim
  tickers: [SPY, QQQ, DJT]
  scheduled: false
  verify: V
- date: 2025-04-09
  time_et: "13:18"
  category: tariff_pause
  move: "S&P 500 +9.52% (best day since 2008), Nasdaq +12.2%, Dow +2,963 (+7.9%)"
  first_source: "Truth Social post by Trump"
  headlines: ["I have authorized a 90 day PAUSE, and a substantially lowered Reciprocal Tariff during this period, of 10%, also effective immediately.", "I am hereby raising the Tariff charged to China by the United States of America to 125%, effective immediately", "TRUMP PAUSES RECIPROCAL TARIFFS FOR 90 DAYS, RAISES CHINA TO 125%"]
  headline_conf: verbatim
  tickers: [SPY, QQQ, DIA, AAPL, NVDA]
  scheduled: false
  verify: V
- date: 2025-04-21
  time_et: "~10:00 (Truth Social)"
  category: fed_independence
  move: "Dow -971 (-2.4%), S&P -2.4%, Nasdaq -2.5%; DXY at 2022 low; gold above $3,400"
  first_source: "Truth Social"
  headlines: ["'Mr. Too Late,' a major loser", "TRUMP RAMPS UP ATTACKS ON POWELL, CALLS HIM 'MAJOR LOSER'"]
  headline_conf: near_verbatim
  tickers: [SPY, UUP, GLD, TLT]
  scheduled: false
  verify: V
- date: 2025-05-12
  time_et: "~04:00 (Geneva statement)"
  category: trade_deal_truce
  move: "S&P +3.3%, Nasdaq +4.4%, Dow +2.8% (~+1,160)"
  first_source: "US-China joint statement / Bessent-Greer press conference"
  headlines: ["US, CHINA AGREE TO SLASH TARIFFS FOR 90 DAYS", "US TO CUT CHINA TARIFFS TO 30% FROM 145%; CHINA TO 10% FROM 125%"]
  headline_conf: reconstructed
  tickers: [SPY, QQQ, FXI, KWEB, AAPL]
  scheduled: false
  verify: V
- date: 2025-05-23
  time_et: "~07:20"
  category: president_social_post
  move: "S&P -0.7% (intraday ~-1.5%); AAPL -3%"
  first_source: "Truth Social"
  headlines: ["I am recommending a straight 50% Tariff on the European Union, starting on June 1, 2025.", "TRUMP THREATENS 25% TARIFF ON IPHONES NOT MADE IN US"]
  headline_conf: near_verbatim
  tickers: [SPY, AAPL, VGK]
  scheduled: false
  verify: M
- date: 2025-06-13
  time_et: "~20:00 Jun 12"
  category: war_escalation_oil
  move: "S&P -1.1%, Dow ~-1.8% (~-770), WTI +7% (intraday +13%)"
  first_source: "Israeli military announcement"
  headlines: ["ISRAEL LAUNCHES STRIKES ON IRAN NUCLEAR AND MILITARY SITES", "OIL SURGES MOST SINCE 2022"]
  headline_conf: reconstructed
  tickers: [USO, XLE, LMT, NOC, SPY, JETS]
  scheduled: false
  verify: V
- date: 2025-06-23
  time_et: "afternoon (Iran strike); ~18:00 (ceasefire post)"
  category: war_deescalation_ceasefire
  move: "WTI -6.8%; stocks higher; next day further gains"
  first_source: "Truth Social"
  headlines: ["IRAN FIRES MISSILES AT US AL UDEID BASE IN QATAR", "It has been fully agreed by and between Israel and Iran that there will be a Complete and Total CEASEFIRE"]
  headline_conf: near_verbatim
  tickers: [USO, XLE, SPY]
  scheduled: false
  verify: V
- date: 2025-07-16
  time_et: "~10:30-12:30"
  category: fed_independence_rumor
  move: "S&P intraday -0.7%, DXY -0.8%, then S&P closed +0.3% after denial"
  first_source: "Bloomberg/CBS (White House official), then Trump remarks"
  headlines: ["TRUMP LIKELY TO FIRE POWELL SOON, WHITE HOUSE OFFICIAL SAYS", "Trump Says Firing Fed Chair Powell Is 'Highly Unlikely'"]
  headline_conf: near_verbatim
  tickers: [SPY, UUP, TLT, GLD]
  scheduled: false
  verify: V
- date: 2025-08-01
  time_et: "08:30 (BLS); ~13:30 (firing post)"
  category: jobs_report_stats_agency
  move: "S&P ~-1.6%, Nasdaq ~-2.2% (index % from memory, M)"
  first_source: "BLS release; Truth Social"
  headlines: ["US JULY PAYROLLS +73K; PRIOR TWO MONTHS REVISED DOWN 258K", "TRUMP ORDERS FIRING OF BLS COMMISSIONER MCENTARFER"]
  headline_conf: reconstructed
  tickers: [SPY, QQQ, TLT]
  scheduled: true
  verify: V
- date: 2025-08-22
  time_et: "10:00"
  category: fed_speech_dovish
  move: "S&P +1.5%, Dow +846 (record), Russell 2000 +3.9%"
  first_source: "Powell, Jackson Hole"
  headlines: ["POWELL: SHIFTING BALANCE OF RISKS MAY WARRANT ADJUSTING OUR POLICY STANCE"]
  headline_conf: near_verbatim
  tickers: [SPY, IWM]
  scheduled: true
  verify: M
- date: 2025-10-10
  time_et: "~10:57 (first post); after close (100% post)"
  category: tariff_escalation_president_post
  move: "S&P -2.71%, Nasdaq -3.56%, Dow -879 (-1.9%); MCHI -5%"
  first_source: "Truth Social"
  headlines: ["One of the Policies that we are calculating at this moment is a massive increase of Tariffs on Chinese products coming into the United States of America.", "the United States of America will impose a Tariff of 100% on China, over and above any Tariff that they are currently paying, starting November 1st"]
  headline_conf: near_verbatim
  tickers: [SPY, QQQ, SMH, MCHI, MP]
  scheduled: false
  verify: V
- date: 2025-10-13
  time_et: "Sun Oct 12"
  category: deescalation_post
  move: "S&P ~+1.6%, Nasdaq ~+2.2%"
  first_source: "Truth Social"
  headlines: ["Don't worry about China, it will all be fine! Highly respected President Xi just had a bad moment."]
  headline_conf: near_verbatim
  tickers: [SPY, QQQ]
  scheduled: false
  verify: M
- date: 2026-01-12
  time_et: "Sun Jan 11 evening (Powell video)"
  category: fed_independence_doj_probe
  move: "DXY -0.36%, gold record above $4,600; stocks/Treasuries dipped then steadied"
  first_source: "Fed Chair Powell video statement"
  headlines: ["POWELL SAYS DOJ SERVED FED WITH GRAND JURY SUBPOENAS, THREATENING CRIMINAL INDICTMENT", "Jerome Powell Vows to Stand Firm as DOJ Conducts Criminal Investigation"]
  headline_conf: near_verbatim
  tickers: [UUP, GLD, TLT, SPY]
  scheduled: false
  verify: V
- date: 2026-01-20
  time_et: "post over holiday weekend (~Jan 17)"
  category: tariff_threat_geopolitical
  move: "S&P -2.06%, Nasdaq -2.39%, Dow -871 (-1.76%), VIX +27% to 20.14; 'Sell America'"
  first_source: "Truth Social"
  headlines: ["until such time as a Deal is reached for the Complete and Total purchase of Greenland", "TRUMP TO IMPOSE 10% TARIFF ON 8 EUROPEAN NATIONS FROM FEB 1, RISING TO 25% JUNE 1 OVER GREENLAND"]
  headline_conf: near_verbatim
  tickers: [SPY, QQQ, VGK, UUP, GLD, TLT]
  scheduled: false
  verify: V
- date: 2026-01-21
  time_et: "Davos speech / afternoon post"
  category: tariff_pause_framework
  move: "Relief rally Jan 21-22 (S&P ~+1.2% Jan 21, from memory)"
  first_source: "Davos speech; Truth Social after meeting NATO's Rutte"
  headlines: ["TRUMP RULES OUT USING FORCE TO TAKE GREENLAND", "TRUMP SAYS HE WON'T IMPOSE FEB. 1 TARIFFS AFTER 'FRAMEWORK' DEAL ON GREENLAND"]
  headline_conf: reconstructed
  tickers: [SPY, VGK]
  scheduled: false
  verify: V
- date: 2026-01-30
  time_et: "morning announcement"
  category: fed_chair_nomination
  move: "S&P -0.4% (intraday -1.1%), Nasdaq -0.9%; silver -26% to -31% (record), gold ~-9%, DXY +0.8%"
  first_source: "Trump announcement (Truth Social)"
  headlines: ["TRUMP NOMINATES KEVIN WARSH AS FED CHAIR", "SILVER SUFFERS RECORD-BREAKING FALL"]
  headline_conf: reconstructed
  tickers: [SLV, GLD, GDX, UUP, SPY]
  scheduled: false
  verify: V
- date: 2026-02-20
  time_et: "10:00 (opinion release)"
  category: court_ruling_tariffs
  move: "S&P +0.69%, Nasdaq +0.9%, Dow +231; AMZN, ETSY, W up"
  first_source: "US Supreme Court opinion"
  headlines: ["SUPREME COURT STRIKES DOWN TRUMP'S IEEPA TARIFFS", "TRUMP INVOKES SECTION 122, IMPOSES 10% GLOBAL TARIFF"]
  headline_conf: reconstructed
  tickers: [SPY, AMZN, W, ETSY, XRT]
  scheduled: false
  verify: V
- date: 2026-03-02
  time_et: "Sat Feb 28 strikes; Sun evening futures"
  category: war_escalation_oil_shock
  move: "Oil up to +13% Sunday night; Brent +6.7% to $77.74; S&P +0.04%, Dow -73 (intraday -600); XOM/CVX +4%"
  first_source: "US/Israeli strike announcements; Trump on Khamenei's death"
  headlines: ["US AND ISRAEL LAUNCH STRIKES ON IRAN", "TRUMP SAYS IRAN SUPREME LEADER KHAMENEI KILLED", "IRAN CLOSES STRAIT OF HORMUZ"]
  headline_conf: reconstructed
  tickers: [USO, BNO, XLE, XOM, CVX, LMT, NOC, GLD, SPY]
  scheduled: false
  verify: V
- date: 2026-03-12
  time_et: "intraday"
  category: oil_shock
  move: "WTI +~10% to ~$96; S&P -1.52%, Dow -1.56%"
  first_source: "Reports of Iranian attacks on Gulf shipping; IEA comment"
  headlines: ["IRAN ATTACKS SHIPS IN PERSIAN GULF", "IEA: LARGEST OIL SUPPLY DISRUPTION EVER"]
  headline_conf: reconstructed
  tickers: [USO, XLE, SPY]
  scheduled: false
  verify: V
- date: 2026-03-26
  time_et: "intraday"
  category: war_diplomacy_failure
  move: "S&P -1.74%, Nasdaq -2.38% (correction); Brent +4.8% to $101.89"
  first_source: "Iranian statements rejecting US ceasefire proposal (via Pakistan)"
  headlines: ["IRAN DISMISSES US CEASEFIRE PROPOSAL", "NASDAQ CLOSES IN CORRECTION"]
  headline_conf: reconstructed
  tickers: [QQQ, SPY, BNO]
  scheduled: false
  verify: V
- date: 2026-04-08
  time_et: "evening Apr 7 (announcement)"
  category: ceasefire
  move: "Dow +1,325 (+2.85%), S&P +2.51%, Nasdaq +2.80%; Brent -13.3% to $94.75"
  first_source: "Trump announcement"
  headlines: ["TRUMP ANNOUNCES TWO-WEEK CEASEFIRE WITH IRAN", "OIL POSTS BIGGEST DROP SINCE APRIL 2020"]
  headline_conf: reconstructed
  tickers: [SPY, DIA, USO, XLE, JETS]
  scheduled: false
  verify: V
- date: 2026-06-05
  time_et: "08:30"
  category: jobs_report_hot
  move: "S&P -2.64% (worst of 2026), Nasdaq -4.18%, Dow -695; SOX worst since Mar 2020; NVDA ~-6%"
  first_source: "BLS Employment Situation"
  headlines: ["US MAY NONFARM PAYROLLS +172K; EST. +80K", "Nasdaq, S&P 500 suffer worst day of year as AI stocks tumble and Fed rate-hike odds rise"]
  headline_conf: near_verbatim
  tickers: [QQQ, SMH, NVDA, SPY, TLT]
  scheduled: true
  verify: V
- date: 2026-06-17
  time_et: "14:00"
  category: fed_decision_hawkish_hold
  move: "S&P -1.21%, Nasdaq -1.34%, Dow -507"
  first_source: "FOMC statement (first under Warsh)"
  headlines: ["FED HOLDS RATES, SIGNALS RATE HIKES INCREASINGLY LIKELY"]
  headline_conf: reconstructed
  tickers: [SPY, QQQ, TLT]
  scheduled: true
  verify: V
- date: 2026-07-29
  time_et: "14:00"
  category: fed_decision_hawkish_dissent
  move: "Dow ~-1,000 (-1.9%), S&P ~-1%; 30y highest since 2007"
  first_source: "FOMC statement"
  headlines: ["FED HOLDS AT 3.5%-3.75%; HAMMACK, KASHKARI, LOGAN DISSENT IN FAVOR OF HIKE"]
  headline_conf: reconstructed
  tickers: [SPY, DIA, TLT]
  scheduled: true
  verify: V
- date: 2026-08-19
  time_et: "intraday"
  category: treasury_intervention
  move: "30y -~10bp from 5.337%; Dow +~230 on headline"
  first_source: "US Treasury announcement"
  headlines: ["TREASURY DOUBLES LONG-END BUYBACK OPERATIONS TO $4 BILLION", "Treasury doubles debt buybacks as Bessent moves to steady bond market"]
  headline_conf: near_verbatim
  tickers: [TLT, SPY]
  scheduled: false
  verify: V
```

---

## 3. What made news move the market: criteria and keywords

### A. Features that predicted a big move (roughly in order of weight)

1. **Surprise relative to a known expectation is the strongest factor.** The biggest scheduled-data days all had a sizable miss against consensus:
   - CPI: 7.7% vs 7.9% (+5.5% S&P); 8.3% vs 8.1% with core 0.6% vs 0.3% (−4.3%).
   - Payrolls: 172K vs 80K, more than 2x consensus (−2.6%); 114K vs 175K (−1.8%).
   - Fed dot plot: 2 cuts vs an expected 3 (−3%).

   The scoring needs a `surprise = (actual − consensus) / typical_dispersion` feature. For CPI, core m/m matters most; a 0.2pp miss or more was decisive in 2022. When an event is about 90% priced in (the Sep 2026 hike, the Feb 2026 IEEPA ruling), it barely moves, even when the headline sounds historic.

2. **Unscheduled news from an authority that can act immediately.** This covers the president (Truth Social/X), the FOMC (intermeeting action), Treasury, the FDIC and other regulators, foreign commerce ministries (MOFCOM), the PBOC/BOJ, and militaries. Wording like "effective immediately", "hereby", "authorized" or "ordered" signals action, not talk.

3. **Policy reversal or a regime break.** Reversals produced the largest single-day *gains*: the Apr 9, 2025 pause, the May 12 Geneva cut, the Apr 2026 Iran ceasefire, the Jan 21, 2026 Greenland climb-down, and the Dec 26, 2018 "100% safe" comment. A reversal of an *already-priced* shock moves more than the original shock did.

4. **Quantified magnitude.** Specific large numbers, such as a tariff of 25% / 34% / 50% / 100% / 125% / 145%, "$300 billion", "50 basis points", "to zero", "$700 billion" or "−258K revisions", go with bigger moves. Scale by both size and breadth: "all countries", "all US goods" or "every trading partner" moved more than single-sector actions.

5. **Systemic or liquidity vocabulary.** Words like receivership, closed by regulators, deposits, bank run, backstop, systemic risk exception, liquidity, repo, funding, AT1, written down, margin call, carry trade, limit down, halt or circuit breaker carry high weight. Also weigh the cross-asset spillover: KRE −12% with S&P flat is still important at sector level.

6. **Threats to institutional credibility.** Attacks on Fed independence (Apr 21, 2025; Jan 12, 2026), the BLS firing, a sovereign downgrade, and "Sell America" (stocks, bonds and the dollar falling together) all count. These move the dollar, gold and long yields first. Equity reaction varies: it is large when paired with an explicit threat to fire the chair, and muted for governor-level actions such as the Cook firing.

7. **Energy-chokepoint geopolitics.** Hormuz closures, Gulf ship attacks and strikes on producers move oil by 7–25% within hours. Equities react mainly through oil, so the second-day move often depends on whether oil holds. Wars that do not threaten supply (Hamas Oct 2023, Ukraine on its first day) often see buy-the-dip reversals.

8. **Market-state amplifiers.** The same headline hits harder when the market is near records with low VIX (Oct 10, 2025), when positioning is crowded (yen carry in Aug 2024, short-vol in Feb 2018), when liquidity is thin (holiday sessions on Nov 26, 2021 and Dec 24, 2018), or when it lands on top of a recent shock (Mar 2020 and Apr 2025 clusters). Feed VIX level and the recent drawdown in as features.

9. **Timing.** Weekend and overnight releases produce gap moves at the open: Sunday Fed actions, Sunday tariff tweets, Saturday strikes, and the Sunday-night Powell video. A Truth Social post between 09:30 and 16:00 ET produced minute-scale moves: the Apr 9 pause at 13:18, the 13:26 tariff tweet, and the Oct 10 post just before 11:00.

10. **Source credibility gate.** On Apr 7, 2025, an unverified X aggregator post briefly moved the S&P 8% before the White House called it "fake news." Score these as **important but unconfirmed**. Upgrade only when a primary source (whitehouse.gov or the Truth Social account itself, federalreserve.gov, fdic.gov, bls.gov, MOFCOM) or a tier-1 wire confirms.

### B. Keyword and phrase lexicon (taken from the headlines above)

- **Trade:** tariff, reciprocal, baseline, "effective immediately", "hereby", "authorized", "90-day", "90 day PAUSE", pause, suspend, delay, exempt, exemption, retaliat(e/ion), countermeasures, "all US goods", "all countries", "except China", Section 301 / 232 / 122, IEEPA, "export controls", "rare earths", truce, "framework", "deal reached", "slash tariffs", "roll back", "Liberation Day", "on top of", "over and above".
- **Central bank:**
  - Actions: emergency, intermeeting, "unscheduled", "cuts rates to zero", "basis points" / "bp", "half point", "50 basis points", "75 basis points", hike, "first hike since", "first cut since".
  - Tools: QE, "unlimited", "in the amounts needed", "balance sheet", runoff, "automatic pilot", "dot plot", "fewer cuts", "two cuts", dissent / "dissents".
  - Tone: "patient", "long way from neutral", "not a foregone conclusion", "pain", "may warrant adjusting".
  - Independence: "fire Powell", "remove", "for cause", "subpoena", "grand jury", "criminal investigation", "Fed independence", "nominate(s) … Fed chair".
- **Data:** CPI, "core", "m/m", "y/y", "est.", "vs. expected", "hotter than expected", "cooler", payrolls, "nonfarm", "revised down", revisions, "Sahm rule", "unemployment rate rises", "40-year high".
- **Systemic / financial:** "bank run", deposits, "deposit outflows", receivership, "closed by regulators", "FDIC", "systemic risk exception", "backstop", "Bank Term Funding Program", liquidity, "repo", "capital raise", "loss on securities sale", "AT1", "written down", "halted", "limit down", "circuit breaker", "Level 1", "margin call", "carry trade", "unwind", "liquidations", "flash crash", "VIX spikes", "ETN terminated", "downgrade(s) US", "AAA", "yield curve inverts", "30-year yield highest since 2007", "buyback".
- **Geopolitical / energy:** strikes, "launches strikes", "declaration of war", invasion, "special military operation", killed (leader), "Strait of Hormuz", "closes", blockade, "tankers struck", "supply disruption", "OPEC+ collapse", "price war", "oil surges / plunges", "ceasefire", "two-week ceasefire", "Complete and Total CEASEFIRE".
- **Pandemic:** "pandemic", "national emergency", "travel ban / suspend travel", "lockdown", "variant of concern", "vaccine … 90% effective", "interim analysis".
- **Political / social:** "Truth Social", "DJT", ALL-CAPS phrases from the president ("THIS IS A GREAT TIME TO BUY", "PAUSE", "CEASEFIRE"), "fake news" (a denial reverses the move), "highly unlikely" (denial), "no intention of firing".

### C. Suggested scoring rule

**importance = authority × actionability × magnitude × surprise × breadth × market_state, gated by source_confidence**

- **authority** runs from 1.0 for POTUS, the FOMC, Treasury, FDIC, PBOC/BOJ and foreign governments, down to 0.3 for a single official's opinion or a regional Fed president.
- **actionability** is high for "effective immediately", "hereby" or "signed" and low for "considering", "could" or "weighing" (although "weighing a massive increase" still moved stocks −2.7% on Oct 10, 2025).
- **magnitude** comes from the parsed numbers (tariff %, bp, $B, % of GDP, oil move) compared with historical norms.
- **surprise** is measured against consensus or prediction markets.
- **breadth** compares "all countries / all goods" with one sector.
- **market_state** reflects VIX, drawdown and liquidity.
- Separately, flag **reversal** (a pause, deal, ceasefire or denial of an earlier shock) as a high-weight class of its own.

---

## 4. Counterexamples: headlines that sounded big but did not move US equities much

| Date | Headline (gist) | What happened | Why | Tag |
|---|---|---|---|---|
| 2020-01-03 / 01-08 | "US KILLS IRAN'S SOLEIMANI"; "IRAN FIRES MISSILES AT US BASES IN IRAQ" | S&P −0.7%, then +0.5% after Trump tweeted "All is well!" | No oil supply hit; signs of restraint | M |
| 2021-01-06 | "RIOTERS STORM US CAPITOL" | S&P +0.6% | No change in economic policy | M |
| 2022-02-24 | "RUSSIA INVADES UKRAINE" | Nasdaq −3.5% at the open, then closed +3.3% | Priced in; sell the rumor, buy the news | M |
| 2022-10-13 | Hot CPI (core 6.6%) | S&P fell −2.4% early, then closed **+2.6%** | Positioning flush | M |
| 2023-03-13 | "SIGNATURE BANK CLOSED; SYSTEMIC RISK EXCEPTION" | S&P −0.15%, but KRE −12% and FRC −62% | Backstop offset the index effect; important at **sector** level | M |
| 2023-05-01 | "FIRST REPUBLIC SEIZED, SOLD TO JPMORGAN" | S&P −0.04% | Resolution was expected | M |
| 2023-05 | Debt-ceiling "X-date" headlines | Mostly small moves | Market expected a deal | M |
| 2023-10-09 | Hamas attack on Israel | S&P +0.6% | No energy-supply threat | M |
| 2024-09-18 | "FED CUTS HALF POINT" (first cut, 50bp) | S&P −0.3% on the day (then +1.7% the next) | About 60% priced in | M |
| 2025-05-29 | Trade court strikes IEEPA tariffs (evening May 28) | Futures +1.5%, faded to S&P +0.4% | Expected appeal or stay | M |
| 2025-06-23 | "IRAN STRIKES US BASE IN QATAR" | Oil −7%, stocks **up** | Telegraphed, symbolic retaliation | V ([CNN](https://www.cnn.com/2025/06/23/investing/stock-market-dow-oil-iran)) |
| 2025-07-16 | "TRUMP LIKELY TO FIRE POWELL SOON" | S&P −0.7% intraday, closed +0.3% | Denial within hours ("highly unlikely") | V |
| 2025-08-26 | "TRUMP FIRES FED GOVERNOR LISA COOK" | S&P +0.4% | A governor, not the chair; legal fight expected | V ([CBS](https://www.cbsnews.com/news/us-stock-steady-trump-lisa-cook-federal-reserve-governors)) |
| 2025-10-01 to 11-12 | "GOVERNMENT SHUTS DOWN" (longest ever, 43 days) | S&P at records on day 1 and through early October | Historically non-economic; AI and earnings dominated | V ([CNBC](https://www.cnbc.com/2025/09/30/stock-market-today-live-updates.html), [CNN](https://www.cnn.com/2025/10/03/investing/us-stock-market-government-shutdown-jobs-report)) |
| 2026-01-30 | "TRUMP NOMINATES WARSH AS FED CHAIR" | S&P −0.4%, but silver −26% to −31% | Big for **metals and FX**, small for equities, so score by asset class | V |
| 2026-02-14 to 04-30 | DHS-only partial shutdown (reported as 76 days) | No equity reaction found | Narrow scope | V (existence only) |
| 2026-02-20 | "SUPREME COURT STRIKES DOWN TRUMP TARIFFS" | S&P +0.69% | Priced in by prediction markets; immediately replaced by Section 122 | V |
| 2026-03-02 | "IRAN'S KHAMENEI KILLED; HORMUZ CLOSED" | S&P +0.04% (Dow −600 intraday) | Weekend pre-hedging; oil reacted, equities waited (then fell over the following weeks) | V |
| 2026-09-11 | Hot August CPI (core 0.3% vs 0.2%) | S&P **+1%** | Oil fell from about $110, which dominated | V ([Yahoo](https://finance.yahoo.com/markets/live/stock-market-today-friday-september-11-dow-sp-500-nasdaq-cpi-inflation-082201751.html)) |
| 2026-09-16 | "FED RAISES RATES, FIRST HIKE SINCE 2023" | Muted or mixed | 92% priced in | V |
| 2026-07-24 | Section 122 tariffs lapse and are replaced by Section 301 | No index reaction found | Swap of legal authority, similar rate | V (existence only) |

**Calibration lessons:**
1. "Historic" or "first since" wording with no surprise does not predict a move.
2. A denial or reversal within hours erases the move, so score the follow-up as well.
3. Score importance per asset class (equities, rates, FX, oil, metals). Several of these "non-movers" were big for oil, metals or a single sector.
4. A military escalation matters for equities mainly when it threatens energy supply or shipping chokepoints.
5. Hawkish *guidance* (dots, dissents, "hikes increasingly likely") moved stocks more than the actual expected hike.

---

## Where to verify next
- Re-check all [M] index percentages against an official close history before using them as labels. That covers everything before 2024-12-18 except the items marked V. The ones to check first are Aug 5 2024, Sep 18 2024, Mar 2020 circuit-breaker times, Nov 10 2022 and Sep 13 2022.
- Exact first-print flash headlines (Bloomberg/Reuters/Dow Jones) are paywalled. The "reconstructed" entries are style-faithful test inputs, not archival text. Truth Social texts for Apr 9 2025, Oct 10 2025, Apr 21 2025 and Jan 2026 (Greenland) are verbatim or near-verbatim according to reporting.
- Still open: index closes for Jan 12 2026, Jan 21 2026, Jul 30 2026 (the CNBC piece said the Nasdaq rose 2.8% to end a 6-day losing streak after the Fed selloff), Sep 16 2026, and the Oct 7–8 2026 yield and AI selloff.
- More searches can continue this if the user sends a follow-up or raises the search limit (`CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION`).

No files were created; this report is the whole deliverable.