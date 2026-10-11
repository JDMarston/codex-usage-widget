# Changelog

## 2.0.0

### Quota reliability
- Prefer Grok's CLI credits endpoint with its token-auth header; retry the billing RPC when the percentage is missing.
- Missing, malformed, non-finite, and explicit-zero percentages are separate states. A billing period without a percentage never becomes a made-up 100% remaining bar.
- Validate gRPC frames, trailers, length bounds, and overflowing protobuf values. Use a nonempty request with explicit default billing semantics.
- Use the public Hermes Codex usage snapshot instead of a renamed private URL helper, preserving additional model limits and window duration when supplied.
- Collect providers independently, keep local history responsive, and retain last-good numbers with their original timestamp on temporary failures.
- Disconnected providers cannot be resurrected by a late worker result or historical token rows.

### Native UI
- Quota-first account cards, readable remaining-capacity bars, reset countdowns, and per-provider check times.
- Separate token-history view with all recorded models and stable scroll position.
- Expanded and compact layouts, system light/dark colors, DPI-aware sizing, negative-coordinate monitor support, keyboard refresh, and exact reset details on hover.
- Visible unavailable/sign-in/not-reported states in compact mode and the tray; no false global “Live” indicator.
- Bar fills and percentage labels now both represent **remaining** allowance.

### Maintenance
- One persistent Tk process; short-lived parallel quota helpers, bounded retries, clean worker-tree shutdown, and rotating logs.
- Hash-locked Windows dependencies, native UI regression tests, package-integrity manifests, and ZIP checksums.
- Per-user installer uses graceful shutdown, verified staging, and timestamped rollback copies. Startup opt-in is preserved.

### Known boundaries
- Grok billing interfaces are observed provider interfaces, not a guaranteed public API. An omitted protobuf scalar can mean zero or an unpublished meter; this app deliberately shows “Not reported” unless an endpoint publishes a number.
- Gemini's browser-backed adapter shows a 28-day rate-limit peak, not current subscription headroom. It requires the optional dedicated-browser integration documented in the README.
- The legacy Hermes desktop pane is not installed or updated by the Windows installer.
