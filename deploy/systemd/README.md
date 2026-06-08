# hh-monitor systemd units (vendored)

Version-controlled mirror of the live systemd configuration on srv-hh
(/etc/systemd/system/). Captured for reproducibility. The *.d/override.conf
files are systemd drop-ins layered on top of the base units.

## Effective runtime config (base + drop-ins)
- bot: always-on Telegram long-polling, Restart=always.
- pipeline: hourly at :00, 05:00-21:00 MSK (drop-in narrows the base unit). ExecStart passes
  `--max-pages 20` — GLOBAL per-search page cap (~1000 freshest resumes), sized for search
  id=5's ~21-day freshness window; revisit before re-enabling unfiltered (large-pool) searches.
- llm: hourly at :05, 05:05-21:05 MSK (+5 min after pipeline); drop-in adds
  `--max-events-per-search 100` and TimeoutStartSec=60min.
- oauth refresh: every 6h, `hh refresh --if-due`.
- digest: weekly Excel HR digest, Fri 12:00 MSK, oneshot `digest weekly`.

NOTE: the base *.timer Description lines still say "15 runs/day 01,04,09..21".
That is superseded by the drop-ins (hourly 05-21). Descriptions are kept
verbatim to mirror the server byte-for-byte; the drop-in OnCalendar wins.

## Restore on a fresh server

```bash
sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo mkdir -p /etc/systemd/system/hh-monitor-llm.service.d \
              /etc/systemd/system/hh-monitor-llm.timer.d \
              /etc/systemd/system/hh-monitor-pipeline.timer.d
sudo cp deploy/systemd/hh-monitor-llm.service.d/override.conf /etc/systemd/system/hh-monitor-llm.service.d/
sudo cp deploy/systemd/hh-monitor-llm.timer.d/override.conf   /etc/systemd/system/hh-monitor-llm.timer.d/
sudo cp deploy/systemd/hh-monitor-pipeline.timer.d/override.conf /etc/systemd/system/hh-monitor-pipeline.timer.d/
sudo systemctl daemon-reload
sudo systemctl enable --now hh-monitor-bot.service hh-monitor-llm.timer \
     hh-monitor-pipeline.timer hh-oauth-refresh.timer hh-digest.timer
```
