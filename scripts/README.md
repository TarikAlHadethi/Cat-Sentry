# scripts

- `verify.sh` — self-test. Checks config, secrets hygiene, container health,
  network exposure, and whether Frigate is actually receiving frames.
  Run it after any config change: `bash scripts/verify.sh`

- `first-frame.jpg` — written by `python setup.py --test-stream`. Not committed.
