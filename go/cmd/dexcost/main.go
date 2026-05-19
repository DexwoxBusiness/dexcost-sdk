// Command dexcost provides a CLI for inspecting the dexcost local database
// and managing non-LLM service rate registries.
//
// Usage:
//
//	dexcost status [--db path]
//	dexcost rates [--import path] [--export path] [--list]
package main

import (
	"database/sql"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/DexwoxBusiness/dexcost-go/pricing"
	"github.com/DexwoxBusiness/dexcost-go/scanner"

	// Pure-Go SQLite driver (no CGO required).
	_ "modernc.org/sqlite"
)

func main() {
	if len(os.Args) < 2 {
		printUsage(os.Stdout)
		os.Exit(1)
	}
	switch os.Args[1] {
	case "status":
		os.Exit(runStatus(os.Args[2:], os.Stdout))
	case "rates":
		os.Exit(runRates(os.Args[2:], os.Stdout))
	case "scan":
		os.Exit(runScan(os.Args[2:], os.Stdout))
	case "help", "--help", "-h":
		printUsage(os.Stdout)
		os.Exit(0)
	default:
		fmt.Fprintf(os.Stdout, "unknown command: %s\n\n", os.Args[1])
		printUsage(os.Stdout)
		os.Exit(1)
	}
}

func printUsage(w io.Writer) {
	fmt.Fprintln(w, "Usage: dexcost <command> [flags]")
	fmt.Fprintln(w, "")
	fmt.Fprintln(w, "Commands:")
	fmt.Fprintln(w, "  status   Show local database statistics")
	fmt.Fprintln(w, "  rates    Manage non-LLM service rate registry")
	fmt.Fprintln(w, "  scan     Scan codebase for cost points (AST-based, no API key needed)")
}

// defaultDBPath returns the default database path: ~/.dexcost/dexcost.db
func defaultDBPath() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return "dexcost.db"
	}
	return filepath.Join(home, ".dexcost", "dexcost.db")
}

// runStatus implements the `status` subcommand.
// Returns the process exit code (0 = success, 1 = error).
func runStatus(args []string, w io.Writer) int {
	fs := flag.NewFlagSet("status", flag.ContinueOnError)
	fs.SetOutput(w)
	dbFlag := fs.String("db", defaultDBPath(), "path to dexcost SQLite database")

	if err := fs.Parse(args); err != nil {
		return 1
	}

	dbPath := *dbFlag

	// Check if the file exists.
	if _, err := os.Stat(dbPath); os.IsNotExist(err) {
		fmt.Fprintf(w, "%s\nDatabase not found\n", dbPath)
		return 1
	}

	// Open the DB directly for COUNT queries.
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		fmt.Fprintf(w, "error: cannot open database: %v\n", err)
		return 1
	}
	defer db.Close()

	var eventCount, taskCount, pending, synced int
	if err := db.QueryRow("SELECT COUNT(*) FROM events").Scan(&eventCount); err != nil {
		fmt.Fprintf(w, "error: query events: %v\n", err)
		return 1
	}
	if err := db.QueryRow("SELECT COUNT(*) FROM tasks").Scan(&taskCount); err != nil {
		fmt.Fprintf(w, "error: query tasks: %v\n", err)
		return 1
	}
	if err := db.QueryRow("SELECT COUNT(*) FROM events WHERE sync_status = 'pending'").Scan(&pending); err != nil {
		fmt.Fprintf(w, "error: query pending: %v\n", err)
		return 1
	}
	if err := db.QueryRow("SELECT COUNT(*) FROM events WHERE sync_status = 'synced'").Scan(&synced); err != nil {
		fmt.Fprintf(w, "error: query synced: %v\n", err)
		return 1
	}

	var lastTask sql.NullString
	db.QueryRow("SELECT started_at FROM tasks ORDER BY started_at DESC LIMIT 1").Scan(&lastTask) //nolint:errcheck

	// Get pricing version from the embedded engine.
	pricingVersion := "(unavailable)"
	if engine, err := pricing.NewEngine(); err == nil {
		pricingVersion = engine.PricingVersion()
	}

	lastTaskStr := "(none)"
	if lastTask.Valid {
		lastTaskStr = lastTask.String
	}

	fmt.Fprintf(w, "DB location:       %s\n", dbPath)
	fmt.Fprintf(w, "Event count:       %d\n", eventCount)
	fmt.Fprintf(w, "Task count:        %d\n", taskCount)
	fmt.Fprintf(w, "Last task:         %s\n", lastTaskStr)
	fmt.Fprintf(w, "Pricing version:   %s\n", pricingVersion)
	fmt.Fprintf(w, "Pending sync:      %d\n", pending)
	fmt.Fprintf(w, "Synced:            %d\n", synced)

	return 0
}

// rateRow is a flattened rate entry used for `--list` table output.
type rateRow struct {
	Service string
	Per     string
	CostUSD string
}

// defaultRatesPath returns the default persistent rates store: ~/.dexcost/rates.yaml
func defaultRatesPath() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return "rates.yaml"
	}
	return filepath.Join(home, ".dexcost", "rates.yaml")
}

