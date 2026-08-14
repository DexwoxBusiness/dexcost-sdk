package core

import "testing"

func TestParsePmonUtilizationUsesCanonicalColumns(t *testing.T) {
	raw := "# gpu pid type sm mem enc dec command\n0 4242 C 75 20 - - whisper\n"
	parsed := parsePmonUtilization(raw, 1_234_000)
	samples := parsed[4242]
	if len(samples) != 1 || samples[0].SMUtil != 75 || samples[0].MemUtil != 20 || samples[0].TimeStamp != 1_234_000 {
		t.Fatalf("unexpected pmon parse: %+v", parsed)
	}
}

func TestParsePmonUtilizationSkipsNAAndHeaders(t *testing.T) {
	raw := "# header\n0 - - - - - - -\n"
	if parsed := parsePmonUtilization(raw, 1); len(parsed) != 0 {
		t.Fatalf("N/A rows must not become usage: %+v", parsed)
	}
}
