# Changelog

## 0.1.1 — 2026-11-07
- Moved from the private `Emacee/ha-app` monorepo to its own public repository, so it
  can be installed through HACS as a custom repository.
- `documentation`, `issue_tracker` and the device's configuration URL now point to
  this repository. Removed the redundant `loggers` entry from `manifest.json`.
- Added `hacs.json`, GitHub Actions for hassfest, HACS validation, ruff and the test
  suite, plus `requirements_test.txt`.
- New original icon. It ships in `brand/` as local brand images (`icon.png`
  256×256, `icon@2x.png` 512×512), shown by Home Assistant 2026.3 and newer. The
  source is in `assets/icon.svg`, plus a GitHub social preview image.
- README: compatibility overview of the HomeWizard products (only the P1 meter is
  supported), when to use cloud versus local, and a trademark note.
- README and changelog are now in English.
- Licensed under MIT (`LICENSE`).
- CI fix: ruff is pinned (`ruff==0.17.4`) and the lint rules are set explicitly in
  `pyproject.toml`. Before this, CI ran a newer ruff whose defaults no longer include
  `DTZ007`, so the intended `# noqa: DTZ007` in `protocol.py` failed as an unused
  directive (`RUF100`). Entity names are still translated (English
  and Dutch).
- No functional changes. Existing installs keep their entities, because the domain
  and unique ids are unchanged.

## 0.1.0 — 2026-11-06
- First version. Reads a HomeWizard Wi-Fi P1 meter through the (unofficial)
  HomeWizard cloud, for meters that are not on the Home Assistant network.
- Config flow:
  - log in with the HomeWizard Energy account;
  - pick a P1 meter when the account has several (one entry per meter, unique id =
    cloud device id);
  - re-authentication when the login is rejected;
  - options for the realtime stream, phases and the minimum time between updates
    (default 5 s).
- Two streams:
  - the state stream: full state plus JSON patches, with the same field names as
    local API v1;
  - the optional realtime stream: power every second, per phase.
- Stream handling: automatic reconnect with increasing delay, resubscribe after
  120 s of silence, token renewed 5 minutes before it expires, and a 3-minute
  availability grace period while reconnecting.
- Sensors appear as soon as their field shows up in the cloud state:
  - power (total and per phase), voltage, current, frequency;
  - energy import/export: the total (falling back to the sum of T1–T4) and per tariff;
  - tariff, 15-minute average demand and monthly peak;
  - external M-Bus meters (gas, water, heat), falling back to `total_gas_m3`;
  - voltage sags/swells, power failures, Wi-Fi strength, DSMR version, meter model;
  - realtime power (total and per phase);
  - binary sensors *Cloud connection* and *P1 meter online*.
- Diagnostics include the full last cloud message, with credentials and identifiers
  redacted.
- No external requirements. Requires Home Assistant 2025.2 or newer.
- Not yet tested against the real HomeWizard cloud.
