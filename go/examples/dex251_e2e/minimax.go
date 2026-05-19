package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"time"

	"github.com/DexwoxBusiness/dexcost-go/clients"
	"github.com/DexwoxBusiness/dexcost-go/core"
	"github.com/DexwoxBusiness/dexcost-go/pricing"
	"github.com/google/uuid"
	"github.com/shopspring/decimal"
)

const (
	minimaxBaseURL = "https://api.minimax.io/anthropic"
	minimaxModel   = "MiniMax-M2.7"
)

// MiniMaxClient wraps the MiniMax Anthropic-compatible API.
type MiniMaxClient struct {
	httpClient *http.Client
	apiKey     string
	buffer     core.Buffer
	pricing    *pricing.Engine
	budget     *BudgetTracker
}

// NewMiniMaxClient creates a MiniMax client.
func NewMiniMaxClient(apiKey string, buffer core.Buffer, pricing *pricing.Engine, budget *BudgetTracker) *MiniMaxClient {
	return &MiniMaxClient{
		httpClient: &http.Client{Timeout: 60 * time.Second},
		apiKey:     apiKey,
		buffer:     buffer,
		pricing:    pricing,
		budget:     budget,
	}
}

// Query sends a chat request to MiniMax and records the LLM cost via the SDK.
// It implements a retry loop that records retry markers on transient failures.
func (c *MiniMaxClient) Query(ctx context.Context, taskID uuid.UUID, system string, messages []map[string]string) (string, error) {
	const maxRetries = 3
	var answer string
	var retryOf *uuid.UUID

	for attempt := 0; attempt < maxRetries; attempt++ {
		timeout := 60 * time.Second
		if attempt == 0 {
			// Deliberately trigger a timeout on ~25% of first attempts.
			if time.Now().UnixNano()%4 == 0 {
				timeout = 1 * time.Millisecond
			}
		}

		reqCtx, cancel := context.WithTimeout(ctx, timeout)
		respMap, text, err := c.doChat(reqCtx, system, messages)
		cancel()

		if err == nil {
			// Budget check before recording cost.
			var inTok, outTok int
			if usage, ok := respMap["usage"].(map[string]interface{}); ok {
				inTok = intFromMap(usage, "input_tokens")
				outTok = intFromMap(usage, "output_tokens")
			}
			costResult := c.pricing.GetCost(minimaxModel, inTok, outTok, 0, 0)
			if c.budget != nil {
				if berr := c.budget.CheckAndAdd(costResult.CostUSD, "minimax"); berr != nil {
					return "", berr
				}
			}

			// Record the successful LLM call.
			_, recErr := clients.RecordAnthropicResponse(c.buffer, c.pricing, taskID, respMap)
			if recErr != nil {
				slog.Warn("failed to record anthropic response", "error", recErr)
			}
			answer = text
			break
		}

		// Record retry marker, linked to first failed attempt.
		c.recordRetry(taskID, "minimax_timeout", attempt, retryOf)
		if retryOf == nil {
			failedEvent := core.NewEvent(taskID, core.EventTypeLLMCall)
			failedEvent.Provider = "minimax"
			failedEvent.Model = minimaxModel
			failedEvent.CostUSD = decimal.Zero
			failedEvent.CostConfidence = core.CostConfidenceUnknown
			failedEvent.ErrorType = "timeout"
			failedEvent.Details["attempt"] = attempt
			_ = c.buffer.InsertEvent(failedEvent)
			retryOf = &failedEvent.EventID
		}
		slog.Warn("minimax retry", "attempt", attempt, "error", err)

		if attempt < maxRetries-1 {
			backoff := time.Duration(attempt+1) * time.Second
			time.Sleep(backoff)
		} else {
			return "", fmt.Errorf("minimax query exhausted retries: %w", err)
		}
	}

	return answer, nil
}

func (c *MiniMaxClient) doChat(ctx context.Context, system string, messages []map[string]string) (map[string]interface{}, string, error) {
	payload := map[string]interface{}{
		"model":       minimaxModel,
		"max_tokens":  256,
		"temperature": 0.7,
		"messages":    messages,
	}
	if system != "" {
		payload["system"] = system
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return nil, "", err
	}

	req, err := http.NewRequestWithContext(ctx, "POST", minimaxBaseURL+"/v1/messages", bytes.NewReader(body))
	if err != nil {
		return nil, "", err
	}
	req.Header.Set("Authorization", "Bearer "+c.apiKey)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("anthropic-version", "2023-06-01")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		var errBody map[string]interface{}
		_ = json.NewDecoder(resp.Body).Decode(&errBody)
		return nil, "", fmt.Errorf("minimax %d: %v", resp.StatusCode, errBody)
	}

	var data map[string]interface{}
	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		return nil, "", fmt.Errorf("decode response: %w", err)
	}

	// Extract text from content blocks.
	text := ""
	if content, ok := data["content"].([]interface{}); ok {
		for _, block := range content {
			if m, ok := block.(map[string]interface{}); ok {
				if m["type"] == "text" {
					if t, ok := m["text"].(string); ok {
						text += t
					}
				}
			}
		}
	}

	return data, text, nil
}

func (c *MiniMaxClient) recordRetry(taskID uuid.UUID, reason string, attempt int, retryOf *uuid.UUID) {
	event := core.NewEvent(taskID, core.EventTypeRetryMarker)
	event.IsRetry = true
	event.RetryReason = reason
	event.CostUSD = decimal.Zero
	event.RetryOf = retryOf
	event.Details["service"] = "minimax"
	event.Details["model"] = minimaxModel
	event.Details["attempt"] = attempt
	if err := c.buffer.InsertEvent(event); err != nil {
		slog.Warn("failed to record retry marker", "error", err)
	}
}

// intFromMap safely extracts an int from a map[string]interface{}.
func intFromMap(m map[string]interface{}, key string) int {
	if m == nil {
		return 0
	}
	v, ok := m[key]
	if !ok {
		return 0
	}
	switch n := v.(type) {
	case int:
		return n
	case float64:
		return int(n)
	case json.Number:
		i, _ := n.Int64()
		return int(i)
	default:
		return 0
	}
}
