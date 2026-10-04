# Hosted OCR attribution — verified 2026-10-04

## Money and trust boundary

This is **automatic native page capture plus a trusted manual invoice mapping**, not automatic invoice discovery or an authenticated billing-import adapter. SDKs contain no OCR price formula. The caller must verify which billing account owns the client and processor. No account lookup, upload, polling, document retrieval or paid verification call is made.

Wrap native clients with Python `instrument_textract` / `instrument_document_ai`, or TypeScript `instrumentTextract` / `instrumentDocumentAI`. To disable capture on the returned facade, use the matching Python `uninstrument_textract(facade)` / `uninstrument_document_ai(facade)`, or TypeScript `uninstrumentTextract(facade)` / `uninstrumentDocumentAI(facade)`. Cleanup is idempotent and does not close or otherwise change the native provider client.

The server JSON profile owns the allowed page meter and resource identity. Submit closed, reconciled OCR-only money through `/v1/provider-billing-costs`, with the stable invoice population record, revision, exact decimal amount/currency, account, matching resource and billing period. When supplying `allocation_basis_quantity`, set `allocation_population: "all_usage_including_free"` and include **all successfully processed pages in that exact invoice population**: free-tier pages, all volume tiers, and uninstrumented/external usage. Do not allocate a paid-tier subtotal using all pages, nor divide an account invoice independently among processors or regions. Aggregate reconciled tier/free lines into a single matched population first, or leave unmatched money unallocated. Discounts/credits included in that reconciled subtotal reduce its actual net money; unrelated tax, add-ons, storage, minimums or unclassified adjustments remain residual.

This population assertion is validated as an explicit caller attestation; DexCost does not independently authenticate the provider invoice, account ownership or completeness. Raw canonical cost-pools remain a trusted ingestion boundary, not OCR-adapter-verified invoices. Missing denominator => entire pool residual; missing invoice => usage unpriced. Neither means free. The allocator requires the entire observed call interval inside the period. External usage keeps its share residual. Replays are idempotent; invoice revisions can correct positive to negative, zero and back without changing stable identity.

## Amazon Textract

