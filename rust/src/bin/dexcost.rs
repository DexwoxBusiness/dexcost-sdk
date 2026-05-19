//! dexcost CLI binary
//!
//! Commands:
//!   dexcost status [--db path]
//!       Print event/task counts from the SQLite buffer.
//!
//!   dexcost rates --list
//!       List all registered rates (empty registry by default).
//!
//!   dexcost rates --import <path>
//!       Import rates from a YAML `rates:` file and list them.
//!
//!   dexcost rates --export <path>
//!       Export the current rate registry to a YAML `rates:` file.
//!
//! The `rates` subcommand reads and writes the same canonical YAML format as
//! the library [`RateRegistry`] (a top-level `rates:` mapping), so files
//! exported by the CLI re-import cleanly via the SDK and vice versa. A legacy
//! flat JSON array is still accepted on import for backward compatibility.

use std::collections::HashMap;
use std::env;

use std::path::Path;

use rusqlite::{Connection, OpenFlags};

use dexcost::pricing::rates::RateRegistry;
use dexcost::scanner;

// ---------------------------------------------------------------------------
// Rate row used for listing — a plain triple decoupled from the on-disk format.
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct RateRow {
    service: String,
    per: String,
    cost_usd: String, // stored as string to preserve Decimal precision
}

/// Collects the registry's rates into a service-keyed map for display.
fn rows_from_registry(registry: &RateRegistry) -> HashMap<String, RateRow> {
    let mut out = HashMap::new();
    for (service, entry) in registry.rates() {
        out.insert(
            service.clone(),
            RateRow {
                service: entry.service.clone(),
                per: entry.per.clone(),
                cost_usd: entry.cost_usd.to_string(),
            },
        );
    }
    out
}

// ---------------------------------------------------------------------------
// status subcommand
// ---------------------------------------------------------------------------

/// Open the SQLite DB at `db_path` (read-only) and print count stats.
/// Returns an Err string if the file does not exist or the DB cannot be read.
pub fn cmd_status(db_path: &str) -> Result<String, String> {
    let flags = OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX;
    let conn = Connection::open_with_flags(db_path, flags)
        .map_err(|e| format!("Cannot open database '{}': {}", db_path, e))?;

    let task_count: i64 = conn
        .query_row("SELECT COUNT(*) FROM tasks", [], |r| r.get(0))
        .map_err(|e| format!("Failed to query tasks: {}", e))?;

    let event_count: i64 = conn
        .query_row("SELECT COUNT(*) FROM events", [], |r| r.get(0))
        .map_err(|e| format!("Failed to query events: {}", e))?;

    let pending_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM events WHERE sync_status = 'pending'",
            [],
            |r| r.get(0),
        )
        .map_err(|e| format!("Failed to query pending events: {}", e))?;

    let total_cost: Option<String> = conn
        .query_row("SELECT SUM(CAST(cost_usd AS REAL)) FROM events", [], |r| {
            r.get(0)
        })
        .unwrap_or(None);

    let mut output = String::new();
    output.push_str(&format!("Database:      {}\n", db_path));
    output.push_str(&format!("Tasks:         {}\n", task_count));
    output.push_str(&format!("Events:        {}\n", event_count));
    output.push_str(&format!("Pending sync:  {}\n", pending_count));
    output.push_str(&format!(
        "Total cost:    ${}\n",
        total_cost.unwrap_or_else(|| "0.00".into())
    ));

    Ok(output)
}

// ---------------------------------------------------------------------------
// rates subcommand
// ---------------------------------------------------------------------------

/// List rates from a map, returning a formatted string.
pub fn cmd_rates_list(rates: &HashMap<String, RateRow>) -> String {
    if rates.is_empty() {
        return "No rates registered.\n".to_string();
    }

    let mut keys: Vec<&String> = rates.keys().collect();
    keys.sort();

    let mut out = format!("{:<24} {:<20} {}\n", "SERVICE", "PER", "COST_USD");
    out.push_str(&"-".repeat(56));
    out.push('\n');

    for k in keys {
        let e = &rates[k];
        out.push_str(&format!("{:<24} {:<20} {}\n", e.service, e.per, e.cost_usd));
    }

    out
}

