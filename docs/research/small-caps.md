# Research: small and mid caps that moved big on one headline (2024 – Oct 2026)

Compiled 2026-10-09 for the small-cap lane ([docs/SMALLCAPS.md](../SMALLCAPS.md)). Move figures
differ between outlets; close-to-close is used where found, otherwise premarket (PM), after-hours
(AH) or intraday is stated. Market caps are approximate (last close × shares). ≈ marks headline
wording reconstructed from coverage. The backtest (`news247/data/history.yaml`, categories
`smallcap_*`) uses the events below whose first report and move are documented.

## Events

| Date | Ticker | Cap before | Catalyst | First report (venue) | Move |
|---|---|---|---|---|---|
| 2024-02-15 | SOUN | ~$0.9B | Nvidia's first 13F-HR lists a stake | SEC EDGAR 13F-HR | +67% close |
| 2024-04-22 | MTTR | ~$0.55B | CoStar buyout, $5.50, 216% premium | ≈ company releases | +175–181% intraday |
| 2024-07-08 | MORF | ~$1.6B | Lilly buyout, $57 cash, 79% premium | ≈ Lilly / Morphic release | ≈+75% |
| 2024-07-19 | SERV | ~$0.1B | Nvidia ~10% stake | SEC Schedule 13G | +187% close, +233% in 2 days |
| 2024-08-29 | AILE | ~$0.43B | Hindenburg short report | short seller's site + X | −30% early, −60% low |
| 2024-11-25 | SAVA | ~$1.27B | Phase 3 failure | ≈ company release, premarket | −83.8% |
| 2024-11-26 | PSTX | ~$0.32B | Roche buyout, $9 + CVR, 215% premium | "Roche enters into a definitive agreement to acquire Poseida Therapeutics…" | ≈+200% |
| 2024-11-27 | APLT | ~$1.0B | FDA CRL | "Applied Therapeutics Receives FDA Complete Response Letter for Govorestat New Drug Application" | −76% |
| 2025-04-21 | UPXI | ~$45M | $100M PIPE for a Solana treasury | ≈ wire + 8-K | +335% close |
| 2025-05-21 | NVTS | <$0.4B | Nvidia collaboration (800 V HVDC) | "NVIDIA Selects Navitas to Collaborate on Next Generation 800 V HVDC Architecture" (GlobeNewswire) | ~+190% AH, +164% next day |
| 2025-05-22 | VIGL | ~$0.13B | Sanofi buyout, $8 + CVR, 246% premium | "Vigil Neuroscience Enters into Definitive Merger Agreement to be Acquired by Sanofi" | ≈+240% |
| 2025-05-27 | SBET | <$10M | $425M PIPE for an Ether treasury | "SharpLink Gaming Announces $425,000,000 Private Placement to Initiate Ethereum Treasury Strategy" | +400%+ intraday, then −69% on Jun 13 |
| 2025-05-28 | SPRO | ~$40M | Phase 3 stopped early for efficacy | "Spero Therapeutics and GSK Announce PIVOT-PO Phase 3 Study … Stopped Early for Efficacy …" | +244% close |
| 2025-06-02 | APLD | ~$1.5B | CoreWeave 250 MW leases, ~$7B | ≈ company release | +48% intraday |
| 2025-06-17 | VERV | ~$0.55B | Lilly buyout, $10.50 + CVR | ≈ Lilly release | +81% |
| 2025-06-30 | BMNR | ~$25M | $250M PIPE for an Ether treasury | "BitMine Immersion Technologies Announces $250 Million Private Placement to Initiate Ethereum Treasury Strategy" | +695% close |
| 2025-07-10 | MP | ~$4.9B | DoD $400M preferred, ~15% holder | ≈ Business Wire | +50% close |
| 2025-07-22 | REPL | ~$0.95B | FDA CRL | ≈ Business Wire | −63% to −77% |
| 2025-09-08 | OCTO | ~$5M | $250M PIPE for a Worldcoin treasury | ≈ wire + 8-K | +3,009% close |
| 2025-09-24 | QURE | ~$0.75B | Pivotal Phase I/II success | "uniQure Announces Positive Topline Results from Pivotal Phase I/II Study of AMT-130 …" | +248% close |
| 2025-09-24 | LAC | ~$0.7B | Government equity stake (scoop) | ≈ Reuters, evening before | +95.7% close |
| 2025-10-06 | TMQ | ~$0.33B | US government 10% stake + warrants | "Trilogy Metals Announces Strategic Investment by US Federal Government" | +211% close |
| 2025-10-06 | CRML | ~$0.8B | Stake talks (scoop, then denied) | ≈ Reuters | +75% PM, ~+45% after denial |
| 2025-12-03 | CAPR | ~$0.29B | Phase 3 success | "Capricor Therapeutics Announces Positive Topline Results from Pivotal Phase 3 HOPE-3 Study …" | ~4× intraday |
| 2025-12-11 | RZLT | ~$1.0B | Phase 3 failure | ≈ company release | opened −89% |
| 2026-03-17 | ALDX | ~$0.25B | Third CRL | ≈ company release | −70.7% |
| 2026-04-23 | GRCE | ~$60M | FDA CRL | ≈ company release | −58% |
| 2026-06-04 | XOS | ~$50M | $6M registered direct | ≈ wire + 8-K | −24% PM |
| 2026-06-30 | UNCY | ~$0.1B | CRL (manufacturing only) | ≈ company release | −37% |
| 2026-07-16 | ATAI | ~$2.2B | Lilly buyout, $6.75 + CVR, 26% premium | ≈ Lilly / ATAI release | ≈+25% |
| 2026-07-30 | CAPR | ~$0.3B | FDA panel votes 9–3 against | ≈ media | −54% intraday |
| 2026-09-28 | KOD | ~$1.7B | Phase 3 success (date pre-announced Sep 25) | ≈ company release | +178% close |