Rendered official [pricing](https://aws.amazon.com/textract/pricing/) and [DetectDocumentText response](https://docs.aws.amazon.com/textract/latest/APIReference/API_DetectDocumentText.html) were checked on 2026-10-04. The pricing page advertises a limited three-month allowance of 1,000 Detect Document Text pages/month and Oregon examples of $0.0015/page for the first million and $0.0006 thereafter. Its regional dynamic table did not establish a universal tariff. These examples are **not** active SDK/server flat cash rules.

Scope: synchronous `DetectDocumentText` via boto3/aiobotocore and AWS SDK v3. `DocumentMetadata.Pages` is the detected page count; `ResponseMetadata.RequestId` / `$metadata.requestId` gives stable deduplication within account+region. Capture requires native successful HTTP200, a positive safe integer page count, a single observed transport attempt, zero reported retries, and the ordinary regional HTTPS Textract route. Unknown pages, failed calls, custom routes and retried calls are not assumed billable or free. A retry may have incurred provider charges; its unmatched invoice share stays residual.

Native actual request routing is observed with a read-only botocore before-send hook / Smithy middleware. Documents, blocks, text, S3 locations, headers and credentials never persist. Only configured payer/credential-owner account, region, request ID, page count and call interval do.

Resource identity: `PAYER/USAGE_ACCOUNT.REGION.detect_document_text`.
Profile: `amazon_textract`; category `detect_document_text_pages`; meter `amazon_textract.detect_document_text_pages` / `Pages`.
Both account IDs are explicit 12-digit AWS IDs, not discovered or verified by the SDK. The verified native region is retained in the durable job's `region` billing dimension and emitted as `provider.region`, so an invoice may supply the matching `region` explicitly.

Excluded: AnalyzeDocument/forms/tables/queries/signatures/layout, expenses/IDs/lending, async Start/Get job APIs, nonstandard endpoints/partitions and SDK-derived money.

## Google Document AI Enterprise OCR

Rendered official [pricing](https://cloud.google.com/products/document-ai/pricing) and [process API](https://docs.cloud.google.com/document-ai/docs/reference/rest/v1/projects.locations.processors/process) were checked on 2026-10-04. The page shows the first 1,000 Enterprise OCR pages free, subsequent usage bands, and separate commitment and OCR add-on columns; the displayed table alone is not treated as a universal per-page cash rule. The page says failed 4xx/5xx requests are not charged. Format-specific billing page definitions differ; this slice admits PDF/JPEG/PNG/TIFF only, not HTML, Office, spreadsheets or arbitrary text.

[Document](https://docs.cloud.google.com/document-ai/docs/reference/rest/v1/Document) supplies `pages[].pageNumber` but does **not** guarantee a stable billing request ID. Each successful native call therefore gets a local event UUID. Awaiting the same promise again does not duplicate capture; making another process call is a new potentially paid call. Replay of a persisted event retains its UUID. No content hash, document ID or fake provider request ID is introduced.

[ProcessOptions](https://docs.cloud.google.com/document-ai/docs/reference/rest/v1/ProcessOptions) can select partial pages and enable premium OCR. This bounded facade rejects capture when options, output masks, inline preprocessed documents, premium flags, shards or nonconsecutive page numbers make the complete basic OCR population ambiguous. It requires caller-verified `OCR_PROCESSOR` plus a full processor-version path; default-version processor paths and other processor types are excluded. The invoice resource groups versions of the same processor, never different processors. Type attestation is not a hidden GetProcessor call.

The official generated SDK retries ProcessDocument by default. For this slice, the caller explicitly disables retry: Python `retry=None`, TypeScript `{retry:null}`. DexCost does not alter request/retry behavior. Custom metadata or billing/routing overrides are not admitted. The only permitted Node option header is the generated `x-goog-request-params`, which the native method overwrites from the exact bound processor name. Python requires empty caller metadata. Regional configured transport host must match `LOCATION-documentai.googleapis.com`; no global/custom endpoint assumptions.

Resource identity: `BILLING_ACCOUNT/PROJECT.LOCATION.PROCESSOR.enterprise_ocr`.
Profile: `google_document_ai`; category `enterprise_ocr_pages`; meter `google_document_ai.enterprise_ocr_pages` / `Pages`.
The bound processor location is emitted as `provider.region`; an invoice may supply that matching location as `region`. Missing or different observation regions never match an explicitly region-scoped invoice.

Supported packages: Google Python v1 synchronous and asynchronous ProcessDocument; Node `v1.DocumentProcessorServiceClient.processDocument` promise API preserving its native tuple values. Callback, batch, human review, streaming, premium OCR, forms/custom processors, partial results and local/self-hosted tools remain outside scope.

## Native API evidence and reproducible tests

Official source: [Google Node generated client](https://github.com/googleapis/google-cloud-node/blob/main/packages/google-cloud-documentai/src/v1/document_processor_service_client.ts), [Node retry defaults](https://github.com/googleapis/google-cloud-node/blob/main/packages/google-cloud-documentai/src/v1/document_processor_service_client_config.json), [Google Python client](https://github.com/googleapis/google-cloud-python/blob/main/packages/google-cloud-documentai/google/cloud/documentai_v1/services/document_processor_service/client.py), [google-gax retry options](https://github.com/googleapis/gax-nodejs/blob/main/gax/src/gax.ts).

No-network native tests use pinned `@aws-sdk/client-textract@3.1146.0`, `@google-cloud/documentai@10.1.1`, Python `google-cloud-documentai==3.16.0`, and the repository's boto3 range. AWS tests exercise serialization, actual middleware and synthetic in-memory transport; Google tests exercise generated method/request conversion and mocked underlying RPC only. Shared fixtures cover malformed/missing/zero counters, retry ambiguity, wrong routes, processor mismatch and partial pages. No test sends documents or credentials to a provider.