/// Import rates from a file via the library [`RateRegistry`].
///
/// Accepts the canonical YAML `rates:` mapping (the format the CLI exports and
/// the SDK reads/writes) and, for backward compatibility, a legacy flat JSON
/// array of `{service, per, cost_usd}` objects.
pub fn cmd_rates_import(path: &str) -> Result<HashMap<String, RateRow>, String> {
    let mut registry = RateRegistry::new();
    registry
        .load_from_file(Path::new(path))
        .map_err(|e| format!("Cannot import rates from '{}': {}", path, e))?;
    Ok(rows_from_registry(&registry))
}

/// Export a rates map to a YAML `rates:` file via the library [`RateRegistry`].
///
/// Output is the same canonical format the SDK's `RateRegistry::save_to_file`
/// produces, so the file re-imports cleanly through `RateRegistry::load`.
pub fn cmd_rates_export(path: &str, rates: &HashMap<String, RateRow>) -> Result<(), String> {
    let registry = registry_from_rows(rates)?;
    registry
        .save_to_file(Path::new(path))
        .map_err(|e| format!("Failed to export rates to '{}': {}", path, e))?;
    Ok(())
}

/// Builds a [`RateRegistry`] from a service-keyed row map, validating costs.
fn registry_from_rows(rates: &HashMap<String, RateRow>) -> Result<RateRegistry, String> {
    let mut registry = RateRegistry::new();
    for row in rates.values() {
        let cost = row.cost_usd.parse().map_err(|_| {
            format!(
                "Invalid cost_usd '{}' for service '{}'",
                row.cost_usd, row.service
            )
        })?;
        registry.register(&row.service, &row.per, cost);
    }
    Ok(registry)
}

/// Load rates from the persistent registry at `RateRegistry::default_path()`
/// (`~/.dexcost/rates.yaml`). Returns an empty map if the file does not exist.
fn load_persistent_rates() -> Result<HashMap<String, RateRow>, String> {
    let path = RateRegistry::default_path();
    if !path.exists() {
        return Ok(HashMap::new());
    }
    let mut registry = RateRegistry::new();
    registry.load_from_file(&path)?;
    Ok(rows_from_registry(&registry))
}