// runRates implements the `rates` subcommand.
// Returns the process exit code (0 = success, 1 = error).
//
// Rates live in a persistent store (default ~/.dexcost/rates.yaml): `--import`
// merges a file into the store, and `--list` / `--export` read from it. Without
// the store, each invocation started from an empty registry, so `--import`
// followed by `--list` showed nothing.
func runRates(args []string, w io.Writer) int {
	fs := flag.NewFlagSet("rates", flag.ContinueOnError)
	fs.SetOutput(w)
	importFlag := fs.String("import", "", "import rates from a YAML file into the persistent store")
	exportFlag := fs.String("export", "", "export the persistent store to a YAML file")
	listFlag := fs.Bool("list", false, "list all rates in the persistent store")
	storeFlag := fs.String("store", defaultRatesPath(), "path to the persistent rates store")

	if err := fs.Parse(args); err != nil {
		return 1
	}

	if *importFlag == "" && *exportFlag == "" && !*listFlag {
		fmt.Fprintln(w, "Usage: dexcost rates [--import path] [--export path] [--list] [--store path]")
		return 1
	}

	registry := pricing.NewRateRegistry()
	storePath := *storeFlag

	// Load the existing persistent store so list/export/import all see prior rates.
	if _, statErr := os.Stat(storePath); statErr == nil {
		if err := registry.LoadYAML(storePath); err != nil {
			fmt.Fprintf(w, "error: load rates store %s: %v\n", storePath, err)
			return 1
		}
	}

	// Import from YAML (canonical `rates:`-keyed format, shared with the
	// library RateRegistry and the Python SDK) and persist into the store.
	if *importFlag != "" {
		if err := registry.LoadYAML(*importFlag); err != nil {
			fmt.Fprintf(w, "error: import rates: %v\n", err)
			return 1
		}
		if err := os.MkdirAll(filepath.Dir(storePath), 0755); err != nil {
			fmt.Fprintf(w, "error: create rates store dir: %v\n", err)
			return 1
		}
		if err := registry.ExportYAML(storePath); err != nil {
			fmt.Fprintf(w, "error: persist rates store: %v\n", err)
			return 1
		}
		fmt.Fprintf(w, "Imported %d rate(s) from %s into %s\n",
			len(registry.GetAll()), *importFlag, storePath)
	}

	// List rates.
	if *listFlag {
		fmt.Fprintln(w, strings.Repeat("-", 60))
		fmt.Fprintf(w, "%-25s %-15s %s\n", "SERVICE", "PER", "COST_USD")
		fmt.Fprintln(w, strings.Repeat("-", 60))
		entries := listRegistryEntries(registry)
		for _, e := range entries {
			fmt.Fprintf(w, "%-25s %-15s %s\n", e.Service, e.Per, e.CostUSD)
		}
		fmt.Fprintln(w, strings.Repeat("-", 60))
		fmt.Fprintf(w, "Total: %d rate(s)\n", len(entries))
	}

	// Export to YAML (canonical `rates:`-keyed format).
	if *exportFlag != "" {
		if err := registry.ExportYAML(*exportFlag); err != nil {
			fmt.Fprintf(w, "error: export rates: %v\n", err)
			return 1
		}
		fmt.Fprintf(w, "Exported %d rate(s) to %s\n", len(registry.GetAll()), *exportFlag)
	}

	return 0
}

// ── scan command ─────────────────────────────────────────────────────

func runScan(args []string, w io.Writer) int {
	fs := flag.NewFlagSet("scan", flag.ContinueOnError)
	stubs := fs.Bool("generate-stubs", false, "Generate record_cost() stub snippets for manual cost points")
	if err := fs.Parse(args); err != nil {
		return 1
	}

	target := "."
	if fs.NArg() > 0 {
		target = fs.Arg(0)
	}

	result := scanner.ScanDirectory(target)

	if len(result.CostPoints) == 0 {
		fmt.Fprintf(w, "Scanned %d file(s). No cost points found.\n", result.FilesScanned)
		return 0
	}

	fmt.Fprintf(w, "\nScanned %d file(s)\n\n", result.FilesScanned)

	// Group by category
	var autoPoints, manualPoints []scanner.CostPoint
	for _, cp := range result.CostPoints {
		if cp.AutoInstrumented {
			autoPoints = append(autoPoints, cp)
		} else {
			manualPoints = append(manualPoints, cp)
		}
	}

	if len(autoPoints) > 0 {
		fmt.Fprintln(w, "AUTO-INSTRUMENTED")
		for _, cp := range autoPoints {
			fmt.Fprintf(w, "  [auto] %s:%d  %s\n", cp.File, cp.Line, cp.Description)
		}
	}

	if len(manualPoints) > 0 {
		fmt.Fprintln(w, "\nNEED RecordCost()")
		for _, cp := range manualPoints {
			fmt.Fprintf(w, "  [manual] %s:%d  %s (%s)\n", cp.File, cp.Line, cp.Description, cp.Provider)
		}
	}

	fmt.Fprintf(w, "\nSUMMARY\n")
	fmt.Fprintf(w, "  %d auto-instrumented\n", result.AutoCount())
	fmt.Fprintf(w, "  %d need RecordCost()\n", result.ManualCount())

	if *stubs && result.ManualCount() > 0 {
		fmt.Fprintf(w, "\nGENERATED STUBS:\n\n")
		fmt.Fprint(w, scanner.GenerateStubs(result))
	}

	return 0
}

// listRegistryEntries returns rate entries from the registry using GetAll.
// Results are sorted by service name for deterministic output.
func listRegistryEntries(registry *pricing.RateRegistry) []rateRow {
	all := registry.GetAll()
	result := make([]rateRow, 0, len(all))
	for _, re := range all {
		result = append(result, rateRow{
			Service: re.Service,
			Per:     re.Per,
			CostUSD: re.CostUSD.String(),
		})
	}
	sort.Slice(result, func(i, j int) bool {
		return result[i].Service < result[j].Service
	})
	return result
}
