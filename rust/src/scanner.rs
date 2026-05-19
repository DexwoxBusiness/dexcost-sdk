//! Code scanner for cost point detection.
//!
//! Regex-based static analysis — no API key needed, runs offline.
//! Detects LLM SDK usage, agent framework calls, paid service imports,
//! and direct HTTP calls to known AI API endpoints in Rust source files.

use regex::Regex;
use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

/// A detected cost-generating pattern in source code.
#[derive(Debug, Clone)]
pub struct CostPoint {
    pub file: String,
    pub line: usize,
    pub category: String,
    pub provider: String,
    pub description: String,
    pub auto_instrumented: bool,
    pub pattern: String,
}

/// Aggregated scan results.
#[derive(Debug, Default)]
pub struct ScanResult {
    pub cost_points: Vec<CostPoint>,
    pub files_scanned: usize,
}

impl ScanResult {
    pub fn auto_count(&self) -> usize {
        self.cost_points
            .iter()
            .filter(|cp| cp.auto_instrumented)
            .count()
    }

    pub fn manual_count(&self) -> usize {
        self.cost_points
            .iter()
            .filter(|cp| !cp.auto_instrumented)
            .count()
    }
}

/// Directories to skip during scanning.
const SKIP_DIRS: &[&str] = &[
    "target",
    ".git",
    "node_modules",
    ".cargo",
    "vendor",
    "dist",
    "build",
    ".cache",
];

struct ScanPattern {
    regex: Regex,
    provider: &'static str,
    category: &'static str,
    description: &'static str,
    auto_instrumented: bool,
}

