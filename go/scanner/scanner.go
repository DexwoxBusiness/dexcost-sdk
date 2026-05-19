// Package scanner provides AST-based static analysis to detect cost-generating
// patterns in Go source files.  Uses only stdlib go/ast and go/parser — no
// external dependencies.
package scanner

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
)

// CostPoint represents a detected cost-generating call site in source code.
type CostPoint struct {
	File              string `json:"file"`
	Line              int    `json:"line"`
	Category          string `json:"category"` // "llm", "http", "service", "framework", "embedding", "speech", "image"
	Provider          string `json:"provider"`
	Description       string `json:"description"`
	AutoInstrumented  bool   `json:"auto_instrumented"`
	ImportPath        string `json:"import_path"`
}

// ScanResult holds the aggregated output of a directory scan.
type ScanResult struct {
	CostPoints   []CostPoint `json:"cost_points"`
	FilesScanned int         `json:"files_scanned"`
}

// AutoCount returns the number of auto-instrumented cost points.
func (r ScanResult) AutoCount() int {
	n := 0
	for _, cp := range r.CostPoints {
		if cp.AutoInstrumented {
			n++
		}
	}
	return n
}

// ManualCount returns the number of cost points requiring manual tracking.
func (r ScanResult) ManualCount() int {
	n := 0
	for _, cp := range r.CostPoints {
		if !cp.AutoInstrumented {
			n++
		}
	}
	return n
}

// ── Skip directories ────────────────────────────────────────────────

var skipDirs = map[string]bool{
	"vendor": true, ".git": true, "node_modules": true,
	"testdata": true, ".cache": true, "dist": true, "build": true,
}

// ── Import → provider mapping ───────────────────────────────────────

// importPattern maps an import path prefix to a provider name and whether
// dexcost auto-instruments calls through this import.
type importPattern struct {
	prefix          string
	provider        string
	autoInstrumented bool
}

var knownImports = []importPattern{
	// LLM providers
	{"github.com/sashabaranov/go-openai", "openai", false},
	{"github.com/openai/openai-go", "openai", false},
	{"github.com/anthropics/anthropic-sdk-go", "anthropic", false},
	{"github.com/google/generative-ai-go", "google-ai", false},
	{"google.golang.org/genai", "google-ai", false},
	{"github.com/cohere-ai/cohere-go", "cohere", false},
	{"github.com/mistralai/client-go", "mistral", false},
	{"github.com/replicate/replicate-go", "replicate", false},
	{"github.com/ollama/ollama", "ollama", false},

	// Missing LLM providers
	{"github.com/groq/groq-go", "groq", false},
	{"github.com/deepseek-ai/deepseek-go", "deepseek", false},
	{"github.com/together-ai/together-go", "together", false},

	// Agent frameworks
	{"github.com/tmc/langchaingo", "langchaingo", false},
	{"github.com/cloudwego/eino", "eino", false},
	{"github.com/mozilla-ai/any-llm-go", "anyllm", false},
	{"github.com/aiochan/crewai-go", "crewai", false},

	// Bedrock-specific (not just generic AWS) — must come before generic AWS
	{"github.com/aws/aws-sdk-go-v2/service/bedrockruntime", "aws-bedrock", false},

	// Cloud / Infrastructure
	{"github.com/aws/aws-sdk-go-v2", "aws", false},
	{"github.com/aws/aws-sdk-go", "aws", false},
	{"cloud.google.com/go", "gcp", false},
	{"github.com/Azure/azure-sdk-for-go", "azure", false},

	// Databases
	{"go.mongodb.org/mongo-driver", "mongodb", false},
	{"github.com/elastic/go-elasticsearch", "elasticsearch", false},
	{"github.com/redis/go-redis", "redis", false},
	{"github.com/go-redis/redis", "redis", false},

	// Vector databases
	{"github.com/pinecone-io/go-pinecone", "pinecone", false},
	{"github.com/weaviate/weaviate-go-client", "weaviate", false},
	{"github.com/milvus-io/milvus-sdk-go", "milvus", false},
	{"github.com/qdrant/go-client", "qdrant", false},

	// Payments
	{"github.com/stripe/stripe-go", "stripe", false},

	// Messaging
	{"github.com/twilio/twilio-go", "twilio", false},
	{"github.com/sendgrid/sendgrid-go", "sendgrid", false},
	{"github.com/resend/resend-go", "resend", false},
	{"github.com/slack-go/slack", "slack", false},

	// Geo / Maps
	{"googlemaps.github.io/maps", "google-maps", false},

	// Scraping
	{"github.com/gocolly/colly", "colly", false},

	// HTTP clients (need manual cost tracking)
	{"net/http", "net/http", false},
}

