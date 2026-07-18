# Changelog

## [0.15.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/typescript/v0.14.0...typescript/v0.15.0) (2026-07-18)


### Features

* **attribution:** capture observer billing dimensions across SDKs ([#95](https://github.com/DexwoxBusiness/dexcost-sdk/issues/95)) ([c4d7f71](https://github.com/DexwoxBusiness/dexcost-sdk/commit/c4d7f7104412b697d1c77c04ff89f30ff50aa95d))

## [0.14.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/typescript/v0.13.1...typescript/v0.14.0) (2026-07-18)


### Features

* **attribution:** observe unpriced service usage across SDKs ([#89](https://github.com/DexwoxBusiness/dexcost-sdk/issues/89)) ([93109e3](https://github.com/DexwoxBusiness/dexcost-sdk/commit/93109e348f4b339e0c532a5bed7e03a1be43ee40))

## [0.13.1](https://github.com/DexwoxBusiness/dexcost-sdk/compare/typescript/v0.13.0...typescript/v0.13.1) (2026-07-18)


### Bug Fixes

* **typescript:** preserve retry attribution delivery ([#80](https://github.com/DexwoxBusiness/dexcost-sdk/issues/80)) ([3e84dc5](https://github.com/DexwoxBusiness/dexcost-sdk/commit/3e84dc5858d7dae3032b85d6935ddd9068e60283))

## [0.13.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/typescript/v0.12.1...typescript/v0.13.0) (2026-07-17)


### Features

* **go:** add attribution v2 ingestion ([#74](https://github.com/DexwoxBusiness/dexcost-sdk/issues/74)) ([655931d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/655931d446ad1986f5e9b1aaeb93a32faf6524a4))

## [0.12.1](https://github.com/DexwoxBusiness/dexcost-sdk/compare/typescript/v0.12.0...typescript/v0.12.1) (2026-07-16)


### Bug Fixes

* **release:** recover skipped SDK releases ([#67](https://github.com/DexwoxBusiness/dexcost-sdk/issues/67)) ([466b332](https://github.com/DexwoxBusiness/dexcost-sdk/commit/466b332898be927be4d7aaa56544d9143fc4ffff))

## [0.12.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/typescript/v0.11.1...typescript/v0.12.0) (2026-07-16)


### Features

* **typescript:** emit attribution v2 events ([a9726f6](https://github.com/DexwoxBusiness/dexcost-sdk/commit/a9726f6f12219a53b17957de056263905635811c))
* **typescript:** emit attribution v2 events ([4d5ab69](https://github.com/DexwoxBusiness/dexcost-sdk/commit/4d5ab694aa14f607b14d865f46d6563c7eba3fea))


### Bug Fixes

* **typescript:** preserve infrastructure attribution ([0e71336](https://github.com/DexwoxBusiness/dexcost-sdk/commit/0e71336e0443a9a7300e789e0893299e02e2330e))

## [0.11.1](https://github.com/DexwoxBusiness/dexcost-sdk/compare/typescript/v0.11.0...typescript/v0.11.1) (2026-07-02)


### Bug Fixes

* **typescript:** never capture the SDK's own telemetry traffic ([7495be7](https://github.com/DexwoxBusiness/dexcost-sdk/commit/7495be7a87f174a7aa3c023ea7272cada0f1316a))

## [0.11.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/typescript/v0.10.0...typescript/v0.11.0) (2026-07-02)


### Features

* **typescript:** AI SDK middleware, dexcost doctor, debug mode, Fastify/Hono middleware ([05d5ef2](https://github.com/DexwoxBusiness/dexcost-sdk/commit/05d5ef25d3e2a4fc4a543d38988e49f7248133f2))
* **typescript:** OTel ingestion bridge and instrumentModules bundler escape hatch ([1ce22cf](https://github.com/DexwoxBusiness/dexcost-sdk/commit/1ce22cfa4903a92f59cf3d68d08667f2e0ca349d))
* **typescript:** P1 integration surface — sessions unified, NestJS, workers, injectable fetch, Gemini fallback, bun:sqlite ([97dde5a](https://github.com/DexwoxBusiness/dexcost-sdk/commit/97dde5a2c305ed4e456b700a55ab4c630944670a))


### Bug Fixes

* **typescript:** accept qualified callees in the dedup guardrail counter ([ce69b39](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ce69b397d5839973bbd6ce26eaffc5cddd9c0c90))
* **typescript:** Bun/Deno runtime support enforced by CI smoke tests ([af626d6](https://github.com/DexwoxBusiness/dexcost-sdk/commit/af626d615b0893f84ad311652ea9648a6faf0cab))
* **typescript:** count dedup registrations via TypeScript AST, not substring scan ([8ec8699](https://github.com/DexwoxBusiness/dexcost-sdk/commit/8ec869955413560875ee961632fa1b2fe7023fea))
* **typescript:** register dedup fingerprints at EVERY llm_call recording site ([e521cca](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e521ccaa1cfc229770e4abbc112af4f14823fbb2))
* **typescript:** resolve import bindings in the dedup guardrail counter ([3dc4e63](https://github.com/DexwoxBusiness/dexcost-sdk/commit/3dc4e63d7a5c03e55883b81c1e353ed0792c2404))

## [0.10.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/typescript/v0.9.0...typescript/v0.10.0) (2026-07-02)


### Features

* **typescript:** scoped runWithContext + kodus-ai ambient integration example ([6ca0afc](https://github.com/DexwoxBusiness/dexcost-sdk/commit/6ca0afc95a6d65f6a1d6f907a4a2306a66d863dd))


### Bug Fixes

* **typescript:** capture LLM calls on prefixed compatible endpoints and AI SDK v5+ ([e5ba7e2](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e5ba7e2bd4852f694ec3bb02267dd0a83bc06e38))
* **typescript:** gate network outcome on body-drain AND classification completion ([51d1c34](https://github.com/DexwoxBusiness/dexcost-sdk/commit/51d1c345a7117d5d4aee5d8f349e418a190c16fa))
* **typescript:** release the network-outcome gate for unparseable URLs ([c48c1e5](https://github.com/DexwoxBusiness/dexcost-sdk/commit/c48c1e5de10d53e7d7fa2d460a6e02b16f18bca1))
* **typescript:** stamp response bytes on JSON-path fallback llm_call events ([9fc0635](https://github.com/DexwoxBusiness/dexcost-sdk/commit/9fc06353d0504e741f40bf0e6b2e786e2d6c5b26))
* **typescript:** task lifecycle, session grouping, and network cost persistence ([9445621](https://github.com/DexwoxBusiness/dexcost-sdk/commit/944562151c0edf801b90846f81c8614b61ef4373))

## [0.9.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/typescript/v0.8.0...typescript/v0.9.0) (2026-07-01)


### ⚠ BREAKING CHANGES

* **sdk:** DEXCOST_ENDPOINT is no longer read. Configure a non-default endpoint via the in-code option instead. Pre-launch; no external consumers.

### Features

* **compute-ts:** auto-emit + back-fill compute_cost events at task finalize ([919b425](https://github.com/DexwoxBusiness/dexcost-sdk/commit/919b42548ca3ef7d5f66eb1691225033d086017f))
* **compute-ts:** cgroup v2 file readers (node-only, browser-safe) ([81bf7cb](https://github.com/DexwoxBusiness/dexcost-sdk/commit/81bf7cb465b53748c40f6e79f7925049e59c7a32))
* **compute-ts:** extend CloudEnv with instanceType from IMDS (Decision [#3](https://github.com/DexwoxBusiness/dexcost-sdk/issues/3)) ([1bf71fd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/1bf71fd3d9b2986354ecbaa87a6e10809dda3271))
* **compute-ts:** Fargate ECS task metadata helper ([eec21a6](https://github.com/DexwoxBusiness/dexcost-sdk/commit/eec21a6a375eeb69746a8a6c17d13d301649d4d1))
* **compute-ts:** per-task accountant — cgroup start/end snapshots, single event ([fb4de60](https://github.com/DexwoxBusiness/dexcost-sdk/commit/fb4de601f3ceda84a594b221cdafc52c7d97097d))
* **compute-ts:** pricing engine — per-billing-model math + degradation ladder ([546f80b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/546f80b38478d9e82501f32519c048ea454ee6e1))
* **compute-ts:** runtime resolver — serverless &gt; k8s &gt; cloud_detect IaaS ([0cecf38](https://github.com/DexwoxBusiness/dexcost-sdk/commit/0cecf3852b87c1b14bd481c62861a4af04fb6809))
* **compute-ts:** serverless handler wraps + Options knobs ([8693405](https://github.com/DexwoxBusiness/dexcost-sdk/commit/869340594823fdb0f6a4b2d234060deaaf2675bb))
* **gpu-ts:** auto-emit dual events + back-fill cost at task finalize ([251b453](https://github.com/DexwoxBusiness/dexcost-sdk/commit/251b45390658119f9c0ce5ba9b3fb03cdcd6207b))
* **gpu-ts:** cgroup-scope classifier — Decision [#1](https://github.com/DexwoxBusiness/dexcost-sdk/issues/1) verification gate ([20a1bac](https://github.com/DexwoxBusiness/dexcost-sdk/commit/20a1bac0d0555a495e672be088ce73ae9cde424e))
* **gpu-ts:** EventType.{gpu_cost,gpu_utilization_signal} + Task.gpuCostUsd ([81c0f9d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/81c0f9dc39a4630d7f836703fc681143a4ce453e))
* **gpu-ts:** GPU runtime cascade — serverless env &gt; IaaS family &gt; NVML ([ada3182](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ada3182f042f83410d46842dc90172694cf31a27))
* **gpu-ts:** NVML reader via nvidia-smi shell-out ([a9c6ee3](https://github.com/DexwoxBusiness/dexcost-sdk/commit/a9c6ee3ee20dc56dc760bd805c74db0eeb924bec))
* **gpu-ts:** per-task accountant — cgroup walk + NVML snapshot pair + dual emission ([958bd7b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/958bd7b8df36d375a3b3d92234541b62c65fd376))
* **gpu-ts:** pricing engine — 4 billing models + 5-tier ladder + device-class fallback ([1601e07](https://github.com/DexwoxBusiness/dexcost-sdk/commit/1601e07cfa8492c11dc85bc9f8d8666ea7d649dc))
* **gpu-ts:** serverless handler wraps (Modal / RunPod / Replicate) + Task._gpu ([abf5488](https://github.com/DexwoxBusiness/dexcost-sdk/commit/abf54882672f99edc28d36fe963a53560290fc15))
* **gpu:** bundle initial gpu_prices.json across four SDKs from live 2026 sources ([79c8745](https://github.com/DexwoxBusiness/dexcost-sdk/commit/79c8745026f92740c5f83d7171080ce98cf81c30))
* implement compute, network, and GPU cost capture & attribution ([f56f42d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f56f42d49043eea2569ea062bf2fada5cc4d1f06))
* **network:** _netbytes helpers — classifier + byte measurement ([42be7f8](https://github.com/DexwoxBusiness/dexcost-sdk/commit/42be7f8b45c588c7157869740c4e9a10c4bc571e))
* **network,ts:** cloud-detect — env / DMI / IMDS phases, init never blocks ([ec45eda](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ec45eda0af8044252ac38b30e33a8b35ca8fab78))
* **network,ts:** egress pricing engine — 5-tier degradation ladder ([7c631d6](https://github.com/DexwoxBusiness/dexcost-sdk/commit/7c631d6f48db3857fbffac8fd45d87c70fe4d160))
* **network,ts:** fetch patch — byte accounting + TransformStream + network events ([d458b6b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/d458b6baaf145ccead8159b9507ef7c578b9e065))
* **network,ts:** NetworkAccountant + registry — per-task byte-usage accumulator ([e3ad22a](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e3ad22af81e1a0c4c43e320021b89426b2671442))
* **network,ts:** task finalize — v2 egress pricing + per-event back-fill ([8269b05](https://github.com/DexwoxBusiness/dexcost-sdk/commit/8269b05089f43205c6b7bed493524c77367f014e))
* **network:** add four network fields to Task ([7ebfc40](https://github.com/DexwoxBusiness/dexcost-sdk/commit/7ebfc402ea0d51a2b0366be19338906e8f8eab3b))
* **network:** add network event type ([47c5b45](https://github.com/DexwoxBusiness/dexcost-sdk/commit/47c5b4559267ed557a80c50f0553016f8ba12b1f))
* Publish pipeline added with addition of cpying jsons for attribution and removal of pnpm which is no longer needed ([78cbc89](https://github.com/DexwoxBusiness/dexcost-sdk/commit/78cbc8928bd9040c08a4f64753313100cc84361b))
* **security:** scrub_url across all 4 SDKs (Sprint 1 Theme A, part 1) ([07d1097](https://github.com/DexwoxBusiness/dexcost-sdk/commit/07d10977eebcd77b16e409f9781058e80a5a46ce))
* **security:** wire scrub_url into URL-capture call sites (Sprint 1 Theme A, part 2) ([56b4cf9](https://github.com/DexwoxBusiness/dexcost-sdk/commit/56b4cf9845c3ecb1a12ec07b75f69c5ee549a07d))
* **typescript:** ship dual ESM + CommonJS build ([3c7820d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/3c7820d6da83882924182d353cc996210ffbf04e))


### Bug Fixes

* **all:** B14 — public set_api_key for auth-failure recovery across 4 SDKs (Sprint 2 Theme D / §3.2.3) ([bacfacd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/bacfacd2140427bedc036c799d183bf8907794b2))
* **all:** P1 — canonical timestamp serialisation (Sprint 3 Theme F / §4.1.1) ([03064a7](https://github.com/DexwoxBusiness/dexcost-sdk/commit/03064a7bab0ae461fce2fb6f99842945b32d6e8a))
* **all:** P2 — sync LLM cost maps across 4 SDKs + drift CI check (Sprint 3 Theme F / §4.1.2) ([2ce299f](https://github.com/DexwoxBusiness/dexcost-sdk/commit/2ce299f48d21e0d13834dd672dbd7df04ffae5d4))
* **all:** P3/P4/P5 — parity reconciliation (Sprint 3 Theme F / §4.1.3) ([d82407f](https://github.com/DexwoxBusiness/dexcost-sdk/commit/d82407f8f7ba9b77355ea695f94f6a33342b0597))
* finalize all pending sessions on close/closeAsync ([c9781fd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/c9781fdc42199c06b7d818622d54d02523e03c31))
* finalize all pending sessions on close/closeAsync ([737bf9c](https://github.com/DexwoxBusiness/dexcost-sdk/commit/737bf9caf22bd8f5353ef4115803a0d1c31289ec))
* **go,ts,rust:** A3 unbounded growth caps (Sprint 4 §5.2) ([da80181](https://github.com/DexwoxBusiness/dexcost-sdk/commit/da80181eefe2578dba7a11d777ad8ac787c73407))
* **instruments:** patch both CJS and ESM module objects for vercel-ai ([c3b5462](https://github.com/DexwoxBusiness/dexcost-sdk/commit/c3b5462061f6f621305463840f6976980c5a774e))
* **instruments:** patch both CJS and ESM module objects for vercel-ai ([8a0b9fc](https://github.com/DexwoxBusiness/dexcost-sdk/commit/8a0b9fc1c02d2d1c890021f61294b31e1b40f421))
* **instruments:** patch both CJS and ESM module objects for vercel-ai ([9e586c5](https://github.com/DexwoxBusiness/dexcost-sdk/commit/9e586c5d91abab8e301ee603f9babc1eecfa0633))
* proper session lifecycle with explicit task.end() in all LLM instruments ([973a16e](https://github.com/DexwoxBusiness/dexcost-sdk/commit/973a16ed7ecb9aa3138c461217455db457fd67ff))
* **sdk:** cross-SDK canonical serialization parity (decimals, fields, round-trip) ([ae2e40c](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ae2e40c95d1be0034c752f9b5b9ff35b17f78996))
* **sdk:** cross-SDK canonical serialization parity (decimals, fields,… ([f7e156b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f7e156bd080dd9b363e2cd1eb74134e8c5b39be7))
* **security:** A2 — DEXCOST_ENDPOINT https-only allow-list across all 4 SDKs (Sprint 1 Theme A / §2.1) ([64bd3dd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/64bd3dd72bfde3ac475477765ecd22a09fa6f8f7))
* **ts-sdk,security:** route telemetry pusher through the HTTPS allow-list (was reading DEXCOST_ENDPOINT raw, leaking the Bearer key over http) ([facc1b2](https://github.com/DexwoxBusiness/dexcost-sdk/commit/facc1b2544d3cda78ace57708cda1e88ae26bd2a))
* **ts-sdk:** clone Decimal config instead of mutating global; guard toDecimal ([7c77c40](https://github.com/DexwoxBusiness/dexcost-sdk/commit/7c77c400596d0163004abe82de6bae23b8f110ab))
* **typescript,rust:** B12 — pusher partial-success accounting (Sprint 2 Theme D / §3.2.1) ([45ad099](https://github.com/DexwoxBusiness/dexcost-sdk/commit/45ad099cd6dc936396d3714e5dc21ece657040b1))
* **typescript:** B2 — GPU SM-time integration (Sprint 2 Theme C / §3.1.1 TS port) ([05a21bb](https://github.com/DexwoxBusiness/dexcost-sdk/commit/05a21bb7e1c3260aeef9cc03c13cae5387546a3e))
* **typescript:** B3 — Decimal-based cost accumulation (Sprint 2 Theme E / §3.3.1) ([e483cd2](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e483cd27d5d270c1a55c38801c63b988029ba805))
* **typescript:** B8 — graceful fallback when better-sqlite3 unavailable (Sprint 1 Theme B / §2.2.3) ([a6eb6db](https://github.com/DexwoxBusiness/dexcost-sdk/commit/a6eb6db0f8724b94103232aca43112275bab9fac))
* **typescript:** B8 follow-on — in-memory Map-based buffer with 10k FIFO cap (Sprint 1 Theme B / §2.2.3 stretch) ([b95e36a](https://github.com/DexwoxBusiness/dexcost-sdk/commit/b95e36aea5c2ef433e97bb9681031ceed99c2583))
* **typescript:** B9 — flush events on process exit (Sprint 2 Theme E / §3.3.2) ([2ea086d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/2ea086da2f8e7a1ce28ec30392d3137b2be56507))
* **typescript:** clear all 119 lint errors ([f4d9679](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f4d967973a2ec3f69dd728c74e8acac88c1589ab))
* **typescript:** clear all 119 lint errors ([11fbddb](https://github.com/DexwoxBusiness/dexcost-sdk/commit/11fbddb84f450d5c41800d6ac8dd45def0388eb9))
* **typescript:** clearer dev-mode and better-sqlite3 fallback messages ([5b89226](https://github.com/DexwoxBusiness/dexcost-sdk/commit/5b892266dc3d08dfcc761be5e08ca23205e5ee2a))
* **typescript:** runtime support — fetch double-patch + frozen http + Node 18 JSON loads (Sprint 3 Theme E / §4.2) ([e3ee9ed](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e3ee9edfb76eea507a7bb02ad06e2f7a2c03e3e5))
* **typescript:** silence instrument warnings for default providers ([1ad70c8](https://github.com/DexwoxBusiness/dexcost-sdk/commit/1ad70c83bb2dd24c3cea96bf3f5153a42bbbff68))


### Code Refactoring

* **sdk:** endpoint via explicit in-code config; drop DEXCOST_ENDPOINT env read ([0f4b397](https://github.com/DexwoxBusiness/dexcost-sdk/commit/0f4b39733320f3bd9848d83720a811a0a60467b0))

## [0.8.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/typescript/v0.7.0...typescript/v0.8.0) (2026-07-01)


### ⚠ BREAKING CHANGES

* **sdk:** DEXCOST_ENDPOINT is no longer read. Configure a non-default endpoint via the in-code option instead. Pre-launch; no external consumers.

### Features

* **compute-ts:** auto-emit + back-fill compute_cost events at task finalize ([919b425](https://github.com/DexwoxBusiness/dexcost-sdk/commit/919b42548ca3ef7d5f66eb1691225033d086017f))
* **compute-ts:** cgroup v2 file readers (node-only, browser-safe) ([81bf7cb](https://github.com/DexwoxBusiness/dexcost-sdk/commit/81bf7cb465b53748c40f6e79f7925049e59c7a32))
* **compute-ts:** extend CloudEnv with instanceType from IMDS (Decision [#3](https://github.com/DexwoxBusiness/dexcost-sdk/issues/3)) ([1bf71fd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/1bf71fd3d9b2986354ecbaa87a6e10809dda3271))
* **compute-ts:** Fargate ECS task metadata helper ([eec21a6](https://github.com/DexwoxBusiness/dexcost-sdk/commit/eec21a6a375eeb69746a8a6c17d13d301649d4d1))
* **compute-ts:** per-task accountant — cgroup start/end snapshots, single event ([fb4de60](https://github.com/DexwoxBusiness/dexcost-sdk/commit/fb4de601f3ceda84a594b221cdafc52c7d97097d))
* **compute-ts:** pricing engine — per-billing-model math + degradation ladder ([546f80b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/546f80b38478d9e82501f32519c048ea454ee6e1))
* **compute-ts:** runtime resolver — serverless &gt; k8s &gt; cloud_detect IaaS ([0cecf38](https://github.com/DexwoxBusiness/dexcost-sdk/commit/0cecf3852b87c1b14bd481c62861a4af04fb6809))
* **compute-ts:** serverless handler wraps + Options knobs ([8693405](https://github.com/DexwoxBusiness/dexcost-sdk/commit/869340594823fdb0f6a4b2d234060deaaf2675bb))
* **gpu-ts:** auto-emit dual events + back-fill cost at task finalize ([251b453](https://github.com/DexwoxBusiness/dexcost-sdk/commit/251b45390658119f9c0ce5ba9b3fb03cdcd6207b))
* **gpu-ts:** cgroup-scope classifier — Decision [#1](https://github.com/DexwoxBusiness/dexcost-sdk/issues/1) verification gate ([20a1bac](https://github.com/DexwoxBusiness/dexcost-sdk/commit/20a1bac0d0555a495e672be088ce73ae9cde424e))
* **gpu-ts:** EventType.{gpu_cost,gpu_utilization_signal} + Task.gpuCostUsd ([81c0f9d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/81c0f9dc39a4630d7f836703fc681143a4ce453e))
* **gpu-ts:** GPU runtime cascade — serverless env &gt; IaaS family &gt; NVML ([ada3182](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ada3182f042f83410d46842dc90172694cf31a27))
* **gpu-ts:** NVML reader via nvidia-smi shell-out ([a9c6ee3](https://github.com/DexwoxBusiness/dexcost-sdk/commit/a9c6ee3ee20dc56dc760bd805c74db0eeb924bec))
* **gpu-ts:** per-task accountant — cgroup walk + NVML snapshot pair + dual emission ([958bd7b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/958bd7b8df36d375a3b3d92234541b62c65fd376))
* **gpu-ts:** pricing engine — 4 billing models + 5-tier ladder + device-class fallback ([1601e07](https://github.com/DexwoxBusiness/dexcost-sdk/commit/1601e07cfa8492c11dc85bc9f8d8666ea7d649dc))
* **gpu-ts:** serverless handler wraps (Modal / RunPod / Replicate) + Task._gpu ([abf5488](https://github.com/DexwoxBusiness/dexcost-sdk/commit/abf54882672f99edc28d36fe963a53560290fc15))
* **gpu:** bundle initial gpu_prices.json across four SDKs from live 2026 sources ([79c8745](https://github.com/DexwoxBusiness/dexcost-sdk/commit/79c8745026f92740c5f83d7171080ce98cf81c30))
* implement compute, network, and GPU cost capture & attribution ([f56f42d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f56f42d49043eea2569ea062bf2fada5cc4d1f06))
* **network:** _netbytes helpers — classifier + byte measurement ([42be7f8](https://github.com/DexwoxBusiness/dexcost-sdk/commit/42be7f8b45c588c7157869740c4e9a10c4bc571e))
* **network,ts:** cloud-detect — env / DMI / IMDS phases, init never blocks ([ec45eda](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ec45eda0af8044252ac38b30e33a8b35ca8fab78))
* **network,ts:** egress pricing engine — 5-tier degradation ladder ([7c631d6](https://github.com/DexwoxBusiness/dexcost-sdk/commit/7c631d6f48db3857fbffac8fd45d87c70fe4d160))
* **network,ts:** fetch patch — byte accounting + TransformStream + network events ([d458b6b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/d458b6baaf145ccead8159b9507ef7c578b9e065))
* **network,ts:** NetworkAccountant + registry — per-task byte-usage accumulator ([e3ad22a](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e3ad22af81e1a0c4c43e320021b89426b2671442))
* **network,ts:** task finalize — v2 egress pricing + per-event back-fill ([8269b05](https://github.com/DexwoxBusiness/dexcost-sdk/commit/8269b05089f43205c6b7bed493524c77367f014e))
* **network:** add four network fields to Task ([7ebfc40](https://github.com/DexwoxBusiness/dexcost-sdk/commit/7ebfc402ea0d51a2b0366be19338906e8f8eab3b))
* **network:** add network event type ([47c5b45](https://github.com/DexwoxBusiness/dexcost-sdk/commit/47c5b4559267ed557a80c50f0553016f8ba12b1f))
* Publish pipeline added with addition of cpying jsons for attribution and removal of pnpm which is no longer needed ([78cbc89](https://github.com/DexwoxBusiness/dexcost-sdk/commit/78cbc8928bd9040c08a4f64753313100cc84361b))
* **security:** scrub_url across all 4 SDKs (Sprint 1 Theme A, part 1) ([07d1097](https://github.com/DexwoxBusiness/dexcost-sdk/commit/07d10977eebcd77b16e409f9781058e80a5a46ce))
* **security:** wire scrub_url into URL-capture call sites (Sprint 1 Theme A, part 2) ([56b4cf9](https://github.com/DexwoxBusiness/dexcost-sdk/commit/56b4cf9845c3ecb1a12ec07b75f69c5ee549a07d))
* **typescript:** ship dual ESM + CommonJS build ([3c7820d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/3c7820d6da83882924182d353cc996210ffbf04e))


### Bug Fixes

* **all:** B14 — public set_api_key for auth-failure recovery across 4 SDKs (Sprint 2 Theme D / §3.2.3) ([bacfacd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/bacfacd2140427bedc036c799d183bf8907794b2))
* **all:** P1 — canonical timestamp serialisation (Sprint 3 Theme F / §4.1.1) ([03064a7](https://github.com/DexwoxBusiness/dexcost-sdk/commit/03064a7bab0ae461fce2fb6f99842945b32d6e8a))
* **all:** P2 — sync LLM cost maps across 4 SDKs + drift CI check (Sprint 3 Theme F / §4.1.2) ([2ce299f](https://github.com/DexwoxBusiness/dexcost-sdk/commit/2ce299f48d21e0d13834dd672dbd7df04ffae5d4))
* **all:** P3/P4/P5 — parity reconciliation (Sprint 3 Theme F / §4.1.3) ([d82407f](https://github.com/DexwoxBusiness/dexcost-sdk/commit/d82407f8f7ba9b77355ea695f94f6a33342b0597))
* finalize all pending sessions on close/closeAsync ([c9781fd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/c9781fdc42199c06b7d818622d54d02523e03c31))
* finalize all pending sessions on close/closeAsync ([737bf9c](https://github.com/DexwoxBusiness/dexcost-sdk/commit/737bf9caf22bd8f5353ef4115803a0d1c31289ec))
* **go,ts,rust:** A3 unbounded growth caps (Sprint 4 §5.2) ([da80181](https://github.com/DexwoxBusiness/dexcost-sdk/commit/da80181eefe2578dba7a11d777ad8ac787c73407))
* **instruments:** patch both CJS and ESM module objects for vercel-ai ([c3b5462](https://github.com/DexwoxBusiness/dexcost-sdk/commit/c3b5462061f6f621305463840f6976980c5a774e))
* **instruments:** patch both CJS and ESM module objects for vercel-ai ([8a0b9fc](https://github.com/DexwoxBusiness/dexcost-sdk/commit/8a0b9fc1c02d2d1c890021f61294b31e1b40f421))
* **instruments:** patch both CJS and ESM module objects for vercel-ai ([9e586c5](https://github.com/DexwoxBusiness/dexcost-sdk/commit/9e586c5d91abab8e301ee603f9babc1eecfa0633))
* proper session lifecycle with explicit task.end() in all LLM instruments ([973a16e](https://github.com/DexwoxBusiness/dexcost-sdk/commit/973a16ed7ecb9aa3138c461217455db457fd67ff))
* **sdk:** cross-SDK canonical serialization parity (decimals, fields, round-trip) ([ae2e40c](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ae2e40c95d1be0034c752f9b5b9ff35b17f78996))
* **sdk:** cross-SDK canonical serialization parity (decimals, fields,… ([f7e156b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f7e156bd080dd9b363e2cd1eb74134e8c5b39be7))
* **security:** A2 — DEXCOST_ENDPOINT https-only allow-list across all 4 SDKs (Sprint 1 Theme A / §2.1) ([64bd3dd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/64bd3dd72bfde3ac475477765ecd22a09fa6f8f7))
* **ts-sdk,security:** route telemetry pusher through the HTTPS allow-list (was reading DEXCOST_ENDPOINT raw, leaking the Bearer key over http) ([facc1b2](https://github.com/DexwoxBusiness/dexcost-sdk/commit/facc1b2544d3cda78ace57708cda1e88ae26bd2a))
* **ts-sdk:** clone Decimal config instead of mutating global; guard toDecimal ([7c77c40](https://github.com/DexwoxBusiness/dexcost-sdk/commit/7c77c400596d0163004abe82de6bae23b8f110ab))
* **typescript,rust:** B12 — pusher partial-success accounting (Sprint 2 Theme D / §3.2.1) ([45ad099](https://github.com/DexwoxBusiness/dexcost-sdk/commit/45ad099cd6dc936396d3714e5dc21ece657040b1))
* **typescript:** B2 — GPU SM-time integration (Sprint 2 Theme C / §3.1.1 TS port) ([05a21bb](https://github.com/DexwoxBusiness/dexcost-sdk/commit/05a21bb7e1c3260aeef9cc03c13cae5387546a3e))
* **typescript:** B3 — Decimal-based cost accumulation (Sprint 2 Theme E / §3.3.1) ([e483cd2](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e483cd27d5d270c1a55c38801c63b988029ba805))
* **typescript:** B8 — graceful fallback when better-sqlite3 unavailable (Sprint 1 Theme B / §2.2.3) ([a6eb6db](https://github.com/DexwoxBusiness/dexcost-sdk/commit/a6eb6db0f8724b94103232aca43112275bab9fac))
* **typescript:** B8 follow-on — in-memory Map-based buffer with 10k FIFO cap (Sprint 1 Theme B / §2.2.3 stretch) ([b95e36a](https://github.com/DexwoxBusiness/dexcost-sdk/commit/b95e36aea5c2ef433e97bb9681031ceed99c2583))
* **typescript:** B9 — flush events on process exit (Sprint 2 Theme E / §3.3.2) ([2ea086d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/2ea086da2f8e7a1ce28ec30392d3137b2be56507))
* **typescript:** clear all 119 lint errors ([f4d9679](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f4d967973a2ec3f69dd728c74e8acac88c1589ab))
* **typescript:** clear all 119 lint errors ([11fbddb](https://github.com/DexwoxBusiness/dexcost-sdk/commit/11fbddb84f450d5c41800d6ac8dd45def0388eb9))
* **typescript:** clearer dev-mode and better-sqlite3 fallback messages ([5b89226](https://github.com/DexwoxBusiness/dexcost-sdk/commit/5b892266dc3d08dfcc761be5e08ca23205e5ee2a))
* **typescript:** runtime support — fetch double-patch + frozen http + Node 18 JSON loads (Sprint 3 Theme E / §4.2) ([e3ee9ed](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e3ee9edfb76eea507a7bb02ad06e2f7a2c03e3e5))
* **typescript:** silence instrument warnings for default providers ([1ad70c8](https://github.com/DexwoxBusiness/dexcost-sdk/commit/1ad70c83bb2dd24c3cea96bf3f5153a42bbbff68))


### Code Refactoring

* **sdk:** endpoint via explicit in-code config; drop DEXCOST_ENDPOINT env read ([0f4b397](https://github.com/DexwoxBusiness/dexcost-sdk/commit/0f4b39733320f3bd9848d83720a811a0a60467b0))

## [0.7.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/typescript/v0.6.0...typescript/v0.7.0) (2026-07-01)


### ⚠ BREAKING CHANGES

* **sdk:** DEXCOST_ENDPOINT is no longer read. Configure a non-default endpoint via the in-code option instead. Pre-launch; no external consumers.

### Features

* **compute-ts:** auto-emit + back-fill compute_cost events at task finalize ([919b425](https://github.com/DexwoxBusiness/dexcost-sdk/commit/919b42548ca3ef7d5f66eb1691225033d086017f))
* **compute-ts:** cgroup v2 file readers (node-only, browser-safe) ([81bf7cb](https://github.com/DexwoxBusiness/dexcost-sdk/commit/81bf7cb465b53748c40f6e79f7925049e59c7a32))
* **compute-ts:** extend CloudEnv with instanceType from IMDS (Decision [#3](https://github.com/DexwoxBusiness/dexcost-sdk/issues/3)) ([1bf71fd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/1bf71fd3d9b2986354ecbaa87a6e10809dda3271))
* **compute-ts:** Fargate ECS task metadata helper ([eec21a6](https://github.com/DexwoxBusiness/dexcost-sdk/commit/eec21a6a375eeb69746a8a6c17d13d301649d4d1))
* **compute-ts:** per-task accountant — cgroup start/end snapshots, single event ([fb4de60](https://github.com/DexwoxBusiness/dexcost-sdk/commit/fb4de601f3ceda84a594b221cdafc52c7d97097d))
* **compute-ts:** pricing engine — per-billing-model math + degradation ladder ([546f80b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/546f80b38478d9e82501f32519c048ea454ee6e1))
* **compute-ts:** runtime resolver — serverless &gt; k8s &gt; cloud_detect IaaS ([0cecf38](https://github.com/DexwoxBusiness/dexcost-sdk/commit/0cecf3852b87c1b14bd481c62861a4af04fb6809))
* **compute-ts:** serverless handler wraps + Options knobs ([8693405](https://github.com/DexwoxBusiness/dexcost-sdk/commit/869340594823fdb0f6a4b2d234060deaaf2675bb))
* **gpu-ts:** auto-emit dual events + back-fill cost at task finalize ([251b453](https://github.com/DexwoxBusiness/dexcost-sdk/commit/251b45390658119f9c0ce5ba9b3fb03cdcd6207b))
* **gpu-ts:** cgroup-scope classifier — Decision [#1](https://github.com/DexwoxBusiness/dexcost-sdk/issues/1) verification gate ([20a1bac](https://github.com/DexwoxBusiness/dexcost-sdk/commit/20a1bac0d0555a495e672be088ce73ae9cde424e))
* **gpu-ts:** EventType.{gpu_cost,gpu_utilization_signal} + Task.gpuCostUsd ([81c0f9d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/81c0f9dc39a4630d7f836703fc681143a4ce453e))
* **gpu-ts:** GPU runtime cascade — serverless env &gt; IaaS family &gt; NVML ([ada3182](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ada3182f042f83410d46842dc90172694cf31a27))
* **gpu-ts:** NVML reader via nvidia-smi shell-out ([a9c6ee3](https://github.com/DexwoxBusiness/dexcost-sdk/commit/a9c6ee3ee20dc56dc760bd805c74db0eeb924bec))
* **gpu-ts:** per-task accountant — cgroup walk + NVML snapshot pair + dual emission ([958bd7b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/958bd7b8df36d375a3b3d92234541b62c65fd376))
* **gpu-ts:** pricing engine — 4 billing models + 5-tier ladder + device-class fallback ([1601e07](https://github.com/DexwoxBusiness/dexcost-sdk/commit/1601e07cfa8492c11dc85bc9f8d8666ea7d649dc))
* **gpu-ts:** serverless handler wraps (Modal / RunPod / Replicate) + Task._gpu ([abf5488](https://github.com/DexwoxBusiness/dexcost-sdk/commit/abf54882672f99edc28d36fe963a53560290fc15))
* **gpu:** bundle initial gpu_prices.json across four SDKs from live 2026 sources ([79c8745](https://github.com/DexwoxBusiness/dexcost-sdk/commit/79c8745026f92740c5f83d7171080ce98cf81c30))
* implement compute, network, and GPU cost capture & attribution ([f56f42d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f56f42d49043eea2569ea062bf2fada5cc4d1f06))
* **network:** _netbytes helpers — classifier + byte measurement ([42be7f8](https://github.com/DexwoxBusiness/dexcost-sdk/commit/42be7f8b45c588c7157869740c4e9a10c4bc571e))
* **network,ts:** cloud-detect — env / DMI / IMDS phases, init never blocks ([ec45eda](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ec45eda0af8044252ac38b30e33a8b35ca8fab78))
* **network,ts:** egress pricing engine — 5-tier degradation ladder ([7c631d6](https://github.com/DexwoxBusiness/dexcost-sdk/commit/7c631d6f48db3857fbffac8fd45d87c70fe4d160))
* **network,ts:** fetch patch — byte accounting + TransformStream + network events ([d458b6b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/d458b6baaf145ccead8159b9507ef7c578b9e065))
* **network,ts:** NetworkAccountant + registry — per-task byte-usage accumulator ([e3ad22a](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e3ad22af81e1a0c4c43e320021b89426b2671442))
* **network,ts:** task finalize — v2 egress pricing + per-event back-fill ([8269b05](https://github.com/DexwoxBusiness/dexcost-sdk/commit/8269b05089f43205c6b7bed493524c77367f014e))
* **network:** add four network fields to Task ([7ebfc40](https://github.com/DexwoxBusiness/dexcost-sdk/commit/7ebfc402ea0d51a2b0366be19338906e8f8eab3b))
* **network:** add network event type ([47c5b45](https://github.com/DexwoxBusiness/dexcost-sdk/commit/47c5b4559267ed557a80c50f0553016f8ba12b1f))
* Publish pipeline added with addition of cpying jsons for attribution and removal of pnpm which is no longer needed ([78cbc89](https://github.com/DexwoxBusiness/dexcost-sdk/commit/78cbc8928bd9040c08a4f64753313100cc84361b))
* **security:** scrub_url across all 4 SDKs (Sprint 1 Theme A, part 1) ([07d1097](https://github.com/DexwoxBusiness/dexcost-sdk/commit/07d10977eebcd77b16e409f9781058e80a5a46ce))
* **security:** wire scrub_url into URL-capture call sites (Sprint 1 Theme A, part 2) ([56b4cf9](https://github.com/DexwoxBusiness/dexcost-sdk/commit/56b4cf9845c3ecb1a12ec07b75f69c5ee549a07d))
* **typescript:** ship dual ESM + CommonJS build ([3c7820d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/3c7820d6da83882924182d353cc996210ffbf04e))


### Bug Fixes

* **all:** B14 — public set_api_key for auth-failure recovery across 4 SDKs (Sprint 2 Theme D / §3.2.3) ([bacfacd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/bacfacd2140427bedc036c799d183bf8907794b2))
* **all:** P1 — canonical timestamp serialisation (Sprint 3 Theme F / §4.1.1) ([03064a7](https://github.com/DexwoxBusiness/dexcost-sdk/commit/03064a7bab0ae461fce2fb6f99842945b32d6e8a))
* **all:** P2 — sync LLM cost maps across 4 SDKs + drift CI check (Sprint 3 Theme F / §4.1.2) ([2ce299f](https://github.com/DexwoxBusiness/dexcost-sdk/commit/2ce299f48d21e0d13834dd672dbd7df04ffae5d4))
* **all:** P3/P4/P5 — parity reconciliation (Sprint 3 Theme F / §4.1.3) ([d82407f](https://github.com/DexwoxBusiness/dexcost-sdk/commit/d82407f8f7ba9b77355ea695f94f6a33342b0597))
* finalize all pending sessions on close/closeAsync ([c9781fd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/c9781fdc42199c06b7d818622d54d02523e03c31))
* finalize all pending sessions on close/closeAsync ([737bf9c](https://github.com/DexwoxBusiness/dexcost-sdk/commit/737bf9caf22bd8f5353ef4115803a0d1c31289ec))
* **go,ts,rust:** A3 unbounded growth caps (Sprint 4 §5.2) ([da80181](https://github.com/DexwoxBusiness/dexcost-sdk/commit/da80181eefe2578dba7a11d777ad8ac787c73407))
* **instruments:** patch both CJS and ESM module objects for vercel-ai ([c3b5462](https://github.com/DexwoxBusiness/dexcost-sdk/commit/c3b5462061f6f621305463840f6976980c5a774e))
* **instruments:** patch both CJS and ESM module objects for vercel-ai ([8a0b9fc](https://github.com/DexwoxBusiness/dexcost-sdk/commit/8a0b9fc1c02d2d1c890021f61294b31e1b40f421))
* **instruments:** patch both CJS and ESM module objects for vercel-ai ([9e586c5](https://github.com/DexwoxBusiness/dexcost-sdk/commit/9e586c5d91abab8e301ee603f9babc1eecfa0633))
* proper session lifecycle with explicit task.end() in all LLM instruments ([973a16e](https://github.com/DexwoxBusiness/dexcost-sdk/commit/973a16ed7ecb9aa3138c461217455db457fd67ff))
* **sdk:** cross-SDK canonical serialization parity (decimals, fields, round-trip) ([ae2e40c](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ae2e40c95d1be0034c752f9b5b9ff35b17f78996))
* **sdk:** cross-SDK canonical serialization parity (decimals, fields,… ([f7e156b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f7e156bd080dd9b363e2cd1eb74134e8c5b39be7))
* **security:** A2 — DEXCOST_ENDPOINT https-only allow-list across all 4 SDKs (Sprint 1 Theme A / §2.1) ([64bd3dd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/64bd3dd72bfde3ac475477765ecd22a09fa6f8f7))
* **ts-sdk,security:** route telemetry pusher through the HTTPS allow-list (was reading DEXCOST_ENDPOINT raw, leaking the Bearer key over http) ([facc1b2](https://github.com/DexwoxBusiness/dexcost-sdk/commit/facc1b2544d3cda78ace57708cda1e88ae26bd2a))
* **ts-sdk:** clone Decimal config instead of mutating global; guard toDecimal ([7c77c40](https://github.com/DexwoxBusiness/dexcost-sdk/commit/7c77c400596d0163004abe82de6bae23b8f110ab))
* **typescript,rust:** B12 — pusher partial-success accounting (Sprint 2 Theme D / §3.2.1) ([45ad099](https://github.com/DexwoxBusiness/dexcost-sdk/commit/45ad099cd6dc936396d3714e5dc21ece657040b1))
* **typescript:** B2 — GPU SM-time integration (Sprint 2 Theme C / §3.1.1 TS port) ([05a21bb](https://github.com/DexwoxBusiness/dexcost-sdk/commit/05a21bb7e1c3260aeef9cc03c13cae5387546a3e))
* **typescript:** B3 — Decimal-based cost accumulation (Sprint 2 Theme E / §3.3.1) ([e483cd2](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e483cd27d5d270c1a55c38801c63b988029ba805))
* **typescript:** B8 — graceful fallback when better-sqlite3 unavailable (Sprint 1 Theme B / §2.2.3) ([a6eb6db](https://github.com/DexwoxBusiness/dexcost-sdk/commit/a6eb6db0f8724b94103232aca43112275bab9fac))
* **typescript:** B8 follow-on — in-memory Map-based buffer with 10k FIFO cap (Sprint 1 Theme B / §2.2.3 stretch) ([b95e36a](https://github.com/DexwoxBusiness/dexcost-sdk/commit/b95e36aea5c2ef433e97bb9681031ceed99c2583))
* **typescript:** B9 — flush events on process exit (Sprint 2 Theme E / §3.3.2) ([2ea086d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/2ea086da2f8e7a1ce28ec30392d3137b2be56507))
* **typescript:** clear all 119 lint errors ([f4d9679](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f4d967973a2ec3f69dd728c74e8acac88c1589ab))
* **typescript:** clear all 119 lint errors ([11fbddb](https://github.com/DexwoxBusiness/dexcost-sdk/commit/11fbddb84f450d5c41800d6ac8dd45def0388eb9))
* **typescript:** clearer dev-mode and better-sqlite3 fallback messages ([5b89226](https://github.com/DexwoxBusiness/dexcost-sdk/commit/5b892266dc3d08dfcc761be5e08ca23205e5ee2a))
* **typescript:** runtime support — fetch double-patch + frozen http + Node 18 JSON loads (Sprint 3 Theme E / §4.2) ([e3ee9ed](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e3ee9edfb76eea507a7bb02ad06e2f7a2c03e3e5))
* **typescript:** silence instrument warnings for default providers ([1ad70c8](https://github.com/DexwoxBusiness/dexcost-sdk/commit/1ad70c83bb2dd24c3cea96bf3f5153a42bbbff68))


### Code Refactoring

* **sdk:** endpoint via explicit in-code config; drop DEXCOST_ENDPOINT env read ([0f4b397](https://github.com/DexwoxBusiness/dexcost-sdk/commit/0f4b39733320f3bd9848d83720a811a0a60467b0))

## [0.6.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/typescript/v0.5.0...typescript/v0.6.0) (2026-07-01)


### ⚠ BREAKING CHANGES

* **sdk:** DEXCOST_ENDPOINT is no longer read. Configure a non-default endpoint via the in-code option instead. Pre-launch; no external consumers.

### Features

* **compute-ts:** auto-emit + back-fill compute_cost events at task finalize ([919b425](https://github.com/DexwoxBusiness/dexcost-sdk/commit/919b42548ca3ef7d5f66eb1691225033d086017f))
* **compute-ts:** cgroup v2 file readers (node-only, browser-safe) ([81bf7cb](https://github.com/DexwoxBusiness/dexcost-sdk/commit/81bf7cb465b53748c40f6e79f7925049e59c7a32))
* **compute-ts:** extend CloudEnv with instanceType from IMDS (Decision [#3](https://github.com/DexwoxBusiness/dexcost-sdk/issues/3)) ([1bf71fd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/1bf71fd3d9b2986354ecbaa87a6e10809dda3271))
* **compute-ts:** Fargate ECS task metadata helper ([eec21a6](https://github.com/DexwoxBusiness/dexcost-sdk/commit/eec21a6a375eeb69746a8a6c17d13d301649d4d1))
* **compute-ts:** per-task accountant — cgroup start/end snapshots, single event ([fb4de60](https://github.com/DexwoxBusiness/dexcost-sdk/commit/fb4de601f3ceda84a594b221cdafc52c7d97097d))
* **compute-ts:** pricing engine — per-billing-model math + degradation ladder ([546f80b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/546f80b38478d9e82501f32519c048ea454ee6e1))
* **compute-ts:** runtime resolver — serverless &gt; k8s &gt; cloud_detect IaaS ([0cecf38](https://github.com/DexwoxBusiness/dexcost-sdk/commit/0cecf3852b87c1b14bd481c62861a4af04fb6809))
* **compute-ts:** serverless handler wraps + Options knobs ([8693405](https://github.com/DexwoxBusiness/dexcost-sdk/commit/869340594823fdb0f6a4b2d234060deaaf2675bb))
* **gpu-ts:** auto-emit dual events + back-fill cost at task finalize ([251b453](https://github.com/DexwoxBusiness/dexcost-sdk/commit/251b45390658119f9c0ce5ba9b3fb03cdcd6207b))
* **gpu-ts:** cgroup-scope classifier — Decision [#1](https://github.com/DexwoxBusiness/dexcost-sdk/issues/1) verification gate ([20a1bac](https://github.com/DexwoxBusiness/dexcost-sdk/commit/20a1bac0d0555a495e672be088ce73ae9cde424e))
* **gpu-ts:** EventType.{gpu_cost,gpu_utilization_signal} + Task.gpuCostUsd ([81c0f9d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/81c0f9dc39a4630d7f836703fc681143a4ce453e))
* **gpu-ts:** GPU runtime cascade — serverless env &gt; IaaS family &gt; NVML ([ada3182](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ada3182f042f83410d46842dc90172694cf31a27))
* **gpu-ts:** NVML reader via nvidia-smi shell-out ([a9c6ee3](https://github.com/DexwoxBusiness/dexcost-sdk/commit/a9c6ee3ee20dc56dc760bd805c74db0eeb924bec))
* **gpu-ts:** per-task accountant — cgroup walk + NVML snapshot pair + dual emission ([958bd7b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/958bd7b8df36d375a3b3d92234541b62c65fd376))
* **gpu-ts:** pricing engine — 4 billing models + 5-tier ladder + device-class fallback ([1601e07](https://github.com/DexwoxBusiness/dexcost-sdk/commit/1601e07cfa8492c11dc85bc9f8d8666ea7d649dc))
* **gpu-ts:** serverless handler wraps (Modal / RunPod / Replicate) + Task._gpu ([abf5488](https://github.com/DexwoxBusiness/dexcost-sdk/commit/abf54882672f99edc28d36fe963a53560290fc15))
* **gpu:** bundle initial gpu_prices.json across four SDKs from live 2026 sources ([79c8745](https://github.com/DexwoxBusiness/dexcost-sdk/commit/79c8745026f92740c5f83d7171080ce98cf81c30))
* implement compute, network, and GPU cost capture & attribution ([f56f42d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f56f42d49043eea2569ea062bf2fada5cc4d1f06))
* **network:** _netbytes helpers — classifier + byte measurement ([42be7f8](https://github.com/DexwoxBusiness/dexcost-sdk/commit/42be7f8b45c588c7157869740c4e9a10c4bc571e))
* **network,ts:** cloud-detect — env / DMI / IMDS phases, init never blocks ([ec45eda](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ec45eda0af8044252ac38b30e33a8b35ca8fab78))
* **network,ts:** egress pricing engine — 5-tier degradation ladder ([7c631d6](https://github.com/DexwoxBusiness/dexcost-sdk/commit/7c631d6f48db3857fbffac8fd45d87c70fe4d160))
* **network,ts:** fetch patch — byte accounting + TransformStream + network events ([d458b6b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/d458b6baaf145ccead8159b9507ef7c578b9e065))
* **network,ts:** NetworkAccountant + registry — per-task byte-usage accumulator ([e3ad22a](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e3ad22af81e1a0c4c43e320021b89426b2671442))
* **network,ts:** task finalize — v2 egress pricing + per-event back-fill ([8269b05](https://github.com/DexwoxBusiness/dexcost-sdk/commit/8269b05089f43205c6b7bed493524c77367f014e))
* **network:** add four network fields to Task ([7ebfc40](https://github.com/DexwoxBusiness/dexcost-sdk/commit/7ebfc402ea0d51a2b0366be19338906e8f8eab3b))
* **network:** add network event type ([47c5b45](https://github.com/DexwoxBusiness/dexcost-sdk/commit/47c5b4559267ed557a80c50f0553016f8ba12b1f))
* Publish pipeline added with addition of cpying jsons for attribution and removal of pnpm which is no longer needed ([78cbc89](https://github.com/DexwoxBusiness/dexcost-sdk/commit/78cbc8928bd9040c08a4f64753313100cc84361b))
* **security:** scrub_url across all 4 SDKs (Sprint 1 Theme A, part 1) ([07d1097](https://github.com/DexwoxBusiness/dexcost-sdk/commit/07d10977eebcd77b16e409f9781058e80a5a46ce))
* **security:** wire scrub_url into URL-capture call sites (Sprint 1 Theme A, part 2) ([56b4cf9](https://github.com/DexwoxBusiness/dexcost-sdk/commit/56b4cf9845c3ecb1a12ec07b75f69c5ee549a07d))
* **typescript:** ship dual ESM + CommonJS build ([3c7820d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/3c7820d6da83882924182d353cc996210ffbf04e))


### Bug Fixes

* **all:** B14 — public set_api_key for auth-failure recovery across 4 SDKs (Sprint 2 Theme D / §3.2.3) ([bacfacd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/bacfacd2140427bedc036c799d183bf8907794b2))
* **all:** P1 — canonical timestamp serialisation (Sprint 3 Theme F / §4.1.1) ([03064a7](https://github.com/DexwoxBusiness/dexcost-sdk/commit/03064a7bab0ae461fce2fb6f99842945b32d6e8a))
* **all:** P2 — sync LLM cost maps across 4 SDKs + drift CI check (Sprint 3 Theme F / §4.1.2) ([2ce299f](https://github.com/DexwoxBusiness/dexcost-sdk/commit/2ce299f48d21e0d13834dd672dbd7df04ffae5d4))
* **all:** P3/P4/P5 — parity reconciliation (Sprint 3 Theme F / §4.1.3) ([d82407f](https://github.com/DexwoxBusiness/dexcost-sdk/commit/d82407f8f7ba9b77355ea695f94f6a33342b0597))
* finalize all pending sessions on close/closeAsync ([c9781fd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/c9781fdc42199c06b7d818622d54d02523e03c31))
* finalize all pending sessions on close/closeAsync ([737bf9c](https://github.com/DexwoxBusiness/dexcost-sdk/commit/737bf9caf22bd8f5353ef4115803a0d1c31289ec))
* **go,ts,rust:** A3 unbounded growth caps (Sprint 4 §5.2) ([da80181](https://github.com/DexwoxBusiness/dexcost-sdk/commit/da80181eefe2578dba7a11d777ad8ac787c73407))
* **instruments:** patch both CJS and ESM module objects for vercel-ai ([c3b5462](https://github.com/DexwoxBusiness/dexcost-sdk/commit/c3b5462061f6f621305463840f6976980c5a774e))
* **instruments:** patch both CJS and ESM module objects for vercel-ai ([8a0b9fc](https://github.com/DexwoxBusiness/dexcost-sdk/commit/8a0b9fc1c02d2d1c890021f61294b31e1b40f421))
* **instruments:** patch both CJS and ESM module objects for vercel-ai ([9e586c5](https://github.com/DexwoxBusiness/dexcost-sdk/commit/9e586c5d91abab8e301ee603f9babc1eecfa0633))
* proper session lifecycle with explicit task.end() in all LLM instruments ([973a16e](https://github.com/DexwoxBusiness/dexcost-sdk/commit/973a16ed7ecb9aa3138c461217455db457fd67ff))
* **sdk:** cross-SDK canonical serialization parity (decimals, fields, round-trip) ([ae2e40c](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ae2e40c95d1be0034c752f9b5b9ff35b17f78996))
* **sdk:** cross-SDK canonical serialization parity (decimals, fields,… ([f7e156b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f7e156bd080dd9b363e2cd1eb74134e8c5b39be7))
* **security:** A2 — DEXCOST_ENDPOINT https-only allow-list across all 4 SDKs (Sprint 1 Theme A / §2.1) ([64bd3dd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/64bd3dd72bfde3ac475477765ecd22a09fa6f8f7))
* **ts-sdk,security:** route telemetry pusher through the HTTPS allow-list (was reading DEXCOST_ENDPOINT raw, leaking the Bearer key over http) ([facc1b2](https://github.com/DexwoxBusiness/dexcost-sdk/commit/facc1b2544d3cda78ace57708cda1e88ae26bd2a))
* **ts-sdk:** clone Decimal config instead of mutating global; guard toDecimal ([7c77c40](https://github.com/DexwoxBusiness/dexcost-sdk/commit/7c77c400596d0163004abe82de6bae23b8f110ab))
* **typescript,rust:** B12 — pusher partial-success accounting (Sprint 2 Theme D / §3.2.1) ([45ad099](https://github.com/DexwoxBusiness/dexcost-sdk/commit/45ad099cd6dc936396d3714e5dc21ece657040b1))
* **typescript:** B2 — GPU SM-time integration (Sprint 2 Theme C / §3.1.1 TS port) ([05a21bb](https://github.com/DexwoxBusiness/dexcost-sdk/commit/05a21bb7e1c3260aeef9cc03c13cae5387546a3e))
* **typescript:** B3 — Decimal-based cost accumulation (Sprint 2 Theme E / §3.3.1) ([e483cd2](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e483cd27d5d270c1a55c38801c63b988029ba805))
* **typescript:** B8 — graceful fallback when better-sqlite3 unavailable (Sprint 1 Theme B / §2.2.3) ([a6eb6db](https://github.com/DexwoxBusiness/dexcost-sdk/commit/a6eb6db0f8724b94103232aca43112275bab9fac))
* **typescript:** B8 follow-on — in-memory Map-based buffer with 10k FIFO cap (Sprint 1 Theme B / §2.2.3 stretch) ([b95e36a](https://github.com/DexwoxBusiness/dexcost-sdk/commit/b95e36aea5c2ef433e97bb9681031ceed99c2583))
* **typescript:** B9 — flush events on process exit (Sprint 2 Theme E / §3.3.2) ([2ea086d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/2ea086da2f8e7a1ce28ec30392d3137b2be56507))
* **typescript:** clear all 119 lint errors ([f4d9679](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f4d967973a2ec3f69dd728c74e8acac88c1589ab))
* **typescript:** clear all 119 lint errors ([11fbddb](https://github.com/DexwoxBusiness/dexcost-sdk/commit/11fbddb84f450d5c41800d6ac8dd45def0388eb9))
* **typescript:** clearer dev-mode and better-sqlite3 fallback messages ([5b89226](https://github.com/DexwoxBusiness/dexcost-sdk/commit/5b892266dc3d08dfcc761be5e08ca23205e5ee2a))
* **typescript:** runtime support — fetch double-patch + frozen http + Node 18 JSON loads (Sprint 3 Theme E / §4.2) ([e3ee9ed](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e3ee9edfb76eea507a7bb02ad06e2f7a2c03e3e5))
* **typescript:** silence instrument warnings for default providers ([1ad70c8](https://github.com/DexwoxBusiness/dexcost-sdk/commit/1ad70c83bb2dd24c3cea96bf3f5153a42bbbff68))


### Code Refactoring

* **sdk:** endpoint via explicit in-code config; drop DEXCOST_ENDPOINT env read ([0f4b397](https://github.com/DexwoxBusiness/dexcost-sdk/commit/0f4b39733320f3bd9848d83720a811a0a60467b0))

## [0.5.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/typescript/v0.4.3...typescript/v0.5.0) (2026-07-01)


### ⚠ BREAKING CHANGES

* **sdk:** DEXCOST_ENDPOINT is no longer read. Configure a non-default endpoint via the in-code option instead. Pre-launch; no external consumers.

### Features

* **compute-ts:** auto-emit + back-fill compute_cost events at task finalize ([919b425](https://github.com/DexwoxBusiness/dexcost-sdk/commit/919b42548ca3ef7d5f66eb1691225033d086017f))
* **compute-ts:** cgroup v2 file readers (node-only, browser-safe) ([81bf7cb](https://github.com/DexwoxBusiness/dexcost-sdk/commit/81bf7cb465b53748c40f6e79f7925049e59c7a32))
* **compute-ts:** extend CloudEnv with instanceType from IMDS (Decision [#3](https://github.com/DexwoxBusiness/dexcost-sdk/issues/3)) ([1bf71fd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/1bf71fd3d9b2986354ecbaa87a6e10809dda3271))
* **compute-ts:** Fargate ECS task metadata helper ([eec21a6](https://github.com/DexwoxBusiness/dexcost-sdk/commit/eec21a6a375eeb69746a8a6c17d13d301649d4d1))
* **compute-ts:** per-task accountant — cgroup start/end snapshots, single event ([fb4de60](https://github.com/DexwoxBusiness/dexcost-sdk/commit/fb4de601f3ceda84a594b221cdafc52c7d97097d))
* **compute-ts:** pricing engine — per-billing-model math + degradation ladder ([546f80b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/546f80b38478d9e82501f32519c048ea454ee6e1))
* **compute-ts:** runtime resolver — serverless &gt; k8s &gt; cloud_detect IaaS ([0cecf38](https://github.com/DexwoxBusiness/dexcost-sdk/commit/0cecf3852b87c1b14bd481c62861a4af04fb6809))
* **compute-ts:** serverless handler wraps + Options knobs ([8693405](https://github.com/DexwoxBusiness/dexcost-sdk/commit/869340594823fdb0f6a4b2d234060deaaf2675bb))
* **gpu-ts:** auto-emit dual events + back-fill cost at task finalize ([251b453](https://github.com/DexwoxBusiness/dexcost-sdk/commit/251b45390658119f9c0ce5ba9b3fb03cdcd6207b))
* **gpu-ts:** cgroup-scope classifier — Decision [#1](https://github.com/DexwoxBusiness/dexcost-sdk/issues/1) verification gate ([20a1bac](https://github.com/DexwoxBusiness/dexcost-sdk/commit/20a1bac0d0555a495e672be088ce73ae9cde424e))
* **gpu-ts:** EventType.{gpu_cost,gpu_utilization_signal} + Task.gpuCostUsd ([81c0f9d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/81c0f9dc39a4630d7f836703fc681143a4ce453e))
* **gpu-ts:** GPU runtime cascade — serverless env &gt; IaaS family &gt; NVML ([ada3182](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ada3182f042f83410d46842dc90172694cf31a27))
* **gpu-ts:** NVML reader via nvidia-smi shell-out ([a9c6ee3](https://github.com/DexwoxBusiness/dexcost-sdk/commit/a9c6ee3ee20dc56dc760bd805c74db0eeb924bec))
* **gpu-ts:** per-task accountant — cgroup walk + NVML snapshot pair + dual emission ([958bd7b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/958bd7b8df36d375a3b3d92234541b62c65fd376))
* **gpu-ts:** pricing engine — 4 billing models + 5-tier ladder + device-class fallback ([1601e07](https://github.com/DexwoxBusiness/dexcost-sdk/commit/1601e07cfa8492c11dc85bc9f8d8666ea7d649dc))
* **gpu-ts:** serverless handler wraps (Modal / RunPod / Replicate) + Task._gpu ([abf5488](https://github.com/DexwoxBusiness/dexcost-sdk/commit/abf54882672f99edc28d36fe963a53560290fc15))
* **gpu:** bundle initial gpu_prices.json across four SDKs from live 2026 sources ([79c8745](https://github.com/DexwoxBusiness/dexcost-sdk/commit/79c8745026f92740c5f83d7171080ce98cf81c30))
* implement compute, network, and GPU cost capture & attribution ([f56f42d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f56f42d49043eea2569ea062bf2fada5cc4d1f06))
* **network:** _netbytes helpers — classifier + byte measurement ([42be7f8](https://github.com/DexwoxBusiness/dexcost-sdk/commit/42be7f8b45c588c7157869740c4e9a10c4bc571e))
* **network,ts:** cloud-detect — env / DMI / IMDS phases, init never blocks ([ec45eda](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ec45eda0af8044252ac38b30e33a8b35ca8fab78))
* **network,ts:** egress pricing engine — 5-tier degradation ladder ([7c631d6](https://github.com/DexwoxBusiness/dexcost-sdk/commit/7c631d6f48db3857fbffac8fd45d87c70fe4d160))
* **network,ts:** fetch patch — byte accounting + TransformStream + network events ([d458b6b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/d458b6baaf145ccead8159b9507ef7c578b9e065))
* **network,ts:** NetworkAccountant + registry — per-task byte-usage accumulator ([e3ad22a](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e3ad22af81e1a0c4c43e320021b89426b2671442))
* **network,ts:** task finalize — v2 egress pricing + per-event back-fill ([8269b05](https://github.com/DexwoxBusiness/dexcost-sdk/commit/8269b05089f43205c6b7bed493524c77367f014e))
* **network:** add four network fields to Task ([7ebfc40](https://github.com/DexwoxBusiness/dexcost-sdk/commit/7ebfc402ea0d51a2b0366be19338906e8f8eab3b))
* **network:** add network event type ([47c5b45](https://github.com/DexwoxBusiness/dexcost-sdk/commit/47c5b4559267ed557a80c50f0553016f8ba12b1f))
* Publish pipeline added with addition of cpying jsons for attribution and removal of pnpm which is no longer needed ([78cbc89](https://github.com/DexwoxBusiness/dexcost-sdk/commit/78cbc8928bd9040c08a4f64753313100cc84361b))
* **security:** scrub_url across all 4 SDKs (Sprint 1 Theme A, part 1) ([07d1097](https://github.com/DexwoxBusiness/dexcost-sdk/commit/07d10977eebcd77b16e409f9781058e80a5a46ce))
* **security:** wire scrub_url into URL-capture call sites (Sprint 1 Theme A, part 2) ([56b4cf9](https://github.com/DexwoxBusiness/dexcost-sdk/commit/56b4cf9845c3ecb1a12ec07b75f69c5ee549a07d))
* **typescript:** ship dual ESM + CommonJS build ([3c7820d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/3c7820d6da83882924182d353cc996210ffbf04e))


### Bug Fixes

* **all:** B14 — public set_api_key for auth-failure recovery across 4 SDKs (Sprint 2 Theme D / §3.2.3) ([bacfacd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/bacfacd2140427bedc036c799d183bf8907794b2))
* **all:** P1 — canonical timestamp serialisation (Sprint 3 Theme F / §4.1.1) ([03064a7](https://github.com/DexwoxBusiness/dexcost-sdk/commit/03064a7bab0ae461fce2fb6f99842945b32d6e8a))
* **all:** P2 — sync LLM cost maps across 4 SDKs + drift CI check (Sprint 3 Theme F / §4.1.2) ([2ce299f](https://github.com/DexwoxBusiness/dexcost-sdk/commit/2ce299f48d21e0d13834dd672dbd7df04ffae5d4))
* **all:** P3/P4/P5 — parity reconciliation (Sprint 3 Theme F / §4.1.3) ([d82407f](https://github.com/DexwoxBusiness/dexcost-sdk/commit/d82407f8f7ba9b77355ea695f94f6a33342b0597))
* finalize all pending sessions on close/closeAsync ([c9781fd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/c9781fdc42199c06b7d818622d54d02523e03c31))
* finalize all pending sessions on close/closeAsync ([737bf9c](https://github.com/DexwoxBusiness/dexcost-sdk/commit/737bf9caf22bd8f5353ef4115803a0d1c31289ec))
* **go,ts,rust:** A3 unbounded growth caps (Sprint 4 §5.2) ([da80181](https://github.com/DexwoxBusiness/dexcost-sdk/commit/da80181eefe2578dba7a11d777ad8ac787c73407))
* **instruments:** patch both CJS and ESM module objects for vercel-ai ([c3b5462](https://github.com/DexwoxBusiness/dexcost-sdk/commit/c3b5462061f6f621305463840f6976980c5a774e))
* **instruments:** patch both CJS and ESM module objects for vercel-ai ([8a0b9fc](https://github.com/DexwoxBusiness/dexcost-sdk/commit/8a0b9fc1c02d2d1c890021f61294b31e1b40f421))
* **instruments:** patch both CJS and ESM module objects for vercel-ai ([9e586c5](https://github.com/DexwoxBusiness/dexcost-sdk/commit/9e586c5d91abab8e301ee603f9babc1eecfa0633))
* proper session lifecycle with explicit task.end() in all LLM instruments ([973a16e](https://github.com/DexwoxBusiness/dexcost-sdk/commit/973a16ed7ecb9aa3138c461217455db457fd67ff))
* **sdk:** cross-SDK canonical serialization parity (decimals, fields, round-trip) ([ae2e40c](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ae2e40c95d1be0034c752f9b5b9ff35b17f78996))
* **sdk:** cross-SDK canonical serialization parity (decimals, fields,… ([f7e156b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f7e156bd080dd9b363e2cd1eb74134e8c5b39be7))
* **security:** A2 — DEXCOST_ENDPOINT https-only allow-list across all 4 SDKs (Sprint 1 Theme A / §2.1) ([64bd3dd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/64bd3dd72bfde3ac475477765ecd22a09fa6f8f7))
* **ts-sdk,security:** route telemetry pusher through the HTTPS allow-list (was reading DEXCOST_ENDPOINT raw, leaking the Bearer key over http) ([facc1b2](https://github.com/DexwoxBusiness/dexcost-sdk/commit/facc1b2544d3cda78ace57708cda1e88ae26bd2a))
* **ts-sdk:** clone Decimal config instead of mutating global; guard toDecimal ([7c77c40](https://github.com/DexwoxBusiness/dexcost-sdk/commit/7c77c400596d0163004abe82de6bae23b8f110ab))
* **typescript,rust:** B12 — pusher partial-success accounting (Sprint 2 Theme D / §3.2.1) ([45ad099](https://github.com/DexwoxBusiness/dexcost-sdk/commit/45ad099cd6dc936396d3714e5dc21ece657040b1))
* **typescript:** B2 — GPU SM-time integration (Sprint 2 Theme C / §3.1.1 TS port) ([05a21bb](https://github.com/DexwoxBusiness/dexcost-sdk/commit/05a21bb7e1c3260aeef9cc03c13cae5387546a3e))
* **typescript:** B3 — Decimal-based cost accumulation (Sprint 2 Theme E / §3.3.1) ([e483cd2](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e483cd27d5d270c1a55c38801c63b988029ba805))
* **typescript:** B8 — graceful fallback when better-sqlite3 unavailable (Sprint 1 Theme B / §2.2.3) ([a6eb6db](https://github.com/DexwoxBusiness/dexcost-sdk/commit/a6eb6db0f8724b94103232aca43112275bab9fac))
* **typescript:** B8 follow-on — in-memory Map-based buffer with 10k FIFO cap (Sprint 1 Theme B / §2.2.3 stretch) ([b95e36a](https://github.com/DexwoxBusiness/dexcost-sdk/commit/b95e36aea5c2ef433e97bb9681031ceed99c2583))
* **typescript:** B9 — flush events on process exit (Sprint 2 Theme E / §3.3.2) ([2ea086d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/2ea086da2f8e7a1ce28ec30392d3137b2be56507))
* **typescript:** clear all 119 lint errors ([f4d9679](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f4d967973a2ec3f69dd728c74e8acac88c1589ab))
* **typescript:** clear all 119 lint errors ([11fbddb](https://github.com/DexwoxBusiness/dexcost-sdk/commit/11fbddb84f450d5c41800d6ac8dd45def0388eb9))
* **typescript:** clearer dev-mode and better-sqlite3 fallback messages ([5b89226](https://github.com/DexwoxBusiness/dexcost-sdk/commit/5b892266dc3d08dfcc761be5e08ca23205e5ee2a))
* **typescript:** runtime support — fetch double-patch + frozen http + Node 18 JSON loads (Sprint 3 Theme E / §4.2) ([e3ee9ed](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e3ee9edfb76eea507a7bb02ad06e2f7a2c03e3e5))
* **typescript:** silence instrument warnings for default providers ([1ad70c8](https://github.com/DexwoxBusiness/dexcost-sdk/commit/1ad70c83bb2dd24c3cea96bf3f5153a42bbbff68))


### Code Refactoring

* **sdk:** endpoint via explicit in-code config; drop DEXCOST_ENDPOINT env read ([0f4b397](https://github.com/DexwoxBusiness/dexcost-sdk/commit/0f4b39733320f3bd9848d83720a811a0a60467b0))

## [0.4.3](https://github.com/DexwoxBusiness/dexcost-sdk/compare/typescript/v0.4.2...typescript/v0.4.3) (2026-07-01)


### Bug Fixes

* finalize all pending sessions on close/closeAsync ([c9781fd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/c9781fdc42199c06b7d818622d54d02523e03c31))
* finalize all pending sessions on close/closeAsync ([737bf9c](https://github.com/DexwoxBusiness/dexcost-sdk/commit/737bf9caf22bd8f5353ef4115803a0d1c31289ec))

## [0.4.2](https://github.com/DexwoxBusiness/dexcost-sdk/compare/typescript/v0.4.1...typescript/v0.4.2) (2026-06-30)


### Bug Fixes

* proper session lifecycle with explicit task.end() in all LLM instruments ([973a16e](https://github.com/DexwoxBusiness/dexcost-sdk/commit/973a16ed7ecb9aa3138c461217455db457fd67ff))

## [0.4.1](https://github.com/DexwoxBusiness/dexcost-sdk/compare/typescript/v0.4.0...typescript/v0.4.1) (2026-06-30)


### Bug Fixes

* **instruments:** patch both CJS and ESM module objects for vercel-ai ([c3b5462](https://github.com/DexwoxBusiness/dexcost-sdk/commit/c3b5462061f6f621305463840f6976980c5a774e))
* **instruments:** patch both CJS and ESM module objects for vercel-ai ([8a0b9fc](https://github.com/DexwoxBusiness/dexcost-sdk/commit/8a0b9fc1c02d2d1c890021f61294b31e1b40f421))
* **instruments:** patch both CJS and ESM module objects for vercel-ai ([9e586c5](https://github.com/DexwoxBusiness/dexcost-sdk/commit/9e586c5d91abab8e301ee603f9babc1eecfa0633))

## [0.4.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/typescript/v0.3.2...typescript/v0.4.0) (2026-06-29)


### Features

* **typescript:** ship dual ESM + CommonJS build ([3c7820d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/3c7820d6da83882924182d353cc996210ffbf04e))


### Bug Fixes

* **typescript:** clearer dev-mode and better-sqlite3 fallback messages ([5b89226](https://github.com/DexwoxBusiness/dexcost-sdk/commit/5b892266dc3d08dfcc761be5e08ca23205e5ee2a))
* **typescript:** silence instrument warnings for default providers ([1ad70c8](https://github.com/DexwoxBusiness/dexcost-sdk/commit/1ad70c83bb2dd24c3cea96bf3f5153a42bbbff68))

## [0.3.2](https://github.com/DexwoxBusiness/dexcost-sdk/compare/typescript/v0.3.1...typescript/v0.3.2) (2026-06-04)


### Bug Fixes

* **ts-sdk:** clone Decimal config instead of mutating global; guard toDecimal ([7c77c40](https://github.com/DexwoxBusiness/dexcost-sdk/commit/7c77c400596d0163004abe82de6bae23b8f110ab))

## [0.3.1](https://github.com/DexwoxBusiness/dexcost-sdk/compare/typescript/v0.3.0...typescript/v0.3.1) (2026-06-03)


### Bug Fixes

* **sdk:** cross-SDK canonical serialization parity (decimals, fields, round-trip) ([ae2e40c](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ae2e40c95d1be0034c752f9b5b9ff35b17f78996))
* **sdk:** cross-SDK canonical serialization parity (decimals, fields,… ([f7e156b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f7e156bd080dd9b363e2cd1eb74134e8c5b39be7))

## [0.3.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/typescript/v0.2.1...typescript/v0.3.0) (2026-06-03)


### ⚠ BREAKING CHANGES

* **sdk:** DEXCOST_ENDPOINT is no longer read. Configure a non-default endpoint via the in-code option instead. Pre-launch; no external consumers.

### Code Refactoring

* **sdk:** endpoint via explicit in-code config; drop DEXCOST_ENDPOINT env read ([0f4b397](https://github.com/DexwoxBusiness/dexcost-sdk/commit/0f4b39733320f3bd9848d83720a811a0a60467b0))

## [0.2.1](https://github.com/DexwoxBusiness/dexcost-sdk/compare/typescript/v0.2.0...typescript/v0.2.1) (2026-06-02)


### Bug Fixes

* **ts-sdk,security:** route telemetry pusher through the HTTPS allow-list (was reading DEXCOST_ENDPOINT raw, leaking the Bearer key over http) ([facc1b2](https://github.com/DexwoxBusiness/dexcost-sdk/commit/facc1b2544d3cda78ace57708cda1e88ae26bd2a))

## [0.2.0](https://github.com/DexwoxBusiness/dexcost-sdk/compare/typescript/v0.1.0...typescript/v0.2.0) (2026-05-30)


### Features

* **compute-ts:** auto-emit + back-fill compute_cost events at task finalize ([919b425](https://github.com/DexwoxBusiness/dexcost-sdk/commit/919b42548ca3ef7d5f66eb1691225033d086017f))
* **compute-ts:** cgroup v2 file readers (node-only, browser-safe) ([81bf7cb](https://github.com/DexwoxBusiness/dexcost-sdk/commit/81bf7cb465b53748c40f6e79f7925049e59c7a32))
* **compute-ts:** extend CloudEnv with instanceType from IMDS (Decision [#3](https://github.com/DexwoxBusiness/dexcost-sdk/issues/3)) ([1bf71fd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/1bf71fd3d9b2986354ecbaa87a6e10809dda3271))
* **compute-ts:** Fargate ECS task metadata helper ([eec21a6](https://github.com/DexwoxBusiness/dexcost-sdk/commit/eec21a6a375eeb69746a8a6c17d13d301649d4d1))
* **compute-ts:** per-task accountant — cgroup start/end snapshots, single event ([fb4de60](https://github.com/DexwoxBusiness/dexcost-sdk/commit/fb4de601f3ceda84a594b221cdafc52c7d97097d))
* **compute-ts:** pricing engine — per-billing-model math + degradation ladder ([546f80b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/546f80b38478d9e82501f32519c048ea454ee6e1))
* **compute-ts:** runtime resolver — serverless &gt; k8s &gt; cloud_detect IaaS ([0cecf38](https://github.com/DexwoxBusiness/dexcost-sdk/commit/0cecf3852b87c1b14bd481c62861a4af04fb6809))
* **compute-ts:** serverless handler wraps + Options knobs ([8693405](https://github.com/DexwoxBusiness/dexcost-sdk/commit/869340594823fdb0f6a4b2d234060deaaf2675bb))
* **gpu-ts:** auto-emit dual events + back-fill cost at task finalize ([251b453](https://github.com/DexwoxBusiness/dexcost-sdk/commit/251b45390658119f9c0ce5ba9b3fb03cdcd6207b))
* **gpu-ts:** cgroup-scope classifier — Decision [#1](https://github.com/DexwoxBusiness/dexcost-sdk/issues/1) verification gate ([20a1bac](https://github.com/DexwoxBusiness/dexcost-sdk/commit/20a1bac0d0555a495e672be088ce73ae9cde424e))
* **gpu-ts:** EventType.{gpu_cost,gpu_utilization_signal} + Task.gpuCostUsd ([81c0f9d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/81c0f9dc39a4630d7f836703fc681143a4ce453e))
* **gpu-ts:** GPU runtime cascade — serverless env &gt; IaaS family &gt; NVML ([ada3182](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ada3182f042f83410d46842dc90172694cf31a27))
* **gpu-ts:** NVML reader via nvidia-smi shell-out ([a9c6ee3](https://github.com/DexwoxBusiness/dexcost-sdk/commit/a9c6ee3ee20dc56dc760bd805c74db0eeb924bec))
* **gpu-ts:** per-task accountant — cgroup walk + NVML snapshot pair + dual emission ([958bd7b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/958bd7b8df36d375a3b3d92234541b62c65fd376))
* **gpu-ts:** pricing engine — 4 billing models + 5-tier ladder + device-class fallback ([1601e07](https://github.com/DexwoxBusiness/dexcost-sdk/commit/1601e07cfa8492c11dc85bc9f8d8666ea7d649dc))
* **gpu-ts:** serverless handler wraps (Modal / RunPod / Replicate) + Task._gpu ([abf5488](https://github.com/DexwoxBusiness/dexcost-sdk/commit/abf54882672f99edc28d36fe963a53560290fc15))
* **gpu:** bundle initial gpu_prices.json across four SDKs from live 2026 sources ([79c8745](https://github.com/DexwoxBusiness/dexcost-sdk/commit/79c8745026f92740c5f83d7171080ce98cf81c30))
* implement compute, network, and GPU cost capture & attribution ([f56f42d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f56f42d49043eea2569ea062bf2fada5cc4d1f06))
* **network:** _netbytes helpers — classifier + byte measurement ([42be7f8](https://github.com/DexwoxBusiness/dexcost-sdk/commit/42be7f8b45c588c7157869740c4e9a10c4bc571e))
* **network,ts:** cloud-detect — env / DMI / IMDS phases, init never blocks ([ec45eda](https://github.com/DexwoxBusiness/dexcost-sdk/commit/ec45eda0af8044252ac38b30e33a8b35ca8fab78))
* **network,ts:** egress pricing engine — 5-tier degradation ladder ([7c631d6](https://github.com/DexwoxBusiness/dexcost-sdk/commit/7c631d6f48db3857fbffac8fd45d87c70fe4d160))
* **network,ts:** fetch patch — byte accounting + TransformStream + network events ([d458b6b](https://github.com/DexwoxBusiness/dexcost-sdk/commit/d458b6baaf145ccead8159b9507ef7c578b9e065))
* **network,ts:** NetworkAccountant + registry — per-task byte-usage accumulator ([e3ad22a](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e3ad22af81e1a0c4c43e320021b89426b2671442))
* **network,ts:** task finalize — v2 egress pricing + per-event back-fill ([8269b05](https://github.com/DexwoxBusiness/dexcost-sdk/commit/8269b05089f43205c6b7bed493524c77367f014e))
* **network:** add four network fields to Task ([7ebfc40](https://github.com/DexwoxBusiness/dexcost-sdk/commit/7ebfc402ea0d51a2b0366be19338906e8f8eab3b))
* **network:** add network event type ([47c5b45](https://github.com/DexwoxBusiness/dexcost-sdk/commit/47c5b4559267ed557a80c50f0553016f8ba12b1f))
* Publish pipeline added with addition of cpying jsons for attribution and removal of pnpm which is no longer needed ([78cbc89](https://github.com/DexwoxBusiness/dexcost-sdk/commit/78cbc8928bd9040c08a4f64753313100cc84361b))
* **security:** scrub_url across all 4 SDKs (Sprint 1 Theme A, part 1) ([07d1097](https://github.com/DexwoxBusiness/dexcost-sdk/commit/07d10977eebcd77b16e409f9781058e80a5a46ce))
* **security:** wire scrub_url into URL-capture call sites (Sprint 1 Theme A, part 2) ([56b4cf9](https://github.com/DexwoxBusiness/dexcost-sdk/commit/56b4cf9845c3ecb1a12ec07b75f69c5ee549a07d))


### Bug Fixes

* **all:** B14 — public set_api_key for auth-failure recovery across 4 SDKs (Sprint 2 Theme D / §3.2.3) ([bacfacd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/bacfacd2140427bedc036c799d183bf8907794b2))
* **all:** P1 — canonical timestamp serialisation (Sprint 3 Theme F / §4.1.1) ([03064a7](https://github.com/DexwoxBusiness/dexcost-sdk/commit/03064a7bab0ae461fce2fb6f99842945b32d6e8a))
* **all:** P2 — sync LLM cost maps across 4 SDKs + drift CI check (Sprint 3 Theme F / §4.1.2) ([2ce299f](https://github.com/DexwoxBusiness/dexcost-sdk/commit/2ce299f48d21e0d13834dd672dbd7df04ffae5d4))
* **all:** P3/P4/P5 — parity reconciliation (Sprint 3 Theme F / §4.1.3) ([d82407f](https://github.com/DexwoxBusiness/dexcost-sdk/commit/d82407f8f7ba9b77355ea695f94f6a33342b0597))
* **go,ts,rust:** A3 unbounded growth caps (Sprint 4 §5.2) ([da80181](https://github.com/DexwoxBusiness/dexcost-sdk/commit/da80181eefe2578dba7a11d777ad8ac787c73407))
* **security:** A2 — DEXCOST_ENDPOINT https-only allow-list across all 4 SDKs (Sprint 1 Theme A / §2.1) ([64bd3dd](https://github.com/DexwoxBusiness/dexcost-sdk/commit/64bd3dd72bfde3ac475477765ecd22a09fa6f8f7))
* **typescript,rust:** B12 — pusher partial-success accounting (Sprint 2 Theme D / §3.2.1) ([45ad099](https://github.com/DexwoxBusiness/dexcost-sdk/commit/45ad099cd6dc936396d3714e5dc21ece657040b1))
* **typescript:** B2 — GPU SM-time integration (Sprint 2 Theme C / §3.1.1 TS port) ([05a21bb](https://github.com/DexwoxBusiness/dexcost-sdk/commit/05a21bb7e1c3260aeef9cc03c13cae5387546a3e))
* **typescript:** B3 — Decimal-based cost accumulation (Sprint 2 Theme E / §3.3.1) ([e483cd2](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e483cd27d5d270c1a55c38801c63b988029ba805))
* **typescript:** B8 — graceful fallback when better-sqlite3 unavailable (Sprint 1 Theme B / §2.2.3) ([a6eb6db](https://github.com/DexwoxBusiness/dexcost-sdk/commit/a6eb6db0f8724b94103232aca43112275bab9fac))
* **typescript:** B8 follow-on — in-memory Map-based buffer with 10k FIFO cap (Sprint 1 Theme B / §2.2.3 stretch) ([b95e36a](https://github.com/DexwoxBusiness/dexcost-sdk/commit/b95e36aea5c2ef433e97bb9681031ceed99c2583))
* **typescript:** B9 — flush events on process exit (Sprint 2 Theme E / §3.3.2) ([2ea086d](https://github.com/DexwoxBusiness/dexcost-sdk/commit/2ea086da2f8e7a1ce28ec30392d3137b2be56507))
* **typescript:** clear all 119 lint errors ([f4d9679](https://github.com/DexwoxBusiness/dexcost-sdk/commit/f4d967973a2ec3f69dd728c74e8acac88c1589ab))
* **typescript:** clear all 119 lint errors ([11fbddb](https://github.com/DexwoxBusiness/dexcost-sdk/commit/11fbddb84f450d5c41800d6ac8dd45def0388eb9))
* **typescript:** runtime support — fetch double-patch + frozen http + Node 18 JSON loads (Sprint 3 Theme E / §4.2) ([e3ee9ed](https://github.com/DexwoxBusiness/dexcost-sdk/commit/e3ee9edfb76eea507a7bb02ad06e2f7a2c03e3e5))
