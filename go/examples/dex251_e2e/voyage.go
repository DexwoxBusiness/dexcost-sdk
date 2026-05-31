package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"time"

	"github.com/google/uuid"
	"github.com/shopspring/decimal"
	"golang.org/x/sync/errgroup"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
)

const (
	voyageEmbedEndpoint = "https://api.voyageai.com/v1/embeddings"
	voyageEmbedModel    = "voyage-3-large"
	// voyage-3-large pricing: $0.18 per 1M tokens.
	voyageUSDPer1MTokens = 0.18
)

// VoyageClient wraps the Voyage AI embeddings API with Dexcost cost tracking.
type VoyageClient struct {
	httpClient *http.Client
	apiKey     string
	buffer     core.Buffer
	budget     *BudgetTracker
}

// NewVoyageClient creates a Voyage client.
func NewVoyageClient(apiKey string, buffer core.Buffer, budget *BudgetTracker) *VoyageClient {
	return &VoyageClient{
		httpClient: &http.Client{Timeout: 30 * time.Second},
		apiKey:     apiKey,
		buffer:     buffer,
		budget:     budget,
	}
}

// embeddingResponse is the JSON shape returned by Voyage.
type embeddingResponse struct {
	Object string `json:"object"`
	Data   []struct {
		Object    string    `json:"object"`
		Embedding []float64 `json:"embedding"`
		Index     int       `json:"index"`
	} `json:"data"`
	Model string `json:"model"`
	Usage struct {
		TotalTokens int `json:"total_tokens"`
	} `json:"usage"`
}

// EmbedDocuments fan-outs batch-embedding requests via errgroup.
// Each batch is sent concurrently; results are merged in order.
func (c *VoyageClient) EmbedDocuments(ctx context.Context, taskID string, texts []string) ([][]float64, error) {
	const batchSize = 4 // small batches = many requests = many external_cost + retry events

	var batches [][]string
	for i := 0; i < len(texts); i += batchSize {
		end := i + batchSize
		if end > len(texts) {
			end = len(texts)
		}
		batches = append(batches, texts[i:end])
	}

	type batchResult struct {
		idx        int
		embeddings [][]float64
		tokens     int
	}

	results := make([]batchResult, len(batches))
	g, ctx := errgroup.WithContext(ctx)

	for i, batch := range batches {
		i, batch := i, batch // capture loop vars for goroutine closure
		g.Go(func() error {
			// Pre-mortem mitigation: recover from panics so errgroup doesn't lose data.
			defer func() {
				if r := recover(); r != nil {
					slog.Error("panic in voyage embed goroutine", "recover", r, "batch", i)
				}
			}()
			// Retry loop with deliberate timeout trigger on first attempt.
			embeddings, tokens, err := c.embedWithRetry(ctx, taskID, batch, i)
			if err != nil {
				return fmt.Errorf("batch %d: %w", i, err)
			}
			results[i] = batchResult{idx: i, embeddings: embeddings, tokens: tokens}
			return nil
		})
	}

	if err := g.Wait(); err != nil {
		return nil, err
	}

	// Flatten results preserving order.
	var all [][]float64
	for _, r := range results {
		all = append(all, r.embeddings...)
	}
	return all, nil
}

