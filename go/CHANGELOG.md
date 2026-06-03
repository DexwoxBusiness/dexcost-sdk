# Changelog

## [0.2.1](https://github.com/DexwoxBusiness/dexcost-sdk/compare/go/v0.2.0...go/v0.2.1) (2026-06-03)


### Bug Fixes

* **sdk:** cross-SDK canonical serialization parity (decimals, fields, round-trip) ([ae2e40c](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ae2e40c95d1be0034c752f9b5b9ff35b17f78996))
* **sdk:** cross-SDK canonical serialization parity (decimals, fields,… ([f7e156b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f7e156bd080dd9b363e2cd1eb74134e8c5b39be7))

## [0.2.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/go/v0.1.0...go/v0.2.0) (2026-06-03)


### ⚠ BREAKING CHANGES

* **sdk:** DEXCOST_ENDPOINT is no longer read. Configure a non-default endpoint via the in-code option instead. Pre-launch; no external consumers.

### Code Refactoring

* **sdk:** endpoint via explicit in-code config; drop DEXCOST_ENDPOINT env read ([0f4b397](https://github.com/DexwoxBusiness/dexcost-sdk/commit/0f4b39733320f3bd9848d83720a811a0a60467b0))

## [0.1.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/go/v0.1.0...go/v0.1.0) (2026-06-03)


### ⚠ BREAKING CHANGES

* **sdk:** DEXCOST_ENDPOINT is no longer read. Configure a non-default endpoint via the in-code option instead. Pre-launch; no external consumers.

### Code Refactoring

* **sdk:** endpoint via explicit in-code config; drop DEXCOST_ENDPOINT env read ([0f4b397](https://github.com/DexwoxBusiness/dexcost-sdk/commit/0f4b39733320f3bd9848d83720a811a0a60467b0))

## [0.1.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/go/v0.1.0...go/v0.1.0) (2026-06-03)


### ⚠ BREAKING CHANGES

* **sdk:** DEXCOST_ENDPOINT is no longer read. Configure a non-default endpoint via the in-code option instead. Pre-launch; no external consumers.

### Code Refactoring

* **sdk:** endpoint via explicit in-code config; drop DEXCOST_ENDPOINT env read ([0f4b397](https://github.com/DexwoxBusiness/dexcost-sdk/commit/0f4b39733320f3bd9848d83720a811a0a60467b0))

## 0.1.0 (2026-05-31)


### Features

* **compute-go:** auto-emit + back-fill compute_cost events at task finalize ([c90068a](https://github.com/DexwoxBusiness/dexcost-sdk/commit/c90068a824a3420f35b7aaa9a7add5f62f520527))
* **compute-go:** cgroup v2 file readers ([6432157](https://github.com/DexwoxBusiness/dexcost-sdk/commit/64321572baead5561f389b45e87ef30adaa39d2a))
* **compute-go:** extend CloudEnv with InstanceType from IMDS (Decision [#3](https://github.com/DexwoxBusiness/dexcost-sdk/issues/3)) ([c593076](https://github.com/DexwoxBusiness/dexcost-sdk/commit/c593076f948820c1ec57573a79fe8d0c2b457194))
* **compute-go:** Fargate ECS task metadata helper (MiB-&gt;bytes per Decision [#7](https://github.com/DexwoxBusiness/dexcost-sdk/issues/7)) ([7669aeb](https://github.com/DexwoxBusiness/dexcost-sdk/commit/7669aeb08c8675f25d5eb3df21e4a31a335be7a4))
* **compute-go:** per-task accountant — cgroup start/end snapshots, single event ([0b8ce71](https://github.com/DexwoxBusiness/dexcost-sdk/commit/0b8ce71cda515ca05ca17b9e0ace1613c66887ad))
* **compute-go:** pricing engine — per-billing-model math + degradation ladder ([604d4f6](https://github.com/DexwoxBusiness/dexcost-sdk/commit/604d4f61915c0384c6a39c975e0465cce00f640e))
* **compute-go:** runtime resolver — serverless &gt; k8s &gt; cloud_detect IaaS ([e1e3b8a](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e1e3b8a6cc060b6c37b141fcd56e76ee444ed57a))
* **compute-go:** serverless handler wraps + Options knobs ([a14f28c](https://github.com/DexwoxBusiness/dexcost-sdk/commit/a14f28ced90ced1493a1d26932c8843f5e17b3a2))
* **gpu-go:** cgroup-scope classifier — Decision [#1](https://github.com/DexwoxBusiness/dexcost-sdk/issues/1) verification-gate (Task 2) ([55e5e32](https://github.com/DexwoxBusiness/dexcost-sdk/commit/55e5e32dd003b96e4f025959c5f5a33ae12a5aec))
* **gpu-go:** EventType GPU values + Task.GpuCostUSD field (Task 0) ([28b7cdf](https://github.com/DexwoxBusiness/dexcost-sdk/commit/28b7cdf857eb5ae7f0bf33f46525debdc792d57d))
* **gpu-go:** finalizeGPU wired into tracker.aggregateCosts (Task 8) ([affeb53](https://github.com/DexwoxBusiness/dexcost-sdk/commit/affeb53ebbdcc32549b0fcdc530cc234c0012e59))
* **gpu-go:** GPU runtime resolver — serverless env &gt; IaaS family (Task 3) ([c30ba0c](https://github.com/DexwoxBusiness/dexcost-sdk/commit/c30ba0c5b81e45f906e1f8d19a8ccd71d89194a8))
* **gpu-go:** NVML library wrapper — pluggable backend + NFC normalization (Task 1) ([4c19658](https://github.com/DexwoxBusiness/dexcost-sdk/commit/4c196582802a2f258f5c3c68ae42354788cfc8a7))
* **gpu-go:** per-task accountant — cgroup walk + NVML snapshot pair (Task 6) ([853a41f](https://github.com/DexwoxBusiness/dexcost-sdk/commit/853a41feb2992b5c1bb38c897772c9ff818ffdb0))
* **gpu-go:** pricing engine — 4 billing models + 5-tier ladder (Task 5) ([e3ae7a3](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e3ae7a3254eb61d2538ecdcee148646c1c4a0431))
* **gpu-go:** serverless handler wraps (Modal / RunPod / Replicate) (Task 7) ([b56a932](https://github.com/DexwoxBusiness/dexcost-sdk/commit/b56a932c929f7f6eb6758a12385676cb940e1ac7))
* **gpu:** bundle initial gpu_prices.json across four SDKs from live 2026 sources ([79c8745](https://github.com/DexwoxBusiness/dexcost-sdk/commit/79c8745026f92740c5f83d7171080ce98cf81c30))
* implement compute, network, and GPU cost capture & attribution ([f56f42d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f56f42d49043eea2569ea062bf2fada5cc4d1f06))
* **network:** _netbytes helpers — classifier + byte measurement ([0522a46](https://github.com/DexwoxBusiness/dexcost-sdk/commit/0522a46b16e7f0175ead802b7bcfe307cd754dfc))
* **network,go:** cloud_detect — non-blocking provider/region detection ([70944a3](https://github.com/DexwoxBusiness/dexcost-sdk/commit/70944a3722003d014d95610081c370c5f459940e))
* **network,go:** context-scoped network-event suppression flag ([480b667](https://github.com/DexwoxBusiness/dexcost-sdk/commit/480b6674f830705b15e8f04a9bbc93d6085581e9))
* **network,go:** egress pricing engine — 5-tier degradation ladder ([6d84dc7](https://github.com/DexwoxBusiness/dexcost-sdk/commit/6d84dc7b8d2046724e134bdb19a50efb8bae1697))
* **network,go:** NetworkAccountant + registry — per-task byte-usage accumulator ([4c9c39b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/4c9c39b774c6e8a205b6a88a92cf73875a04296d))
* **network,go:** task finalize — v2 egress pricing + per-event back-fill ([d3a9c7a](https://github.com/DexwoxBusiness/dexcost-sdk/commit/d3a9c7a5e1fce44d91a92ce787d8f33676e3a6fd))
* **network,go:** wire byte accounting + network event emission into HTTP adapter ([69471b5](https://github.com/DexwoxBusiness/dexcost-sdk/commit/69471b57f2624d86a42d27c43ab88518914d18a9))
* **network:** add four network fields to Task ([475574c](https://github.com/DexwoxBusiness/dexcost-sdk/commit/475574c2819d1ab31669b3746c13e16d73702f62))
* **network:** add network event type ([f8901a0](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f8901a0c7aa0677091a36b472b8927cf546981f8))
* Pblishing go sdk with pipeline setup and proper imports ([e31ee59](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e31ee5998f90b1d6763ffde9da2776b8844e9fbb))
* **security:** scrub_url across all 4 SDKs (Sprint 1 Theme A, part 1) ([07d1097](https://github.com/DexwoxBusiness/dexcost-sdk/commit/07d10977eebcd77b16e409f9781058e80a5a46ce))
* **security:** wire scrub_url into URL-capture call sites (Sprint 1 Theme A, part 2) ([56b4cf9](https://github.com/DexwoxBusiness/dexcost-sdk/commit/56b4cf9845c3ecb1a12ec07b75f69c5ee549a07d))


### Bug Fixes

* **all:** B14 — public set_api_key for auth-failure recovery across 4 SDKs (Sprint 2 Theme D / §3.2.3) ([bacfacd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/bacfacd2140427bedc036c799d183bf8907794b2))
* **all:** P1 — canonical timestamp serialisation (Sprint 3 Theme F / §4.1.1) ([03064a7](https://github.com/DexwoxBusiness/dexcost-sdk/commit/03064a7bab0ae461fce2fb6f99842945b32d6e8a))
* **all:** P3/P4/P5 — parity reconciliation (Sprint 3 Theme F / §4.1.3) ([d82407f](https://github.com/DexwoxBusiness/dexcost-sdk/commit/d82407f8f7ba9b77355ea695f94f6a33342b0597))
* **go,ts,rust:** A3 unbounded growth caps (Sprint 4 §5.2) ([da80181](https://github.com/DexwoxBusiness/dexcost-sdk/commit/da80181eefe2578dba7a11d777ad8ac787c73407))
* **go/schema:** B6 — accept gpu_cost / gpu_utilization_signal events and network_cost_usd / gpu_cost_usd task fields (Sprint 1 Theme F / §2.3.2) ([79a9466](https://github.com/DexwoxBusiness/dexcost-sdk/commit/79a9466128c42bc957053265abd4dd1580d1e288))
* **go:** B12 — pusher partial-success accounting (Sprint 2 Theme D / §3.2.1) ([4d36271](https://github.com/DexwoxBusiness/dexcost-sdk/commit/4d36271c96dbbbe5fe9fbc2fe81f36158202001c))
* **go:** B13 — SessionManager race in GetOrCreateSessionForIdentity (Sprint 2 Theme D / §3.2.2) ([0b39458](https://github.com/DexwoxBusiness/dexcost-sdk/commit/0b39458a8e6b9b9906e53883a921d74d42d9e4eb))
* **go:** B2 — GPU SM-time integration (Sprint 2 Theme C / §3.1.1 Go port) ([bbe1133](https://github.com/DexwoxBusiness/dexcost-sdk/commit/bbe1133c3b3ce17cdaed102dd77479557f1ddb6e))
* **go:** B7-1a — mustTracker no longer panics; *TrackedTask methods nil-safe (Sprint 1 Theme B / §2.2.2 1a) ([3feb9f0](https://github.com/DexwoxBusiness/dexcost-sdk/commit/3feb9f0a80915fb0898cc7c4e124e462fc2dae94))
* **go:** crash-prevention — safego helper + RequireFromString panic + retry-engine bad-config (Sprint 1 Theme B) ([ee8777b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ee8777bdbf37099dc81fb8571790bba00d781162))
* **go:** Fargate vs ECS-EC2 disambiguation (Sprint 2 Theme C / §3.1.3 fix 3) ([8d8ebc5](https://github.com/DexwoxBusiness/dexcost-sdk/commit/8d8ebc52a9741b2f9adee7f915564d24609ee592))
* **security:** A2 — DEXCOST_ENDPOINT https-only allow-list across all 4 SDKs (Sprint 1 Theme A / §2.1) ([64bd3dd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/64bd3dd72bfde3ac475477765ecd22a09fa6f8f7))
