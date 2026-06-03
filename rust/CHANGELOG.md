# Changelog

## [0.3.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/rust/v0.2.0...rust/v0.3.0) (2026-06-03)


### ⚠ BREAKING CHANGES

* **sdk:** DEXCOST_ENDPOINT is no longer read. Configure a non-default endpoint via the in-code option instead. Pre-launch; no external consumers.

### Code Refactoring

* **sdk:** endpoint via explicit in-code config; drop DEXCOST_ENDPOINT env read ([0f4b397](https://github.com/DexwoxBusiness/dexcost-sdk/commit/0f4b39733320f3bd9848d83720a811a0a60467b0))

## [0.2.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/rust/v0.1.0...rust/v0.2.0) (2026-05-30)


### Features

* **compute-rust:** auto-emit + back-fill compute_cost events at task finalize ([a39ca58](https://github.com/DexwoxBusiness/dexcost-sdk/commit/a39ca58edfac5bdb600a0cf6d97e31e925a41a66))
* **compute-rust:** cgroup v2 file readers ([1028683](https://github.com/DexwoxBusiness/dexcost-sdk/commit/1028683029fc4227827d0f4a00508aca6d7ed228))
* **compute-rust:** extend CloudEnv with instance_type from IMDS (Decision [#3](https://github.com/DexwoxBusiness/dexcost-sdk/issues/3)) ([3b2ddc9](https://github.com/DexwoxBusiness/dexcost-sdk/commit/3b2ddc9d9afbc5457b331bafba2ae2e8e1f01c2e))
* **compute-rust:** Fargate ECS task metadata helper (MiB-&gt;bytes per Decision [#7](https://github.com/DexwoxBusiness/dexcost-sdk/issues/7)) ([735a47a](https://github.com/DexwoxBusiness/dexcost-sdk/commit/735a47a09962b8ccc4a697b555bcda2ed196e351))
* **compute-rust:** per-task accountant — cgroup start/end snapshots, single event ([ba7a2f7](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ba7a2f767efa52162baa4284af0980e7d6d25205))
* **compute-rust:** pricing engine + catalog integrity tests (Tasks 5 + 6) ([54be148](https://github.com/DexwoxBusiness/dexcost-sdk/commit/54be148c20df17ab041d3f0d644c4a1e463ec8e0))
* **compute-rust:** runtime resolver — serverless &gt; k8s &gt; cloud_detect IaaS ([413acf6](https://github.com/DexwoxBusiness/dexcost-sdk/commit/413acf696925f1f5fe185d729f01ce2752d6aac5))
* **compute-rust:** serverless handler wraps + config knobs ([5773c0a](https://github.com/DexwoxBusiness/dexcost-sdk/commit/5773c0a72cfc797a5162c6af635608d31abbec7e))
* Crates publish pipeline with proper readme and cargo updates ([0a8c61f](https://github.com/DexwoxBusiness/dexcost-sdk/commit/0a8c61fe4c3f0d2314e8f03cf3f9201627fffd36))
* **gpu-rust:** handler wraps + finalize back-fill + Task 9 + Task 10 ([c80f169](https://github.com/DexwoxBusiness/dexcost-sdk/commit/c80f1699d401cb3685bd0fc46d200b74b3982291))
* **gpu-rust:** Phase 2 foundation — Tasks 0+1+2+3+6 scaffold ([26f2f45](https://github.com/DexwoxBusiness/dexcost-sdk/commit/26f2f45e5c5e204a94c828aa00e3785090c866ca))
* **gpu-rust:** pricing engine + catalog integrity tests (Tasks 4+5) ([25ff17c](https://github.com/DexwoxBusiness/dexcost-sdk/commit/25ff17cf657c3409e3e142a437f293cbb0d6634e))
* **gpu:** bundle initial gpu_prices.json across four SDKs from live 2026 sources ([79c8745](https://github.com/DexwoxBusiness/dexcost-sdk/commit/79c8745026f92740c5f83d7171080ce98cf81c30))
* implement compute, network, and GPU cost capture & attribution ([f56f42d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f56f42d49043eea2569ea062bf2fada5cc4d1f06))
* **network:** _netbytes helpers — classifier + byte measurement ([c7722b3](https://github.com/DexwoxBusiness/dexcost-sdk/commit/c7722b3e4c93c9324f69f156e95628f538919dcf))
* **network,rust:** cloud_detect — non-blocking provider/region detection ([d8a3415](https://github.com/DexwoxBusiness/dexcost-sdk/commit/d8a3415981043bbc78fcd4e1a7e9073fed7ac9c1))
* **network,rust:** context-scoped network-event suppression flag ([77ff7b9](https://github.com/DexwoxBusiness/dexcost-sdk/commit/77ff7b9ff80d425bc86b57d0934358b8486c6e68))
* **network,rust:** egress pricing engine — 5-tier degradation ladder ([61a929d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/61a929d6aa427f56cffb432de2eda172e41c95c7))
* **network,rust:** NetworkAccountant — per-task byte-usage accumulator ([5b904f0](https://github.com/DexwoxBusiness/dexcost-sdk/commit/5b904f066a60f5450f9c64963a196fff4b4d6823))
* **network,rust:** stream SSE response bodies instead of buffering ([aa7501a](https://github.com/DexwoxBusiness/dexcost-sdk/commit/aa7501ae6f4d03ead029c07c670100341e1a0fd3))
* **network,rust:** task finalize — v2 egress pricing + per-event back-fill ([722d776](https://github.com/DexwoxBusiness/dexcost-sdk/commit/722d7764ac7b7fa9c1955fa66944d0bc2ad09d7d))
* **network,rust:** wire byte accounting + network event emission into middleware ([e8bb304](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e8bb304cca98c39c39556a61573bb5aaa369acf1))
* **network:** add four network fields to Task ([7052d33](https://github.com/DexwoxBusiness/dexcost-sdk/commit/7052d3349954ad7999245416389996374a7e2d47))
* **network:** add network event type ([8994e30](https://github.com/DexwoxBusiness/dexcost-sdk/commit/8994e302142d7ff7ce6ab1fa0db98c4e31e26cac))
* **security:** scrub_url across all 4 SDKs (Sprint 1 Theme A, part 1) ([07d1097](https://github.com/DexwoxBusiness/dexcost-sdk/commit/07d10977eebcd77b16e409f9781058e80a5a46ce))
* **security:** wire scrub_url into URL-capture call sites (Sprint 1 Theme A, part 2) ([56b4cf9](https://github.com/DexwoxBusiness/dexcost-sdk/commit/56b4cf9845c3ecb1a12ec07b75f69c5ee549a07d))


### Bug Fixes

* **all:** B14 — public set_api_key for auth-failure recovery across 4 SDKs (Sprint 2 Theme D / §3.2.3) ([bacfacd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/bacfacd2140427bedc036c799d183bf8907794b2))
* **all:** P1 — canonical timestamp serialisation (Sprint 3 Theme F / §4.1.1) ([03064a7](https://github.com/DexwoxBusiness/dexcost-sdk/commit/03064a7bab0ae461fce2fb6f99842945b32d6e8a))
* **all:** P2 — sync LLM cost maps across 4 SDKs + drift CI check (Sprint 3 Theme F / §4.1.2) ([2ce299f](https://github.com/DexwoxBusiness/dexcost-sdk/commit/2ce299f48d21e0d13834dd672dbd7df04ffae5d4))
* **all:** P3/P4/P5 — parity reconciliation (Sprint 3 Theme F / §4.1.3) ([d82407f](https://github.com/DexwoxBusiness/dexcost-sdk/commit/d82407f8f7ba9b77355ea695f94f6a33342b0597))
* **go,ts,rust:** A3 unbounded growth caps (Sprint 4 §5.2) ([da80181](https://github.com/DexwoxBusiness/dexcost-sdk/commit/da80181eefe2578dba7a11d777ad8ac787c73407))
* **python,rust:** Sprint 3 Theme F mediums — high-impact items (§4.3) ([c1d87a7](https://github.com/DexwoxBusiness/dexcost-sdk/commit/c1d87a70af21b624aa45e39e9a4f7fd3a2a4a713))
* **rust:** B2 — pin GPU SM-time integration formula (Sprint 2 Theme C / §3.1.1 Rust cross-SDK closeout) ([98ea95b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/98ea95b8660d8b8f8595e648a6e09dd55a3be36e))
* **rust:** B5 — canonical billing_model discriminators (drop _share suffix) (Sprint 1 Theme F / §2.3.1) ([2b0d471](https://github.com/DexwoxBusiness/dexcost-sdk/commit/2b0d471e2a877e317860cc10c8304aca9a182497))
* **rust:** B5b — IaaS share-math now matches Python canonical (Sprint 1 Theme F follow-on) ([152e9bd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/152e9bd312214cc35f4c021fa22947ab36f1360f))
* **rust:** crash-prevention — B4 async panic + §2.2.6 poisoned RwLock (Sprint 1 Theme B) ([5257bd6](https://github.com/DexwoxBusiness/dexcost-sdk/commit/5257bd6e8e01265932f989d4cbd8f2fcfd080702))
* **rust:** total_cost_usd 5-subsystem aggregation (Sprint 2 Theme C / §3.1.3 fix 5) ([80b1482](https://github.com/DexwoxBusiness/dexcost-sdk/commit/80b1482d60cb0df5a4ea90d7b79c16a9a67c2eea))
* **security:** A2 — DEXCOST_ENDPOINT https-only allow-list across all 4 SDKs (Sprint 1 Theme A / §2.1) ([64bd3dd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/64bd3dd72bfde3ac475477765ecd22a09fa6f8f7))
* **typescript,rust:** B12 — pusher partial-success accounting (Sprint 2 Theme D / §3.2.1) ([45ad099](https://github.com/DexwoxBusiness/dexcost-sdk/commit/45ad099cd6dc936396d3714e5dc21ece657040b1))
* **typescript:** clear all 119 lint errors ([f4d9679](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f4d967973a2ec3f69dd728c74e8acac88c1589ab))