// embedWithRetry sends a single batch, recording retry markers on transient failures.
// The first attempt uses a very short timeout to deliberately trigger a timeout retry.
func (c *VoyageClient) embedWithRetry(ctx context.Context, taskID string, batch []string, batchIdx int) ([][]float64, int, error) {
	const maxRetries = 3
	var embeddings [][]float64
	var tokens int
	var retryOf *uuid.UUID

	for attempt := 0; attempt < maxRetries; attempt++ {
		timeout := 30 * time.Second
		if attempt == 0 {
			// Deliberately trigger a timeout on the first attempt for some batches.
			if batchIdx%4 == 0 {
				timeout = 1 * time.Millisecond
			}
		}

		reqCtx, cancel := context.WithTimeout(ctx, timeout)
		emb, tok, err := c.embedBatch(reqCtx, batch)
		cancel()

		if err == nil {
			embeddings = emb
			tokens = tok
			break
		}

		// Record retry marker, linked to the first failed attempt.
		c.recordRetry(taskID, "voyage_timeout", batchIdx, attempt, retryOf)
		if retryOf == nil {
			// The first failure creates a synthetic "failed" event to act as retry_of.
			failedEvent := core.NewEvent(uuid.MustParse(taskID), core.EventTypeExternalCost)
			failedEvent.ServiceName = "voyageai-embed"
			failedEvent.CostUSD = decimal.Zero
			failedEvent.CostConfidence = core.CostConfidenceUnknown
			failedEvent.ErrorType = "timeout"
			failedEvent.Details["batch_index"] = batchIdx
			failedEvent.Details["attempt"] = attempt
			_ = c.buffer.InsertEvent(failedEvent)
			retryOf = &failedEvent.EventID
		}
		slog.Warn("voyage embed retry",
			"batch", batchIdx,
			"attempt", attempt,
			"error", err,
		)

		if attempt < maxRetries-1 {
			backoff := time.Duration(attempt+1) * 500 * time.Millisecond
			time.Sleep(backoff)
		} else {
			return nil, 0, fmt.Errorf("batch %d exhausted retries: %w", batchIdx, err)
		}
	}

	// Record external_cost event for this batch.
	costUSD := decimal.NewFromFloat(float64(tokens) / 1_000_000 * voyageUSDPer1MTokens)
	if c.budget != nil {
		if err := c.budget.CheckAndAdd(costUSD, "voyageai-embed"); err != nil {
			return nil, 0, err
		}
	}
	event := core.NewEvent(uuid.MustParse(taskID), core.EventTypeExternalCost)
	event.ServiceName = "voyageai-embed"
	event.CostUSD = costUSD
	event.CostConfidence = core.CostConfidenceExact
	event.PricingSource = core.PricingSourceManual
	event.Details["model"] = voyageEmbedModel
	event.Details["batch_index"] = batchIdx
	event.Details["batch_size"] = len(batch)
	event.Details["tokens"] = tokens
	if len(embeddings) > 0 {
		event.Details["dimensions"] = len(embeddings[0])
	}
	if err := c.buffer.InsertEvent(event); err != nil {
		slog.Warn("failed to record voyage external_cost", "error", err)
	}

	return embeddings, tokens, nil
}

func (c *VoyageClient) embedBatch(ctx context.Context, texts []string) ([][]float64, int, error) {
	body, err := json.Marshal(map[string]interface{}{
		"input": texts,
		"model": voyageEmbedModel,
	})
	if err != nil {
		return nil, 0, err
	}

	req, err := http.NewRequestWithContext(ctx, "POST", voyageEmbedEndpoint, bytes.NewReader(body))
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("Authorization", "Bearer "+c.apiKey)
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()

	if resp.StatusCode == 429 {
		return nil, 0, fmt.Errorf("rate limited (429)")
	}
	if resp.StatusCode >= 400 {
		var errBody map[string]interface{}
		_ = json.NewDecoder(resp.Body).Decode(&errBody)
		return nil, 0, fmt.Errorf("voyage %d: %v", resp.StatusCode, errBody)
	}

	var data embeddingResponse
	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		return nil, 0, fmt.Errorf("decode response: %w", err)
	}

	out := make([][]float64, len(texts))
	for _, d := range data.Data {
		if d.Index >= 0 && d.Index < len(out) {
			out[d.Index] = d.Embedding
		}
	}
	return out, data.Usage.TotalTokens, nil
}

func (c *VoyageClient) recordRetry(taskID, reason string, batchIdx, attempt int, retryOf *uuid.UUID) {
	event := core.NewEvent(uuid.MustParse(taskID), core.EventTypeRetryMarker)
	event.IsRetry = true
	event.RetryReason = reason
	event.CostUSD = decimal.Zero
	event.RetryOf = retryOf
	event.Details["service"] = "voyageai-embed"
	event.Details["batch_index"] = batchIdx
	event.Details["attempt"] = attempt
	if err := c.buffer.InsertEvent(event); err != nil {
		slog.Warn("failed to record retry marker", "error", err)
	}
}
