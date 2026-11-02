# Project scope

Read README.md and docs/FORMAT.md before changing ingestion or contracts.
Keep this repository script-driven and read-only: download and validate market
data, with no broker orders, strategy search, portfolio sizing or services.
Keep assets, calendar, raw/split-adjusted prices, feed, provenance and hashes
explicit. Never relabel IEX as consolidated SIP or adjusted prices as raw.
Tests use MockTransport; never use real credentials in tests or commit data.
Future execution lives elsewhere and must respect the user's FIFO sells,
31-day equity holds, unrestricted ETF holding periods, and no short selling.
Alpaca's us_equity class alone does not establish ETF classification/exemptions.
