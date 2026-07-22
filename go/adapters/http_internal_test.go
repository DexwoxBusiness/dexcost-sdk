package adapters

import (
	"testing"

	"github.com/DexwoxBusiness/dexcost-sdk/go/pricing"
)

func TestProviderObservationEventIDIsStableAcrossSDKLanguages(t *testing.T) {
	observation := pricing.ServiceUsageObservation{
		ProviderName:     "assemblyai",
		ServiceKey:       "assemblyai_transcription",
		ProviderRecordID: "aa-123",
	}
	got := providerObservationEventID(&observation).String()
	if got != "2dc521b3-742a-5f61-9942-c4a59e6935f6" {
		t.Fatalf("got %s", got)
	}
}
