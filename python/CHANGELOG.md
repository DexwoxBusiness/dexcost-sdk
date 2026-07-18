# Changelog

All notable changes to dexcost will be documented in this file.

## [0.6.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/python/v0.5.0...python/v0.6.0) (2026-07-18)


### Features

* **attribution:** capture observer billing dimensions across SDKs ([#95](https://github.com/DexwoxBusiness/dexcost-sdk/issues/95)) ([c4d7f71](https://github.com/DexwoxBusiness/dexcost-sdk/commit/c4d7f7104412b697d1c77c04ff89f30ff50aa95d))

## [0.5.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/python/v0.4.1...python/v0.5.0) (2026-07-18)


### Features

* **attribution:** observe unpriced service usage across SDKs ([#89](https://github.com/DexwoxBusiness/dexcost-sdk/issues/89)) ([93109e3](https://github.com/DexwoxBusiness/dexcost-sdk/commit/93109e348f4b339e0c532a5bed7e03a1be43ee40))

## [0.4.1](https://github.com/DexwoxBusiness/dexcost-sdk/compare/python/v0.4.0...python/v0.4.1) (2026-07-18)


### Bug Fixes

* **python:** preserve attribution v2 delivery ([#82](https://github.com/DexwoxBusiness/dexcost-sdk/issues/82)) ([774f263](https://github.com/DexwoxBusiness/dexcost-sdk/commit/774f2637ccfbe0b2bb213a4cd4f9edf812e67aac))

## [0.4.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/python/v0.3.0...python/v0.4.0) (2026-07-17)


### Features

* **go:** add attribution v2 ingestion ([#74](https://github.com/DexwoxBusiness/dexcost-sdk/issues/74)) ([655931d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/655931d446ad1986f5e9b1aaeb93a32faf6524a4))

## [0.3.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/python/v0.2.2...python/v0.3.0) (2026-07-17)


### Features

* **python:** emit attribution v2 records ([#72](https://github.com/DexwoxBusiness/dexcost-sdk/issues/72)) ([9af5678](https://github.com/DexwoxBusiness/dexcost-sdk/commit/9af56786d4fe8611b46cd09a4919db3af6384b90))

## [0.2.2](https://github.com/DexwoxBusiness/dexcost-sdk/compare/python/v0.2.1...python/v0.2.2) (2026-07-16)


### Bug Fixes

* **release:** recover skipped SDK releases ([#67](https://github.com/DexwoxBusiness/dexcost-sdk/issues/67)) ([466b332](https://github.com/DexwoxBusiness/dexcost-sdk/commit/466b332898be927be4d7aaa56544d9143fc4ffff))

## [0.2.1](https://github.com/DexwoxBusiness/dexcost-sdk/compare/python/v0.2.0...python/v0.2.1) (2026-06-03)


### Bug Fixes

* **sdk:** cross-SDK canonical serialization parity (decimals, fields, round-trip) ([ae2e40c](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ae2e40c95d1be0034c752f9b5b9ff35b17f78996))
* **sdk:** cross-SDK canonical serialization parity (decimals, fields,… ([f7e156b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f7e156bd080dd9b363e2cd1eb74134e8c5b39be7))

## [0.2.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/python/v0.1.1...python/v0.2.0) (2026-06-03)


### ⚠ BREAKING CHANGES

* **sdk:** DEXCOST_ENDPOINT is no longer read. Configure a non-default endpoint via the in-code option instead. Pre-launch; no external consumers.

### Bug Fixes

* **python-sdk:** assert __version__ format, not a hardcoded literal ([105e587](https://github.com/DexwoxBusiness/dexcost-sdk/commit/105e587c2f63c7119a2205bf9cf8312cd81b9229))
* **python-sdk:** assert __version__ format, not a hardcoded literal ([b4e68c7](https://github.com/DexwoxBusiness/dexcost-sdk/commit/b4e68c738f51761bc7c6692e86d7f53273cb9708))


### Code Refactoring

* **sdk:** endpoint via explicit in-code config; drop DEXCOST_ENDPOINT env read ([0f4b397](https://github.com/DexwoxBusiness/dexcost-sdk/commit/0f4b39733320f3bd9848d83720a811a0a60467b0))

## [0.2.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/python/v0.1.1...python/v0.2.0) (2026-06-03)


### ⚠ BREAKING CHANGES

* **sdk:** DEXCOST_ENDPOINT is no longer read. Configure a non-default endpoint via the in-code option instead. Pre-launch; no external consumers.

### Bug Fixes

* **python-sdk:** assert __version__ format, not a hardcoded literal ([105e587](https://github.com/DexwoxBusiness/dexcost-sdk/commit/105e587c2f63c7119a2205bf9cf8312cd81b9229))
* **python-sdk:** assert __version__ format, not a hardcoded literal ([b4e68c7](https://github.com/DexwoxBusiness/dexcost-sdk/commit/b4e68c738f51761bc7c6692e86d7f53273cb9708))


### Code Refactoring

* **sdk:** endpoint via explicit in-code config; drop DEXCOST_ENDPOINT env read ([0f4b397](https://github.com/DexwoxBusiness/dexcost-sdk/commit/0f4b39733320f3bd9848d83720a811a0a60467b0))

## [0.1.1](https://github.com/DexwoxBusiness/dexcost-sdk/compare/python/v0.1.0...python/v0.1.1) (2026-05-30)


### Bug Fixes

* **python:** point Documentation URL to docs.dexcost.io ([b7ca0ab](https://github.com/DexwoxBusiness/dexcost-sdk/commit/b7ca0ab56e57d813f347a03e75e9b073310aa3d7))
* **python:** point Documentation URL to docs.dexcost.io ([abc6d38](https://github.com/DexwoxBusiness/dexcost-sdk/commit/abc6d38ed07a51f2ebbaa88c7d8debe9135030d5))

## 0.1.0 (2026-05-30)


### Features

* **cloud_detect:** broaden vendor coverage — GPU clouds, PaaS, more IaaS ([feb22fb](https://github.com/DexwoxBusiness/dexcost-sdk/commit/feb22fba4fe3e2665520bb921bcfb0bb83ee6cac))
* **cloud_detect:** cover ECS, Container Apps region, harden AWS heuristics ([1311fe2](https://github.com/DexwoxBusiness/dexcost-sdk/commit/1311fe21de8345f52f429ece019cc7d1173854ba))
* **compute:** auto-emit + back-fill compute_cost events at task finalize ([91beccc](https://github.com/DexwoxBusiness/dexcost-sdk/commit/91becccf2062ef6d50df9c48b7310577909e3338))
* **compute:** bundle compute price catalog (AWS/GCP/Azure/Vercel) ([dc4f419](https://github.com/DexwoxBusiness/dexcost-sdk/commit/dc4f41917b8a3935ecfbb6aee909959423aad440))
* **compute:** cgroup v2 file readers (cpu.stat, cpu.max, memory.{peak,max,current}) ([1609faf](https://github.com/DexwoxBusiness/dexcost-sdk/commit/1609faf1a35b64628ab31bc90d0d437bc01d223e))
* **compute:** extend CloudEnv with instance_type from IMDS (Decision [#3](https://github.com/DexwoxBusiness/dexcost-sdk/issues/3)) ([a613924](https://github.com/DexwoxBusiness/dexcost-sdk/commit/a613924f5b58e1bd3a4049f0669284d58ef4ff12))
* **compute:** Fargate ECS task metadata helper (MiB-&gt;bytes per Decision [#7](https://github.com/DexwoxBusiness/dexcost-sdk/issues/7)) ([08f2b22](https://github.com/DexwoxBusiness/dexcost-sdk/commit/08f2b22ce645b349d693b9855708cbe55cffc667))
* **compute:** per-task accountant — cgroup start/end snapshots, single event ([33cef0f](https://github.com/DexwoxBusiness/dexcost-sdk/commit/33cef0f1386b3a72a73da0b76191c8cab474b0e8))
* **compute:** pricing engine — per-billing-model math + degradation ladder ([e379f40](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e379f40342e33cedba4bcd46f7e0982ce0bc32ef))
* **compute:** runtime resolver — serverless env vars &gt; k8s &gt; cloud_detect IaaS ([afbc007](https://github.com/DexwoxBusiness/dexcost-sdk/commit/afbc007a5bd77b3b19dd5f50076b02cc9c1ff48d))
* **compute:** serverless handler wraps + init knobs ([3babf79](https://github.com/DexwoxBusiness/dexcost-sdk/commit/3babf798f0b026a5524c91ed6dc7b07793b76439))
* **gpu:** auto-emit dual events + back-fill cost at task finalize ([56d8d43](https://github.com/DexwoxBusiness/dexcost-sdk/commit/56d8d43bf458b2f2acc6abcf06a096c331fa1754))
* **gpu:** bundle initial gpu_prices.json across four SDKs from live 2026 sources ([79c8745](https://github.com/DexwoxBusiness/dexcost-sdk/commit/79c8745026f92740c5f83d7171080ce98cf81c30))
* **gpu:** cgroup-scope classifier — Decision [#1](https://github.com/DexwoxBusiness/dexcost-sdk/issues/1) verification-gate impl ([caebcf7](https://github.com/DexwoxBusiness/dexcost-sdk/commit/caebcf7280b9fd3cc3718673d2573b92d2fe990c))
* **gpu:** EventType.{GPU_COST,GPU_UTILIZATION_SIGNAL} + Task.gpu_cost_usd + v5→v6 migration ([2785158](https://github.com/DexwoxBusiness/dexcost-sdk/commit/278515848606df368f0924c4072557b90ca70f3a))
* **gpu:** NVML library wrapper — fail-silent + NFC-normalized productName ([b5424ea](https://github.com/DexwoxBusiness/dexcost-sdk/commit/b5424ea0563de78dd773842049084996071bf690))
* **gpu:** per-task accountant — cgroup walk + NVML snapshot pair + dual emission ([0d47371](https://github.com/DexwoxBusiness/dexcost-sdk/commit/0d47371eac087ed29d3ef81bfb4ac9ce878ccedd))
* **gpu:** pricing engine — 4 billing models + 5-tier ladder + device-class fallback ([a47c58a](https://github.com/DexwoxBusiness/dexcost-sdk/commit/a47c58adceb1c5146ac74d4f1e297598a22bf43b))
* **gpu:** runtime cascade — serverless env &gt; IaaS GPU family &gt; NVML presence ([9bcb0c9](https://github.com/DexwoxBusiness/dexcost-sdk/commit/9bcb0c961201ad3e8c30812df18379f6a7d88152))
* **gpu:** serverless handler wraps (Modal / RunPod / Replicate) + Task._gpu ([fc0860a](https://github.com/DexwoxBusiness/dexcost-sdk/commit/fc0860a3c5eb35a5083548d283eea524dcba63d3))
* implement compute, network, and GPU cost capture & attribution ([f56f42d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f56f42d49043eea2569ea062bf2fada5cc4d1f06))
* **network-cost-v2:** add Task.network_cost_usd field ([e9c8f8e](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e9c8f8ec4013e5a971a3705635620c97be7ef523))
* **network-cost-v2:** bundle egress price catalog (AWS/GCP/Azure) ([42d00d4](https://github.com/DexwoxBusiness/dexcost-sdk/commit/42d00d4b91459330acc78f8e8e494148f212283d))
* **network-cost-v2:** egress rate resolver + degradation ladder ([cb7079c](https://github.com/DexwoxBusiness/dexcost-sdk/commit/cb7079c968ee6ed2cbca1ff45de457e9b0c944fd))
* **network-cost-v2:** finalize-time egress pricing on tasks + events ([420c4c1](https://github.com/DexwoxBusiness/dexcost-sdk/commit/420c4c10e22cbd316720c88d34c4f3742ed73b4d))
* **network-cost-v2:** forward is_internal into accountant + deferred-cost marker ([c652bc3](https://github.com/DexwoxBusiness/dexcost-sdk/commit/c652bc39cd0606908fae2ad05d8491c6d3950b71))
* **network-cost-v2:** launch cloud detection from init() ([b381dd4](https://github.com/DexwoxBusiness/dexcost-sdk/commit/b381dd4c9add89ee88a47576cc135cee3f8e499d))
* **network-cost-v2:** NetworkAccountant external-byte split ([80da35b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/80da35b043ad5f6745575e17e26d222b7356165d))
* **network-cost-v2:** non-blocking cloud provider/region detection ([ca3574d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ca3574dcc519a1e7d3b77ccca53381a3be3fc199))
* **network-cost-v2:** persist network_cost_usd + v4-&gt;v5 migration ([1166e51](https://github.com/DexwoxBusiness/dexcost-sdk/commit/1166e513ab571cc7583f7ee7b905b5bfd6590694))
* **network:** add context-scoped network-event suppression flag ([316df6a](https://github.com/DexwoxBusiness/dexcost-sdk/commit/316df6a53f7e06b80351c852178537fc961e89e2))
* **network:** add destination classifier + byte measurement helpers ([45d7bbd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/45d7bbd452d9cc28a500c7650c8b1183807f46e5))
* **network:** add network event type ([6cce486](https://github.com/DexwoxBusiness/dexcost-sdk/commit/6cce4864d2d376219bcb182e1993190cbcf2e817))
* **network:** add network fields to Task model ([b19da30](https://github.com/DexwoxBusiness/dexcost-sdk/commit/b19da30f6ce7de7150b659eeb0480cdd42eddc37))
* **network:** add network-capture config fields ([1486d11](https://github.com/DexwoxBusiness/dexcost-sdk/commit/1486d11fca86b57aa30b4cea3f9f95a8a020b65f))
* **network:** add NetworkAccountant accumulator ([7834120](https://github.com/DexwoxBusiness/dexcost-sdk/commit/7834120d8fc7089abf360ae32661ccd5b205f80a))
* **network:** attach accountant to Task, finalize at task end ([9533942](https://github.com/DexwoxBusiness/dexcost-sdk/commit/95339422af7bd2e938b1d7342d08fb3c2adc08ce))
* **network:** HTTP adapter byte accounting + re-typed un-cataloged calls ([e0bc666](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e0bc666645762a454eb6b7e44903722741c932b7))
* **network:** LLM instruments suppress duplicate network events ([8438780](https://github.com/DexwoxBusiness/dexcost-sdk/commit/843878096a0a039a25fa0f66154341b403a3f667))
* **network:** persist network fields + v3-&gt;v4 migration ([fb0de71](https://github.com/DexwoxBusiness/dexcost-sdk/commit/fb0de71ce3dbfc939566bd245b028a92f8bea0f3))
* **network:** wire network-capture config through init() ([9089154](https://github.com/DexwoxBusiness/dexcost-sdk/commit/9089154302d49daaa6eb76c08edf2bff3097edc1))
* **security:** scrub_url across all 4 SDKs (Sprint 1 Theme A, part 1) ([07d1097](https://github.com/DexwoxBusiness/dexcost-sdk/commit/07d10977eebcd77b16e409f9781058e80a5a46ce))
* **security:** wire scrub_url into URL-capture call sites (Sprint 1 Theme A, part 2) ([56b4cf9](https://github.com/DexwoxBusiness/dexcost-sdk/commit/56b4cf9845c3ecb1a12ec07b75f69c5ee549a07d))


### Bug Fixes

* **all:** B14 — public set_api_key for auth-failure recovery across 4 SDKs (Sprint 2 Theme D / §3.2.3) ([bacfacd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/bacfacd2140427bedc036c799d183bf8907794b2))
* **all:** P1 — canonical timestamp serialisation (Sprint 3 Theme F / §4.1.1) ([03064a7](https://github.com/DexwoxBusiness/dexcost-sdk/commit/03064a7bab0ae461fce2fb6f99842945b32d6e8a))
* **all:** P3/P4/P5 — parity reconciliation (Sprint 3 Theme F / §4.1.3) ([d82407f](https://github.com/DexwoxBusiness/dexcost-sdk/commit/d82407f8f7ba9b77355ea695f94f6a33342b0597))
* **cloud_detect:** correctness audit — DMI fields, GCP region, OCI region ([a2fa928](https://github.com/DexwoxBusiness/dexcost-sdk/commit/a2fa9283096a86954f746b3c2529fc5fca94c137))
* **network:** aiohttp/urllib3 status_code, thread-safe error counter, lint cleanup ([28ae48d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/28ae48d6119ef26de23c2d64217675734d38cd05))
* **network:** dedupe _other bucket, clamp negative bytes, add accountant tests ([7aab6f4](https://github.com/DexwoxBusiness/dexcost-sdk/commit/7aab6f4d28d5891a40e803708246fa3581cf5720))
* **network:** honor track_network=False to disable byte capture and network events ([05d9cd4](https://github.com/DexwoxBusiness/dexcost-sdk/commit/05d9cd4344b6a8145ba9c27da797458ed7c0dca9))
* **network:** isolate init-wiring test instrumentation; document init() network params ([49a918e](https://github.com/DexwoxBusiness/dexcost-sdk/commit/49a918ee29d92336f6a6c46654c03a04c8e692fe))
* **network:** make _network non-init and copy-safe; cover failed-task finalize ([13c096d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/13c096d2a8759c5744e1f7449c67396e64231a28))
* **python,rust:** Sprint 3 Theme F mediums — high-impact items (§4.3) ([c1d87a7](https://github.com/DexwoxBusiness/dexcost-sdk/commit/c1d87a70af21b624aa45e39e9a4f7fd3a2a4a713))
* **python:** B10 — init() idempotency + fork safety (Sprint 1 Theme B / §2.2.4) ([9bb5d35](https://github.com/DexwoxBusiness/dexcost-sdk/commit/9bb5d35fc93381bc0c43690f3d60a16e5fa09e9c))
* **python:** B11 — HTTP adapter skips body drain on streaming responses (Sprint 2 Theme C / §3.1.2) ([8a2f357](https://github.com/DexwoxBusiness/dexcost-sdk/commit/8a2f35728bbe257c679b4248ec59e11757ea8a16))
* **python:** B2 — GPU SM-time integration (Sprint 2 Theme C / §3.1.1) ([d37b6b5](https://github.com/DexwoxBusiness/dexcost-sdk/commit/d37b6b576bdf863348e3c4090638e68e2c3d7814))
* **python:** compute math — memory.peak per-task + vcpu reset confidence (Sprint 2 Theme C / §3.1.3 fixes 1+2) ([a72b8d2](https://github.com/DexwoxBusiness/dexcost-sdk/commit/a72b8d2b0439c1e12e6dee50ffeac7ded1d36012))
* **security:** A2 — DEXCOST_ENDPOINT https-only allow-list across all 4 SDKs (Sprint 1 Theme A / §2.1) ([64bd3dd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/64bd3dd72bfde3ac475477765ecd22a09fa6f8f7))
* **storage:** re-mark sync_status='pending' on update_event ([ff96e94](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ff96e94ee6885f48019f956a95ce4c0539a07f11))
* testpypi workflow and fixes ([7f28512](https://github.com/DexwoxBusiness/dexcost-sdk/commit/7f28512309abba3ba8d974c60b929746a365b24c))
* **typescript:** clear all 119 lint errors ([f4d9679](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f4d967973a2ec3f69dd728c74e8acac88c1589ab))


### Documentation

* **conventions:** Phase 2 GPU updates — §1 signal-event carve-out, §3 patterns, §8 primitive ([d7d48b6](https://github.com/DexwoxBusiness/dexcost-sdk/commit/d7d48b659c823403ac0e20569992a77b93a43764))

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
