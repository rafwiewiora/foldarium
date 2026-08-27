# Weekly Selector WASM Mapper

Offline Rust/WASM mapper for Foldarium weekly selector submissions. It consumes
the **public kit manifest** plus participant decisions and emits the same
canonical submission JSON validation behavior as the Python reference client
and the JS/API contract.

## Guarantees

- No network access
- No answer keys or private pipeline identifiers
- Exact top-level submission keys: `schema_version`, `submission_id`,
  `environment`, `round_id`, `blind_manifest_sha256`, `kit_sha256`, `items`
- Each item has independent `clustered` (`cluster` or `none`) and
  `unclustered` (`exact` or `none`) tagged decisions
- Nullable shorthand and representative-to-exact inference are rejected
- Method/display identity belongs to token issuance, not submission JSON

## Build

```bash
wasm-pack build --target web \
  --out-dir ../../weekly-selector-offline \
  --out-name weekly_selector_wasm
```

## Test

```bash
cargo test
node --test tests/parity.test.js
```

## JS API

After building with `wasm-pack`, the generated package exports:

- `validateSubmission(manifestJson, submissionJson)`
- `buildSubmission(manifestJson, submissionId, itemsJson)`
- `buildTemplate(manifestJson)`
- `digestSubmission(manifestJson, submissionJson)`

See `weekly_selector.js` for a thin wrapper around the generated bindings.