/// Build all detection patterns. Called once per scan.
fn build_patterns() -> Vec<ScanPattern> {
    let mut patterns = Vec::new();

    macro_rules! pat {
        ($re:expr, $prov:expr, $cat:expr, $desc:expr, $auto:expr) => {
            patterns.push(ScanPattern {
                regex: Regex::new($re).expect("invalid regex"),
                provider: $prov,
                category: $cat,
                description: $desc,
                auto_instrumented: $auto,
            });
        };
    }

    // ── LLM provider crate imports ──────────────────────────────────
    pat!(
        r"use\s+async_openai",
        "openai",
        "llm",
        "async-openai crate",
        false
    );
    pat!(r"use\s+openai", "openai", "llm", "openai crate", false);
    pat!(
        r#"(?:use|extern\s+crate)\s+anthropic"#,
        "anthropic",
        "llm",
        "anthropic crate",
        false
    );
    pat!(
        r"use\s+genai",
        "genai",
        "llm",
        "rust-genai multi-provider crate",
        false
    );
    pat!(
        r"use\s+llm\b",
        "llm",
        "llm",
        "llm multi-provider crate",
        false
    );
    pat!(
        r"use\s+llm_bridge",
        "llm-bridge",
        "llm",
        "llm-bridge crate",
        false
    );
    pat!(
        r"use\s+mistralai",
        "mistral",
        "llm",
        "Mistral AI crate",
        false
    );
    pat!(
        r"use\s+replicate",
        "replicate",
        "llm",
        "Replicate crate",
        false
    );
    pat!(r"use\s+cohere", "cohere", "llm", "Cohere crate", false);
    pat!(r"use\s+ollama_rs", "ollama", "llm", "Ollama crate", false);

    // ── LLM call patterns ──────────────���────────────────────────────
    pat!(
        r"\.create_chat_completion",
        "openai",
        "llm",
        "OpenAI chat completion",
        false
    );
    pat!(
        r"ChatCompletionRequestMessage",
        "openai",
        "llm",
        "OpenAI chat message builder",
        false
    );
    pat!(
        r"\.messages\(\)\s*\.create",
        "anthropic",
        "llm",
        "Anthropic message create",
        false
    );
    pat!(
        r"\.generate_content",
        "google-ai",
        "llm",
        "Google AI generate content",
        false
    );
    pat!(r"\.chat\(\)", "cohere", "llm", "Cohere chat", false);

    // ── Bedrock invoke ───────────────────────────────────────────────
    pat!(
        r"\.invoke_model\b",
        "aws-bedrock",
        "llm",
        "AWS Bedrock invoke model",
        false
    );
    pat!(
        r"\.invoke_model_with_response_stream\b",
        "aws-bedrock",
        "llm",
        "AWS Bedrock streaming invoke",
        false
    );

    // ── Embeddings / Speech / Image ─────────────────────────────────
    pat!(
        r"\.create_embedding",
        "openai",
        "embedding",
        "OpenAI embedding",
        false
    );
    pat!(
        r"\.create_transcription",
        "openai",
        "speech",
        "OpenAI Whisper transcription",
        false
    );
    pat!(r"\.create_speech", "openai", "speech", "OpenAI TTS", false);
    pat!(
        r"\.create_image",
        "openai",
        "image",
        "OpenAI DALL-E image generation",
        false
    );

    // ── Agent frameworks ────────────────────────────────────────────
    pat!(
        r"use\s+langchain",
        "langchaingo",
        "framework",
        "LangChain Rust crate",
        false
    );
    pat!(
        r"use\s+rig\b",
        "rig",
        "framework",
        "Rig AI agent framework",
        false
    );
    pat!(
        r"use\s+eino",
        "eino",
        "framework",
        "Eino LLM framework",
        false
    );
    pat!(
        r"use\s+autoagents",
        "autoagents",
        "framework",
        "AutoAgents framework",
        false
    );
    pat!(r"use\s+adk\b", "adk", "framework", "Google ADK Rust", false);

    // ── Cloud / Infrastructure ───────���──────────────────────────────
    pat!(
        r"use\s+aws_sdk_bedrockruntime",
        "aws-bedrock",
        "llm",
        "AWS Bedrock runtime",
        false
    );
    pat!(r"use\s+aws_sdk_", "aws", "service", "AWS SDK crate", false);
    pat!(
        r"use\s+aws_config",
        "aws",
        "service",
        "AWS SDK config",
        false
    );
    pat!(
        r"use\s+google_cloud",
        "gcp",
        "service",
        "Google Cloud crate",
        false
    );
    pat!(
        r"use\s+azure_",
        "azure",
        "service",
        "Azure SDK crate",
        false
    );

    // ── Databases ───────────────────────────────────────────────────
    pat!(
        r"use\s+mongodb",
        "mongodb",
        "service",
        "MongoDB driver",
        false
    );
    pat!(
        r"use\s+elasticsearch",
        "elasticsearch",
        "service",
        "Elasticsearch client",
        false
    );
    pat!(r"use\s+redis\b", "redis", "service", "Redis client", false);
    pat!(
        r"use\s+rusqlite",
        "sqlite",
        "service",
        "SQLite (rusqlite)",
        false
    );
    pat!(
        r"use\s+sqlx",
        "sqlx",
        "service",
        "SQLx database client",
        false
    );
    pat!(r"use\s+diesel", "diesel", "service", "Diesel ORM", false);
    pat!(r"use\s+sea_orm", "sea-orm", "service", "SeaORM", false);
    pat!(
        r"use\s+surrealdb",
        "surrealdb",
        "service",
        "SurrealDB client",
        false
    );

    // ── Vector databases ────────────��───────────────────────────────
    pat!(
        r"use\s+pinecone",
        "pinecone",
        "service",
        "Pinecone client",
        false
    );
    pat!(
        r"use\s+qdrant_client",
        "qdrant",
        "service",
        "Qdrant client",
        false
    );
    pat!(
        r"use\s+weaviate",
        "weaviate",
        "service",
        "Weaviate client",
        false
    );
    pat!(r"use\s+milvus", "milvus", "service", "Milvus client", false);
    pat!(
        r"use\s+chromadb",
        "chromadb",
        "service",
        "ChromaDB client",
        false
    );

    // ── Payments ─────────���───────────────────────────���──────────────
    pat!(r"use\s+stripe", "stripe", "service", "Stripe crate", false);

    // ── Messaging ────────��──────────────────────────────────────────
    pat!(r"use\s+twilio", "twilio", "service", "Twilio crate", false);
    pat!(
        r"use\s+sendgrid",
        "sendgrid",
        "service",
        "SendGrid crate",
        false
    );
    pat!(r"use\s+resend", "resend", "service", "Resend crate", false);
    pat!(
        r"use\s+slack_morphism",
        "slack",
        "service",
        "Slack SDK",
        false
    );

    // ── HTTP clients (manual cost tracking needed) ──────────────────
    pat!(
        r"use\s+reqwest",
        "reqwest",
        "http",
        "reqwest HTTP client",
        false
    );
    pat!(
        r"use\s+hyper\b",
        "hyper",
        "http",
        "hyper HTTP client",
        false
    );
    pat!(r"use\s+surf\b", "surf", "http", "surf HTTP client", false);

    // ── Direct HTTP calls to AI API endpoints ───────────────────────
    pat!(
        r#"https?://api\.openai\.com"#,
        "openai",
        "http_call",
        "Direct HTTP to OpenAI API",
        false
    );
    pat!(
        r#"https?://api\.anthropic\.com"#,
        "anthropic",
        "http_call",
        "Direct HTTP to Anthropic API",
        false
    );
    pat!(
        r#"https?://api\.cohere\.(ai|com)"#,
        "cohere",
        "http_call",
        "Direct HTTP to Cohere API",
        false
    );
    pat!(
        r#"https?://api\.groq\.com"#,
        "groq",
        "http_call",
        "Direct HTTP to Groq API",
        false
    );
    pat!(
        r#"https?://api\.mistral\.ai"#,
        "mistral",
        "http_call",
        "Direct HTTP to Mistral API",
        false
    );
    pat!(
        r#"https?://api\.together\.xyz"#,
        "together",
        "http_call",
        "Direct HTTP to Together API",
        false
    );
    pat!(
        r#"https?://api\.replicate\.com"#,
        "replicate",
        "http_call",
        "Direct HTTP to Replicate API",
        false
    );
    pat!(
        r#"https?://api\.deepseek\.com"#,
        "deepseek",
        "http_call",
        "Direct HTTP to DeepSeek API",
        false
    );

    // ── Scraping / data ─────────────────────────────────────────────
    pat!(
        r"use\s+spider",
        "spider",
        "service",
        "Spider web crawler",
        false
    );
    pat!(
        r"use\s+scraper\b",
        "scraper",
        "service",
        "scraper HTML parser",
        false
    );

    // ── Geo / Maps ──────────────────────────────────────────────────
    pat!(
        r"use\s+google_maps",
        "google-maps",
        "service",
        "Google Maps crate",
        false
    );

    patterns
}