// ── Call patterns ───────────────────────────────────────────────────

// callPattern matches a method call on an object whose import is known.
type callPattern struct {
	receiverPkg string // import short name to match (e.g. "openai")
	methodChain string // dot-separated method chain to look for
	category    string
	provider    string
	description string
	auto        bool
}

var callPatterns = []callPattern{
	// OpenAI
	{"openai", "CreateChatCompletion", "llm", "openai", "OpenAI chat completion", false},
	{"openai", "CreateCompletion", "llm", "openai", "OpenAI completion", false},
	{"openai", "CreateEmbeddings", "embedding", "openai", "OpenAI embedding", false},
	{"openai", "CreateImage", "image", "openai", "OpenAI DALL-E image", false},
	{"openai", "CreateTranscription", "speech", "openai", "OpenAI Whisper transcription", false},
	{"openai", "CreateSpeech", "speech", "openai", "OpenAI TTS", false},

	// Anthropic
	{"anthropic", "Messages", "llm", "anthropic", "Anthropic message", false},
	{"anthropic", "Complete", "llm", "anthropic", "Anthropic completion", false},

	// Google AI
	{"genai", "GenerateContent", "llm", "google-ai", "Google AI generate content", false},

	// Cohere
	{"cohere", "Chat", "llm", "cohere", "Cohere chat", false},
	{"cohere", "Generate", "llm", "cohere", "Cohere generate", false},
	{"cohere", "Embed", "embedding", "cohere", "Cohere embedding", false},

	// Replicate
	{"replicate", "Run", "llm", "replicate", "Replicate model run", false},

	// LangChainGo
	{"llms", "Call", "framework", "langchaingo", "LangChainGo LLM call", false},
	{"llms", "GenerateContent", "framework", "langchaingo", "LangChainGo generate content", false},
	{"chains", "Call", "framework", "langchaingo", "LangChainGo chain call", false},
	{"chains", "Run", "framework", "langchaingo", "LangChainGo chain run", false},
	{"agents", "Run", "framework", "langchaingo", "LangChainGo agent run", false},

	// MongoDB
	{"mongo", "Find", "service", "mongodb", "MongoDB find", false},
	{"mongo", "FindOne", "service", "mongodb", "MongoDB findOne", false},
	{"mongo", "InsertOne", "service", "mongodb", "MongoDB insertOne", false},
	{"mongo", "InsertMany", "service", "mongodb", "MongoDB insertMany", false},
	{"mongo", "UpdateOne", "service", "mongodb", "MongoDB updateOne", false},
	{"mongo", "DeleteOne", "service", "mongodb", "MongoDB deleteOne", false},
	{"mongo", "Aggregate", "service", "mongodb", "MongoDB aggregate", false},

	// Elasticsearch
	{"elasticsearch", "Search", "service", "elasticsearch", "Elasticsearch search", false},
	{"esapi", "Search", "service", "elasticsearch", "Elasticsearch search", false},

	// Redis
	{"redis", "Get", "service", "redis", "Redis GET", false},
	{"redis", "Set", "service", "redis", "Redis SET", false},
	{"redis", "Del", "service", "redis", "Redis DEL", false},

	// Stripe
	{"stripe", "New", "service", "stripe", "Stripe API call", false},
	{"paymentintent", "New", "service", "stripe", "Stripe PaymentIntent", false},
	{"charge", "New", "service", "stripe", "Stripe Charge", false},

	// Twilio
	{"twilio", "CreateMessage", "service", "twilio", "Twilio message", false},

	// Bedrock-specific
	{"bedrockruntime", "InvokeModel", "llm", "aws-bedrock", "AWS Bedrock invoke model", false},
	{"bedrockruntime", "InvokeModelWithResponseStream", "llm", "aws-bedrock", "AWS Bedrock streaming invoke", false},

	// Groq
	{"groq", "CreateChatCompletion", "llm", "groq", "Groq chat completion", false},

	// DeepSeek
	{"deepseek", "CreateChatCompletion", "llm", "deepseek", "DeepSeek chat completion", false},

	// HTTP calls
	{"http", "Get", "http", "net/http", "HTTP GET", false},
	{"http", "Post", "http", "net/http", "HTTP POST", false},
	{"http", "Do", "http", "net/http", "HTTP request", false},
}

