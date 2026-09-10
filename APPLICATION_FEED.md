# Application link feed

`filtered_jobs.json` contains the same new listings selected for Telegram by
`job_monitor.py`. No additional relevance filter or second scraper is used.
Each record has `ID`, `Title`, `Link` and UTC `first_seen_at`.

The feed is cumulative and deduplicated by NHS Jobs ID. Existing entries survive
quiet runs and scraping failures. Export happens before the seen-ID update.
Telegram is best-effort: inclusion means selected for notification, not proof
that Telegram delivered it. Closed listings remain in the feed; check the live
advert before applying.

The application agent reads this file through GitHub and queues each record's
Link and Title into its private local ledger. Do not use `jobs.txt` for this:
that file also includes rejected listings. Do not put personal profiles,
credentials, application contents, outcomes or email details in this public repo.

Collection starts when the updated workflow runs. Historical Telegram messages
cannot be reconstructed from jobs.txt alone. An already running long polling
block may use its old workflow until the next block starts.

Offline tests: `python -m unittest discover -s tests -v`.