/// Persist the given rate map to `RateRegistry::default_path()` as canonical
/// YAML (`rates:` mapping).
fn save_persistent_rates(rates: &HashMap<String, RateRow>) -> Result<(), String> {
    let registry = registry_from_rows(rates)?;
    registry.save_to_file(&RateRegistry::default_path())?;
    Ok(())
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

fn usage() {
    eprintln!("Usage:");
    eprintln!("  dexcost status [--db <path>]");
    eprintln!("  dexcost rates --list");
    eprintln!("  dexcost rates --import <path>");
    eprintln!("  dexcost rates --export <path>");
    eprintln!("  dexcost scan [<path>] [--generate-stubs]");
}

fn main() {
    let args: Vec<String> = env::args().collect();

    if args.len() < 2 {
        usage();
        std::process::exit(1);
    }

    match args[1].as_str() {
        "status" => {
            // Parse optional --db flag
            let db_path = {
                let mut path = dirs_next::home_dir()
                    .map(|h| {
                        h.join(".dexcost")
                            .join("buffer.db")
                            .to_string_lossy()
                            .into_owned()
                    })
                    .unwrap_or_else(|| "buffer.db".to_string());

                let mut i = 2;
                while i < args.len() {
                    if args[i] == "--db" && i + 1 < args.len() {
                        path = args[i + 1].clone();
                        i += 2;
                    } else {
                        i += 1;
                    }
                }
                path
            };

            match cmd_status(&db_path) {
                Ok(output) => print!("{}", output),
                Err(e) => {
                    eprintln!("Error: {}", e);
                    std::process::exit(1);
                }
            }
        }

        "rates" => {
            if args.len() < 3 {
                eprintln!("Error: 'rates' requires a subcommand flag (--list, --import, --export)");
                usage();
                std::process::exit(1);
            }

            match args[2].as_str() {
                "--list" => {
                    let rates = match load_persistent_rates() {
                        Ok(r) => r,
                        Err(e) => {
                            eprintln!("Error: {}", e);
                            std::process::exit(1);
                        }
                    };
                    print!("{}", cmd_rates_list(&rates));
                }

                "--import" => {
                    if args.len() < 4 {
                        eprintln!("Error: --import requires a file path");
                        std::process::exit(1);
                    }
                    match cmd_rates_import(&args[3]) {
                        Ok(rates) => {
                            // Merge with existing persistent rates and re-save.
                            let mut merged = load_persistent_rates().unwrap_or_default();
                            merged.extend(rates.clone());
                            if let Err(e) = save_persistent_rates(&merged) {
                                eprintln!("Error: {}", e);
                                std::process::exit(1);
                            }
                            println!("Imported {} rate(s) from '{}'", rates.len(), args[3]);
                            print!("{}", cmd_rates_list(&merged));
                        }
                        Err(e) => {
                            eprintln!("Error: {}", e);
                            std::process::exit(1);
                        }
                    }
                }

                "--export" => {
                    if args.len() < 4 {
                        eprintln!("Error: --export requires a file path");
                        std::process::exit(1);
                    }
                    let rates = match load_persistent_rates() {
                        Ok(r) => r,
                        Err(e) => {
                            eprintln!("Error: {}", e);
                            std::process::exit(1);
                        }
                    };
                    let count = rates.len();
                    match cmd_rates_export(&args[3], &rates) {
                        Ok(()) => println!("Exported {} rate(s) to '{}'", count, args[3]),
                        Err(e) => {
                            eprintln!("Error: {}", e);
                            std::process::exit(1);
                        }
                    }
                }

                other => {
                    eprintln!("Unknown rates flag: {}", other);
                    usage();
                    std::process::exit(1);
                }
            }
        }

        "scan" => {
            let mut target = ".".to_string();
            let mut stubs = false;
            let mut i = 2;
            while i < args.len() {
                match args[i].as_str() {
                    "--generate-stubs" => stubs = true,
                    _ if !args[i].starts_with('-') => target = args[i].clone(),
                    _ => {}
                }
                i += 1;
            }

            let result = scanner::scan_directory(Path::new(&target));

            if result.cost_points.is_empty() {
                println!(
                    "Scanned {} file(s). No cost points found.",
                    result.files_scanned
                );
                std::process::exit(0);
            }

            println!("\nScanned {} file(s)\n", result.files_scanned);

            let auto: Vec<_> = result
                .cost_points
                .iter()
                .filter(|cp| cp.auto_instrumented)
                .collect();
            let manual: Vec<_> = result
                .cost_points
                .iter()
                .filter(|cp| !cp.auto_instrumented)
                .collect();

            if !auto.is_empty() {
                println!("AUTO-INSTRUMENTED");
                for cp in &auto {
                    println!("  [auto] {}:{} {}", cp.file, cp.line, cp.description);
                }
            }

            if !manual.is_empty() {
                println!("\nNEED record_cost()");
                for cp in &manual {
                    println!(
                        "  [manual] {}:{} {} ({})",
                        cp.file, cp.line, cp.description, cp.provider
                    );
                }
            }

            println!("\nSUMMARY");
            println!("  {} auto-instrumented", result.auto_count());
            println!("  {} need record_cost()", result.manual_count());

            if stubs && result.manual_count() > 0 {
                println!("\nGENERATED STUBS:\n");
                print!("{}", scanner::generate_stubs(&result));
            }
        }

        other => {
            eprintln!("Unknown command: {}", other);
            usage();
            std::process::exit(1);
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    // -----------------------------------------------------------------------
    // status tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_status_missing_db() {
        let result = cmd_status("/nonexistent/path/that/does/not/exist.db");
        assert!(result.is_err(), "should error for missing DB");
        let msg = result.unwrap_err();
        assert!(
            msg.contains("Cannot open database"),
            "expected helpful error, got: {}",
            msg
        );
    }

    #[test]
    fn test_status_prints_db_info() {
        // Create a temporary SQLite DB with the dexcost schema
        let dir = tempdir().expect("tempdir");
        let db_path = dir.path().join("test.db");
        let db_str = db_path.to_str().unwrap();

        // Bootstrap schema manually (same DDL as EventBuffer)
        let conn = Connection::open(db_str).unwrap();
        conn.execute_batch(
            "
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                task_type TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                metadata TEXT,
                llm_cost_usd TEXT,
                external_cost_usd TEXT,
                compute_cost_usd TEXT,
                total_cost_usd TEXT,
                total_input_tokens INTEGER,
                total_output_tokens INTEGER,
                total_cached_tokens INTEGER,
                retry_count INTEGER DEFAULT 0,
                retry_cost_usd TEXT DEFAULT '0',
                failure_count INTEGER DEFAULT 0,
                customer_id TEXT,
                project_id TEXT,
                parent_task_id TEXT,
                experiment_id TEXT,
                variant TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cached_tokens INTEGER,
                service_name TEXT,
                cost_usd TEXT NOT NULL,
                latency_ms INTEGER,
                cost_confidence TEXT NOT NULL DEFAULT 'exact',
                pricing_source TEXT,
                pricing_version TEXT,
                is_retry INTEGER DEFAULT 0,
                retry_reason TEXT,
                retry_of TEXT,
                details TEXT,
                timestamp TEXT NOT NULL,
                sync_status TEXT NOT NULL DEFAULT 'pending'
            );
        ",
        )
        .unwrap();

        // Insert one task and two events (one pending, one synced)
        conn.execute(
            "INSERT INTO tasks (task_id, task_type, status, started_at) VALUES ('t1', 'test', 'pending', '2024-01-01T00:00:00Z')",
            [],
        ).unwrap();
        conn.execute(
            "INSERT INTO events (event_id, task_id, event_type, cost_usd, timestamp, sync_status) VALUES ('e1', 't1', 'llm_call', '0.005', '2024-01-01T00:00:00Z', 'pending')",
            [],
        ).unwrap();
        conn.execute(
            "INSERT INTO events (event_id, task_id, event_type, cost_usd, timestamp, sync_status) VALUES ('e2', 't1', 'llm_call', '0.010', '2024-01-01T00:00:01Z', 'synced')",
            [],
        ).unwrap();
        drop(conn);

        let output = cmd_status(db_str).expect("status should succeed");

        assert!(
            output.contains("Tasks:         1"),
            "tasks count missing: {}",
            output
        );
        assert!(
            output.contains("Events:        2"),
            "events count missing: {}",
            output
        );
        assert!(
            output.contains("Pending sync:  1"),
            "pending count missing: {}",
            output
        );
    }

    // -----------------------------------------------------------------------
    // rates tests
    // -----------------------------------------------------------------------

    fn row(service: &str, per: &str, cost: &str) -> RateRow {
        RateRow {
            service: service.to_string(),
            per: per.to_string(),
            cost_usd: cost.to_string(),
        }
    }

    #[test]
    fn test_rates_import_list_yaml() {
        let dir = tempdir().expect("tempdir");
        let rates_file = dir.path().join("rates.yaml");

        // Canonical YAML `rates:` mapping — the format the SDK reads/writes.
        std::fs::write(
            &rates_file,
            "rates:\n  stripe:\n    per: per_transaction\n    cost_usd: \"0.029\"\n  twilio:\n    per: per_sms\n    cost_usd: \"0.0075\"\n",
        )
        .unwrap();

        let rates = cmd_rates_import(rates_file.to_str().unwrap()).expect("import should succeed");

        assert_eq!(rates.len(), 2);
        assert!(rates.contains_key("stripe"));
        assert!(rates.contains_key("twilio"));

        let listing = cmd_rates_list(&rates);
        assert!(listing.contains("stripe"), "listing: {}", listing);
        assert!(listing.contains("twilio"), "listing: {}", listing);
        assert!(listing.contains("0.029"), "listing: {}", listing);
        assert!(listing.contains("0.0075"), "listing: {}", listing);
    }

    // Legacy flat JSON arrays are still accepted on import (backward compat).
    #[test]
    fn test_rates_import_legacy_json_array() {
        let dir = tempdir().expect("tempdir");
        let rates_file = dir.path().join("legacy.json");
        let json = serde_json::json!([
            {"service": "stripe", "per": "per_transaction", "cost_usd": "0.029"}
        ]);
        std::fs::write(&rates_file, json.to_string()).unwrap();

        let rates = cmd_rates_import(rates_file.to_str().unwrap()).expect("legacy import");
        assert_eq!(rates.len(), 1);
        assert_eq!(rates["stripe"].cost_usd, "0.029");
    }

    #[test]
    fn test_rates_list_empty() {
        let rates: HashMap<String, RateRow> = HashMap::new();
        let listing = cmd_rates_list(&rates);
        assert!(
            listing.contains("No rates registered"),
            "expected empty message, got: {}",
            listing
        );
    }

    // The CLI export must produce the canonical YAML `rates:` format.
    #[test]
    fn test_rates_export_writes_yaml_rates_key() {
        let dir = tempdir().expect("tempdir");
        let export_path = dir.path().join("exported.yaml");

        let mut rates = HashMap::new();
        rates.insert("sendgrid".to_string(), row("sendgrid", "per_email", "0.0001"));

        cmd_rates_export(export_path.to_str().unwrap(), &rates).expect("export should succeed");

        let contents = std::fs::read_to_string(&export_path).unwrap();
        assert!(
            contents.starts_with("rates:"),
            "CLI export must use the top-level `rates:` key; got:\n{}",
            contents
        );
        assert!(contents.contains("sendgrid"));
        assert!(contents.contains("per: per_email"));
    }

    #[test]
    fn test_rates_export_roundtrip() {
        let dir = tempdir().expect("tempdir");
        let export_path = dir.path().join("exported.yaml");

        let mut rates = HashMap::new();
        rates.insert("sendgrid".to_string(), row("sendgrid", "per_email", "0.0001"));

        cmd_rates_export(export_path.to_str().unwrap(), &rates).expect("export should succeed");

        let reimported =
            cmd_rates_import(export_path.to_str().unwrap()).expect("reimport should succeed");

        assert_eq!(reimported.len(), 1);
        assert!(reimported.contains_key("sendgrid"));
        assert_eq!(reimported["sendgrid"].cost_usd, "0.0001");
    }

    // Fix 3: a file exported by the CLI must re-import cleanly via the library
    // `RateRegistry` — i.e. CLI export and library import round-trip.
    #[test]
    fn test_cli_export_reimports_via_library_registry() {
        let dir = tempdir().expect("tempdir");
        let export_path = dir.path().join("rates.yaml");

        let mut rates = HashMap::new();
        rates.insert("stripe".to_string(), row("stripe", "per_transaction", "0.029"));
        rates.insert("twilio".to_string(), row("twilio", "per_sms", "0.0075"));

        // CLI writes the file.
        cmd_rates_export(export_path.to_str().unwrap(), &rates).expect("CLI export");

        // The library RateRegistry reads it back without a flat-JSON fallback.
        let mut registry = RateRegistry::new();
        let loaded = registry
            .load_from_file(&export_path)
            .expect("library RateRegistry must load the CLI-exported file");
        assert_eq!(loaded, 2, "library should load both CLI-exported rates");
        assert_eq!(
            registry.get("stripe").expect("stripe rate").cost_usd,
            "0.029".parse().unwrap()
        );
        assert_eq!(registry.get("twilio").expect("twilio rate").per, "per_sms");

        // And a registry exported by the library re-imports via the CLI —
        // proving the round-trip works in both directions.
        let lib_path = dir.path().join("lib_rates.yaml");
        registry.save_to_file(&lib_path).expect("library export");
        let via_cli = cmd_rates_import(lib_path.to_str().unwrap())
            .expect("CLI must import a library-exported file");
        assert_eq!(via_cli.len(), 2);
        assert_eq!(via_cli["stripe"].cost_usd, "0.029");
    }

    #[test]
    fn test_rates_import_invalid_content() {
        let dir = tempdir().expect("tempdir");
        let bad_file = dir.path().join("bad.yaml");
        std::fs::write(&bad_file, "not yaml: [[[ {{{").unwrap();

        let result = cmd_rates_import(bad_file.to_str().unwrap());
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("Cannot import rates"));
    }

    #[test]
    fn test_rates_import_invalid_cost() {
        let dir = tempdir().expect("tempdir");
        let bad_file = dir.path().join("bad_cost.yaml");
        std::fs::write(
            &bad_file,
            "rates:\n  x:\n    per: call\n    cost_usd: \"not_a_number\"\n",
        )
        .unwrap();

        let result = cmd_rates_import(bad_file.to_str().unwrap());
        assert!(result.is_err(), "should reject invalid cost_usd");
    }
}