// ── Scanner engine ──────────────────────────────────────────────────

// ScanDirectory walks a directory tree and scans all .go files for cost points.
func ScanDirectory(root string) ScanResult {
	result := ScanResult{}
	info, err := os.Stat(root)
	if err != nil {
		return result
	}
	if !info.IsDir() {
		root = filepath.Dir(root)
	}

	_ = filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return nil
		}
		if info.IsDir() {
			if skipDirs[info.Name()] {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}

		result.FilesScanned++
		points := scanFile(path)
		result.CostPoints = append(result.CostPoints, points...)
		return nil
	})

	return result
}

// scanFile parses a single Go file and extracts cost points using AST analysis.
func scanFile(path string) []CostPoint {
	fset := token.NewFileSet()
	node, err := parser.ParseFile(fset, path, nil, parser.ImportsOnly|parser.ParseComments)
	if err != nil {
		// Try full parse for call detection
		node, err = parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			return nil
		}
	} else {
		// Re-parse with full AST for call detection
		node, err = parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			return nil
		}
	}

	var points []CostPoint
	seen := make(map[int]bool) // deduplicate by line number

	// Step 1: Collect imports and map short names to full paths.
	importMap := make(map[string]string) // shortName → full import path
	for _, imp := range node.Imports {
		importPath := strings.Trim(imp.Path.Value, `"`)
		shortName := filepath.Base(importPath)
		if imp.Name != nil {
			shortName = imp.Name.Name
		}
		// Handle hyphenated package names (e.g. "go-openai" → "openai" in code)
		shortName = strings.TrimPrefix(shortName, "go-")
		importMap[shortName] = importPath
	}

	// Step 2: Detect import-level cost points (known SDKs).
	for _, imp := range node.Imports {
		importPath := strings.Trim(imp.Path.Value, `"`)
		for _, kp := range knownImports {
			if strings.HasPrefix(importPath, kp.prefix) {
				line := fset.Position(imp.Pos()).Line
				if !seen[line] {
					seen[line] = true
					points = append(points, CostPoint{
						File:             path,
						Line:             line,
						Category:         "import",
						Provider:         kp.provider,
						Description:      fmt.Sprintf("Import: %s", importPath),
						AutoInstrumented: kp.autoInstrumented,
						ImportPath:       importPath,
					})
				}
				break
			}
		}
	}

	// Step 3: Walk AST for function/method calls matching known patterns.
	ast.Inspect(node, func(n ast.Node) bool {
		call, ok := n.(*ast.CallExpr)
		if !ok {
			return true
		}

		callStr := resolveCallExpr(call.Fun)
		if callStr == "" {
			return true
		}

		for _, pat := range callPatterns {
			// Match: either "pkg.Method" or "receiver.Method" where receiver
			// is from a known import.
			parts := strings.Split(callStr, ".")
			if len(parts) < 2 {
				continue
			}
			method := parts[len(parts)-1]
			pkgOrVar := parts[0]

			if method == pat.methodChain && pkgOrVar == pat.receiverPkg {
				line := fset.Position(call.Pos()).Line
				if !seen[line] {
					seen[line] = true
					points = append(points, CostPoint{
						File:             path,
						Line:             line,
						Category:         pat.category,
						Provider:         pat.provider,
						Description:      pat.description,
						AutoInstrumented: pat.auto,
						ImportPath:       importMap[pkgOrVar],
					})
				}
				return true
			}

			// Also match deeper chains like client.Chat.Completions.Create
			if method == pat.methodChain {
				// Check if any part of the chain matches the receiver package
				for _, part := range parts[:len(parts)-1] {
					if strings.EqualFold(part, pat.receiverPkg) {
						line := fset.Position(call.Pos()).Line
						if !seen[line] {
							seen[line] = true
							points = append(points, CostPoint{
								File:             path,
								Line:             line,
								Category:         pat.category,
								Provider:         pat.provider,
								Description:      pat.description,
								AutoInstrumented: pat.auto,
								ImportPath:       importMap[pkgOrVar],
							})
						}
						return true
					}
				}
			}
		}

		return true
	})

	return points
}

