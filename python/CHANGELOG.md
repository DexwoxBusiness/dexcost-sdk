# Changelog

All notable changes to dexcost will be documented in this file.

## [0.1.0] - 2026-02-25

### Added
- Task tracking: decorator, context manager, manual start/end (US-005--US-009)
- Auto-instrumentation for OpenAI, Anthropic, LiteLLM (US-012--US-014)
- Pricing engine with bundled model costs (US-010)
- Cost rates registry for non-LLM services (US-011)
- Retry detection and waste tracking (US-015)
- Standard Event Schema v1 with JSON Schema validation (US-002)
- SQLite storage with WAL mode and migrations (US-003)
- API key infrastructure with dx_live_/dx_test_ format (US-017)
- PII redaction and metadata policy (US-018)
- Background event push to Control Layer (US-016)
- Code scanner: `dexcost scan` CLI command (US-019)
- Wrapper clients: TrackedOpenAI, TrackedAnthropic (US-021)
- CLI: status, rates, scan commands
