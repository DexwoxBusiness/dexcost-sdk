package main

import (
	"log/slog"
	"math"
	"time"

	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-sdk/go/core"
)

// VectorStore is an in-memory store for document embeddings with cosine-similarity search.
type VectorStore struct {
	docs       []string
	embeddings [][]float64
	buffer     core.Buffer
}

// NewVectorStore creates an empty vector store.
func NewVectorStore(buffer core.Buffer) *VectorStore {
	return &VectorStore{buffer: buffer}
}

// Add inserts documents and their embeddings, recording compute_cost events.
func (s *VectorStore) Add(taskID string, docs []string, embeddings [][]float64) {
	start := time.Now()
	s.docs = append(s.docs, docs...)
	s.embeddings = append(s.embeddings, embeddings...)
	dur := time.Since(start)

	// Record compute_cost for indexing (chunking + vector insertion).
	costUSD := decimal.NewFromFloat(float64(len(docs)) * 0.00001) // nominal compute cost
	event := core.NewEvent(mustParseUUID(taskID), core.EventTypeComputeCost)
	event.ServiceName = "vector-index"
	event.CostUSD = costUSD
	event.CostConfidence = core.CostConfidenceComputed
	event.PricingSource = core.PricingSourceManual
	event.Details["operation"] = "index"
	event.Details["documents"] = len(docs)
	event.Details["duration_ms"] = dur.Milliseconds()
	if err := s.buffer.InsertEvent(event); err != nil {
		slog.Warn("failed to record compute_cost", "error", err)
	}
}

// Search returns the top-k most similar documents to the query embedding.
func (s *VectorStore) Search(taskID string, query []float64, topK int) []string {
	start := time.Now()

	scores := make([]struct {
		idx   int
		score float64
	}, len(s.embeddings))
	for i, emb := range s.embeddings {
		scores[i] = struct {
			idx   int
			score float64
		}{idx: i, score: cosineSimilarity(query, emb)}
	}

	// Simple selection sort for topK (k is small, N is ~100-200).
	for i := 0; i < topK && i < len(scores); i++ {
		maxIdx := i
		for j := i + 1; j < len(scores); j++ {
			if scores[j].score > scores[maxIdx].score {
				maxIdx = j
			}
		}
		scores[i], scores[maxIdx] = scores[maxIdx], scores[i]
	}

	var results []string
	for i := 0; i < topK && i < len(scores); i++ {
		results = append(results, s.docs[scores[i].idx])
	}

	dur := time.Since(start)

	// Record compute_cost for similarity search.
	costUSD := decimal.NewFromFloat(float64(len(s.embeddings)) * 0.000001) // nominal per-doc compute
	event := core.NewEvent(mustParseUUID(taskID), core.EventTypeComputeCost)
	event.ServiceName = "vector-search"
	event.CostUSD = costUSD
	event.CostConfidence = core.CostConfidenceComputed
	event.PricingSource = core.PricingSourceManual
	event.Details["operation"] = "cosine_similarity"
	event.Details["candidates"] = len(s.embeddings)
	event.Details["top_k"] = topK
	event.Details["duration_ms"] = dur.Milliseconds()
	if err := s.buffer.InsertEvent(event); err != nil {
		slog.Warn("failed to record compute_cost", "error", err)
	}

	return results
}

func cosineSimilarity(a, b []float64) float64 {
	if len(a) != len(b) {
		return 0
	}
	var dot, na, nb float64
	for i := range a {
		dot += a[i] * b[i]
		na += a[i] * a[i]
		nb += b[i] * b[i]
	}
	if na == 0 || nb == 0 {
		return 0
	}
	return dot / (math.Sqrt(na) * math.Sqrt(nb))
}