// resolveCallExpr attempts to resolve a call expression to a dotted string.
// e.g., client.Chat.Completions.Create → "client.Chat.Completions.Create"
func resolveCallExpr(expr ast.Expr) string {
	switch e := expr.(type) {
	case *ast.SelectorExpr:
		prefix := resolveCallExpr(e.X)
		if prefix != "" {
			return prefix + "." + e.Sel.Name
		}
		return e.Sel.Name
	case *ast.Ident:
		return e.Name
	case *ast.CallExpr:
		// e.g., client.Method().Chain() — resolve inner call
		return resolveCallExpr(e.Fun)
	case *ast.IndexExpr:
		// e.g., generic type instantiation
		return resolveCallExpr(e.X)
	default:
		return ""
	}
}

// GenerateStubs produces record_cost snippets for manual cost points.
func GenerateStubs(result ScanResult) string {
	var sb strings.Builder

	auto := []CostPoint{}
	manual := []CostPoint{}
	for _, cp := range result.CostPoints {
		if cp.Category == "import" {
			continue
		}
		if cp.AutoInstrumented {
			auto = append(auto, cp)
		} else {
			manual = append(manual, cp)
		}
	}

	sb.WriteString("// ============================================================\n")
	sb.WriteString("// dexcost integration stubs\n")
	sb.WriteString("// Generated by: dexcost scan --generate-stubs\n")
	sb.WriteString("// ============================================================\n\n")

	sb.WriteString("// --- Step 1: Initialize dexcost ---\n")
	sb.WriteString("import (\n")
	sb.WriteString("    \"context\"\n\n")
	sb.WriteString("    dexcost \"github.com/DexwoxBusiness/dexcost-go\"\n")
	if len(manual) > 0 {
		sb.WriteString("    \"github.com/shopspring/decimal\"\n")
	}
	sb.WriteString(")\n\n")
	sb.WriteString("dexcost.Init(dexcost.Config{\n")
	sb.WriteString("    APIKey: \"dx_live_...\", // or set DEXCOST_API_KEY env var\n")
	sb.WriteString("})\n\n")

	sb.WriteString("// --- Step 2: Set customer context (in your request handler) ---\n")
	sb.WriteString("ctx := dexcost.SetContext(context.Background(), \"your_customer_id\", \"your_project_id\")\n\n")

	sb.WriteString("// --- Step 3: Track tasks ---\n")
	sb.WriteString("ctx, task := dexcost.StartTask(ctx, \"your_task_type\")\n")
	sb.WriteString("defer task.End(dexcost.StatusSuccess)\n\n")

	if len(manual) > 0 {
		sb.WriteString("// --- Manual cost tracking (for services not auto-instrumented) ---\n")
		for _, cp := range manual {
			fmt.Fprintf(&sb, "// %s:%d — %s\n", cp.File, cp.Line, cp.Description)
			fmt.Fprintf(&sb, "task.RecordCost(\"%s\", decimal.RequireFromString(\"0.00\")) // TODO: set actual cost\n\n", cp.Provider)
		}
	}

	if len(auto) > 0 {
		sb.WriteString("// --- Auto-instrumented (no code changes needed) ---\n")
		providers := map[string]int{}
		for _, cp := range auto {
			providers[cp.Provider]++
		}
		for provider, count := range providers {
			fmt.Fprintf(&sb, "// ✓ %s (%d call%s detected)\n", provider, count, pluralS(count))
		}
	}

	return sb.String()
}

func pluralS(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}