Not in the backtest: SOUN (a 13F holdings table cannot be read from a filing's title), index
additions (+3–10%, informative but not push-worthy), sector sympathy moves (quantum, nuclear,
drones; handled by themes and baskets), and events whose move could not be confirmed.

## Patterns

| Catalyst | Typical small-cap move | Timing | Wording |
|---|---|---|---|
| Pivotal readout, positive | +60% to +400% | 07:00–08:30 ET release, most of the move premarket; an offering often follows the same day | "met its primary endpoint", "statistically significant", "stopped early for efficacy", "Positive Topline Results" |
| Pivotal readout, negative | −70% to −90% | the opening print | "did not meet", "did not achieve statistical significance", "Topline Results" with no "positive" |
| FDA CRL | −20% (manufacturing) to −78% (efficacy) | company release after close or premarket; since 2025 the FDA also posts CRLs itself | "Complete Response Letter" |
| FDA panel against | −35% to −58% | next morning | "voted X–Y", "does not support" |
| FDA path cleared | +50% to +80% | premarket | "FDA agreed", "alignment", "accelerated approval pathway" |
| Being acquired | ≈ the premium minus a 2–10% spread: 80–250% for micro biotech, 25–80% mid | first print | "to be Acquired by", "per share in cash", "premium of approximately X%", "CVR" |
| Giant partner / big contract | +40% to +190% when the deal is large vs. market cap | seconds after the release | "Selects [Company]", "collaborate with NVIDIA", "$X billion", "MW lease" |
| Giant's stake disclosed (13F/13G) | +60% to +190% | 13F deadlines; 13G any day | filer = NVIDIA, Alphabet, Amazon… |
| Government equity stake | +30% to +210% | usually leaks first (Reuters, Bloomberg TV, FT, White House) | "Department of War", "Office of Strategic Capital", "equity stake", "warrants" |
| Crypto-treasury PIPE | +300% to +3,000% for micro caps | 08:00–09:00 ET wire, repeated LULD halts | "Private Placement to Initiate [ETH/SOL/WLD] Treasury Strategy" — highest pump risk, frequent reversals |
| Short-seller report | −8% (mid) to −60% | premarket, short seller's site + X | "we are short", "fabricated", "undisclosed related party" |
| Dilutive offering | −10% to −40% (registered directs with warrants worse) | after close / overnight | "Announces Pricing of", "Registered Direct Offering", "pre-funded warrants" |
| Index inclusion | +3% to +10% | ~17:15 ET release | "Set to Join S&P SmallCap 600 / MidCap 400" |

Pump-and-dump filters used by practitioners: Nasdaq/NYSE/NYSE American only; price ≥ $1;
market cap ≥ $30–50M; real dollar volume; extra suspicion for recent China/Hong Kong micro-cap
IPOs (Nasdaq: ~70% of its manipulation referrals; it raised their minimum IPO size in 2025) and
for "treasury strategy" announcements by companies under $50M.

## Feeds and data

- GlobeNewswire subject and industry RSS (Mergers and Acquisitions, Business Contracts, Clinical
  Study, Financing Agreements, Biotechnology, Defense); each release carries its exchange:ticker.
- PR Newswire `health-latest-news/fda-approval-list.rss`, `heavy-industry-manufacturing-latest-news/aerospace-defense-list.rss`.
- Business Wire topic codes (Aerospace `G1QFDERJXkJeGFNZXQ==`).
- Newsfile industry feeds (`feeds.newsfilecorp.com/industry/{slug}`), Canadian-heavy.
- FDA drugs RSS; FDA CRL database `api.fda.gov/transparency/crl.json` (not yet wired in).
- ACCESS Newswire has no public release RSS (only a newsroom JSON API), so it is not polled.
- Nasdaq trading halts RSS (already used): T1 "news pending" on a small cap gives the ticker
  before the headline.
- Market caps: Nasdaq's public screener `api.nasdaq.com/api/screener/stocks?tableonly=true&download=true`
  (`data.rows[]`: symbol, name, lastsale, netchange, pctchange, volume, marketCap, country,
  ipoyear, industry, sector; browser headers required; a snapshot, not tick data).
- Movers: Yahoo `v1/finance/screener/predefined/saved?scrIds=small_cap_gainers` (`finance.result[0].quotes[]`
  with marketCap, regularMarketVolume, averageDailyVolume3Month, pre/post-market fields); may
  require a cookie + crumb (`fc.yahoo.com`, then `v1/test/getcrumb`).

## Sources

Moves and headlines: fool.com (SOUN, NVTS), barchart.com (SERV, QURE), ir.navitassemi.com,
pharmaphorum.com, nasdaq.com, sec.gov 8-K exhibits (Spero, Capricor, BitMine, Trilogy),
thepharmaletter.com, benzinga.com, biospace.com, businesswire.com, bioworld.com, zacks.com,
finance.yahoo.com, seekingalpha.com, clinicaltrialsarena.com, 247wallst.com, tikr.com,
roche.com, bisnow.com, biopharmadive.com, biocentury.com, nbcsandiego.com / CNBC, decrypt.co,
bloomberg.com, cointelegraph.com, sherwood.news, northernminer.com, nbcnewyork.com,
tipranks.com, capitalbrief.com, investing.com, tradingpedia.com, press.spglobal.com, lseg.com,
loeb.com (Nasdaq listing rules), sec.gov trading suspension 34-104113.