/// Scan a directory tree for cost points in .rs files.
pub fn scan_directory(root: &Path) -> ScanResult {
    let mut result = ScanResult::default();
    if !root.exists() {
        return result;
    }

    let target = if root.is_dir() {
        root.to_path_buf()
    } else {
        root.parent().unwrap_or(root).to_path_buf()
    };
    let patterns = build_patterns();

    let files = collect_rs_files(&target);
    for file in files {
        result.files_scanned += 1;
        if let Ok(source) = fs::read_to_string(&file) {
            let points = scan_source(&source, &file.to_string_lossy(), &patterns);
            result.cost_points.extend(points);
        }
    }

    result
}

/// Scan a single source string for cost points.
fn scan_source(source: &str, file_name: &str, patterns: &[ScanPattern]) -> Vec<CostPoint> {
    let mut points = Vec::new();
    let mut seen_lines: HashSet<(usize, &str)> = HashSet::new();

    for pattern in patterns {
        for mat in pattern.regex.find_iter(source) {
            let before = &source[..mat.start()];
            let line = before.chars().filter(|&c| c == '\n').count() + 1;

            let key = (line, pattern.provider);
            if seen_lines.contains(&key) {
                continue;
            }
            seen_lines.insert(key);

            points.push(CostPoint {
                file: file_name.to_string(),
                line,
                category: pattern.category.to_string(),
                provider: pattern.provider.to_string(),
                description: pattern.description.to_string(),
                auto_instrumented: pattern.auto_instrumented,
                pattern: mat.as_str().to_string(),
            });
        }
    }

    points
}

/// Recursively collect .rs files, skipping known non-source directories.
fn collect_rs_files(dir: &Path) -> Vec<PathBuf> {
    let mut files = Vec::new();
    if let Ok(entries) = fs::read_dir(dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
                if !SKIP_DIRS.contains(&name) {
                    files.extend(collect_rs_files(&path));
                }
            } else if path.extension().is_some_and(|ext| ext == "rs") {
                files.push(path);
            }
        }
    }
    files.sort();
    files
}

/// Generate record_cost() stub snippets for manual cost points.
///
/// The emitted snippet must compile against the current public API. The
/// example below is the canonical shape `generate_stubs` produces; being a
/// `no_run` doctest it is type-checked by `cargo test --doc`, so a drift
/// between the stub template and the real API surfaces as a build failure.
///
/// ```no_run
/// use dexcost::{Config, TaskOptions, TaskStatus};
/// use rust_decimal::Decimal;
///
/// # async fn _generated_stub() -> Result<(), dexcost::DexcostError> {
/// // `init` is synchronous — no `.await` here.
/// dexcost::init(Config {
///     api_key: Some("dx_live_...".into()),
///     ..Default::default()
/// })?;
///
/// let mut task = dexcost::start_task("your_task_type", TaskOptions::default()).await?;
/// task.record_cost("stripe", Decimal::ZERO, None, None).await?;
/// task.end(TaskStatus::Success).await?;
/// # Ok(())
/// # }
/// ```
pub fn generate_stubs(result: &ScanResult) -> String {
    let mut output = String::new();

    let auto: Vec<&CostPoint> = result
        .cost_points
        .iter()
        .filter(|cp| cp.auto_instrumented && cp.category != "import")
        .collect();
    let manual: Vec<&CostPoint> = result
        .cost_points
        .iter()
        .filter(|cp| !cp.auto_instrumented && cp.category != "import")
        .collect();

    output.push_str("// ============================================================\n");
    output.push_str("// dexcost integration stubs\n");
    output.push_str("// Generated by: dexcost scan --generate-stubs\n");
    output.push_str("// ============================================================\n\n");

    output.push_str("// --- Step 1: Initialize dexcost ---\n");
    output.push_str("use dexcost::{Config, TaskOptions, TaskStatus};\n");
    output.push_str("use rust_decimal::Decimal;\n\n");
    output.push_str("// `init` is synchronous — no `.await` here.\n");
    output.push_str("dexcost::init(Config {\n");
    output.push_str(
        "    api_key: Some(\"dx_live_...\".into()), // or set DEXCOST_API_KEY env var\n",
    );
    output.push_str("    ..Default::default()\n");
    output.push_str("})?;\n\n");

    output.push_str("// --- Step 2: Track tasks ---\n");
    output.push_str("let mut task = dexcost::start_task(\"your_task_type\", TaskOptions::default()).await?;\n\n");

    if !manual.is_empty() {
        output.push_str("// --- Manual cost tracking (for services not auto-instrumented) ---\n");
        for cp in &manual {
            output.push_str(&format!(
                "// {}:{} — {}\n",
                cp.file, cp.line, cp.description
            ));
            output.push_str(&format!(
                "task.record_cost(\"{}\", Decimal::ZERO, None, None).await?; // TODO: set actual cost\n\n",
                cp.provider
            ));
        }
    }

    output.push_str("task.end(TaskStatus::Success).await?;\n\n");

    if !auto.is_empty() {
        output.push_str("// --- Auto-instrumented (no code changes needed) ---\n");
        let mut providers: std::collections::HashMap<&str, usize> =
            std::collections::HashMap::new();
        for cp in &auto {
            *providers.entry(cp.provider.as_str()).or_insert(0) += 1;
        }
        for (provider, count) in &providers {
            let s = if *count == 1 { "" } else { "s" };
            output.push_str(&format!(
                "// ✓ {} ({} call{} detected)\n",
                provider, count, s
            ));
        }
    }

    output
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use tempfile::TempDir;

    fn scan_code(code: &str) -> ScanResult {
        let patterns = build_patterns();
        let points = scan_source(code, "test.rs", &patterns);
        ScanResult {
            cost_points: points,
            files_scanned: 1,
        }
    }

    #[test]
    fn test_detects_async_openai() {
        let result = scan_code("use async_openai::Client;");
        assert!(result.cost_points.iter().any(|cp| cp.provider == "openai"));
    }

    #[test]
    fn test_detects_anthropic() {
        let result = scan_code("use anthropic::client::Client;");
        assert!(result
            .cost_points
            .iter()
            .any(|cp| cp.provider == "anthropic"));
    }

    #[test]
    fn test_detects_create_chat_completion() {
        let result = scan_code(r#"let resp = client.create_chat_completion(req).await?;"#);
        assert!(result
            .cost_points
            .iter()
            .any(|cp| cp.description.contains("chat completion")));
    }

    #[test]
    fn test_detects_reqwest() {
        let result = scan_code("use reqwest::Client;");
        assert!(result.cost_points.iter().any(|cp| cp.provider == "reqwest"));
    }

    #[test]
    fn test_detects_mongodb() {
        let result = scan_code("use mongodb::Client;");
        assert!(result.cost_points.iter().any(|cp| cp.provider == "mongodb"));
    }

    #[test]
    fn test_detects_stripe() {
        let result = scan_code("use stripe::PaymentIntent;");
        assert!(result.cost_points.iter().any(|cp| cp.provider == "stripe"));
    }

    #[test]
    fn test_detects_redis() {
        let result = scan_code("use redis::Commands;");
        assert!(result.cost_points.iter().any(|cp| cp.provider == "redis"));
    }

    #[test]
    fn test_detects_pinecone() {
        let result = scan_code("use pinecone::PineconeClient;");
        assert!(result
            .cost_points
            .iter()
            .any(|cp| cp.provider == "pinecone"));
    }

    #[test]
    fn test_detects_openai_api_url() {
        let result = scan_code(r#"let url = "https://api.openai.com/v1/chat/completions";"#);
        assert!(result
            .cost_points
            .iter()
            .any(|cp| cp.category == "http_call" && cp.provider == "openai"));
    }

    #[test]
    fn test_detects_aws_sdk() {
        let result = scan_code("use aws_sdk_s3::Client;");
        assert!(result.cost_points.iter().any(|cp| cp.provider == "aws"));
    }

    #[test]
    fn test_detects_aws_bedrock() {
        let result = scan_code("use aws_sdk_bedrockruntime::Client;");
        assert!(result
            .cost_points
            .iter()
            .any(|cp| cp.provider == "aws-bedrock"));
    }

    #[test]
    fn test_detects_embedding() {
        let result = scan_code("let emb = client.create_embedding(req).await?;");
        assert!(result
            .cost_points
            .iter()
            .any(|cp| cp.category == "embedding"));
    }

    #[test]
    fn test_no_false_positive_on_clean_file() {
        let result = scan_code("fn add(a: i32, b: i32) -> i32 { a + b }");
        assert!(result.cost_points.is_empty());
    }

    #[test]
    fn test_deduplicates_same_line() {
        let result = scan_code("use async_openai::Client; // openai crate");
        let openai_points: Vec<_> = result
            .cost_points
            .iter()
            .filter(|cp| cp.provider == "openai")
            .collect();
        assert_eq!(openai_points.len(), 1);
    }

    #[test]
    fn test_scan_directory_skips_target() {
        let tmp = TempDir::new().unwrap();
        let target_dir = tmp.path().join("target").join("debug");
        fs::create_dir_all(&target_dir).unwrap();
        let mut f = fs::File::create(target_dir.join("generated.rs")).unwrap();
        writeln!(f, "use async_openai::Client;").unwrap();

        let mut app = fs::File::create(tmp.path().join("main.rs")).unwrap();
        writeln!(app, "fn main() {{}}").unwrap();

        let result = scan_directory(tmp.path());
        assert_eq!(result.files_scanned, 1); // Only main.rs
    }

    #[test]
    fn test_correct_line_numbers() {
        let code = "// line 1\n// line 2\nuse redis::Commands;\n";
        let result = scan_code(code);
        assert_eq!(result.cost_points[0].line, 3);
    }

    // Fix 3: the generated stub must compile against the current API. This
    // checks the template emits the *correct* forms and none of the old
    // broken ones (`init(...).await`, `start_task` with a single arg, a
    // `&str` status to `end`, an unimported `Decimal`). The full snippet is
    // also type-checked by the `no_run` doctest on `generate_stubs`.
    #[test]
    fn test_generated_stub_uses_current_api() {
        // A manual (non-auto-instrumented) cost point forces a record_cost stub.
        let result = scan_code(r#"let url = "https://api.openai.com/v1/chat";"#);
        assert!(result.manual_count() > 0, "need a manual cost point");

        let stub = generate_stubs(&result);

        // Imports the symbols the snippet relies on.
        assert!(
            stub.contains("use dexcost::{Config, TaskOptions, TaskStatus};"),
            "stub must import Config/TaskOptions/TaskStatus:\n{stub}"
        );
        assert!(
            stub.contains("use rust_decimal::Decimal;"),
            "stub uses Decimal::ZERO, so it must import Decimal:\n{stub}"
        );

        // `init` is synchronous: it must NOT be awaited.
        assert!(
            !stub.contains("init(") || !stub.contains("}).await?;"),
            "init is synchronous — `init(...).await` does not compile:\n{stub}"
        );
        assert!(stub.contains("})?;"), "init must be called without .await:\n{stub}");

        // `start_task` takes (task_type, TaskOptions).
        assert!(
            stub.contains("start_task(\"your_task_type\", TaskOptions::default()).await?"),
            "start_task must be passed a TaskOptions argument:\n{stub}"
        );
        // The handle must be `mut` — record_cost/end take `&mut self`.
        assert!(stub.contains("let mut task ="), "task handle must be mut:\n{stub}");

        // `end` takes a `TaskStatus`, not a string literal.
        assert!(
            stub.contains("task.end(TaskStatus::Success).await?;"),
            "end must take a TaskStatus, not \"success\":\n{stub}"
        );
        assert!(
            !stub.contains("task.end(\"success\")"),
            "old broken `end(\"success\")` form must be gone:\n{stub}"
        );

        // No bare `use dexcost;` (clippy single_component_path_imports).
        assert!(
            !stub.contains("use dexcost;"),
            "bare `use dexcost;` triggers a clippy lint:\n{stub}"
        );
    }
}
