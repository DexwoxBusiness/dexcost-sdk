// Tests for CloudEnv.InstanceType (Decision #3) — Phase 2 IMDS probes also
// extract the IaaS SKU when available so the compute pricing engine can
// resolve EC2 / GCE / Azure VM rates at finalize time.
//
// Mirrors python/tests/test_cloud_detect_instance_type.py.

package cloud

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestCloudEnvCarriesInstanceTypeField(t *testing.T) {
	env := CloudEnv{
		Provider:     "aws",
		Region:       "us-east-1",
		Source:       "imds",
		InstanceType: "c7g.xlarge",
	}
	if env.InstanceType != "c7g.xlarge" {
		t.Fatalf("InstanceType = %q, want %q", env.InstanceType, "c7g.xlarge")
	}
}

func TestCloudEnvInstanceTypeDefaultsToEmpty(t *testing.T) {
	env := CloudEnv{Source: "none"}
	if env.InstanceType != "" {
		t.Fatalf("zero-value InstanceType = %q, want empty", env.InstanceType)
	}
}

func TestAWSProbeReturnsInstanceType(t *testing.T) {
	var calls []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls = append(calls, r.URL.Path)
		switch {
		case strings.HasSuffix(r.URL.Path, "/api/token"):
			_, _ = w.Write([]byte("TOKEN"))
		case strings.HasSuffix(r.URL.Path, "/placement/region"):
			_, _ = w.Write([]byte("us-east-1"))
		case strings.HasSuffix(r.URL.Path, "/meta-data/instance-type"):
			_, _ = w.Write([]byte("c7g.xlarge"))
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	env := probeAWSAgainst(srv.URL)
	if env == nil {
		t.Fatal("expected env, got nil")
	}
	if env.Provider != "aws" || env.Region != "us-east-1" {
		t.Fatalf("got %+v", env)
	}
	if env.InstanceType != "c7g.xlarge" {
		t.Fatalf("InstanceType = %q, want %q", env.InstanceType, "c7g.xlarge")
	}
	found := false
	for _, p := range calls {
		if strings.Contains(p, "/meta-data/instance-type") {
			found = true
			break
		}
	}
	if !found {
		t.Fatalf("instance-type endpoint not hit: %v", calls)
	}
}

func TestAWSProbeInstanceTypeFailureDoesNotLoseRegion(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/api/token"):
			_, _ = w.Write([]byte("TOKEN"))
		case strings.HasSuffix(r.URL.Path, "/placement/region"):
			_, _ = w.Write([]byte("eu-west-2"))
		case strings.HasSuffix(r.URL.Path, "/meta-data/instance-type"):
			http.Error(w, "simulated 404", http.StatusNotFound)
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	env := probeAWSAgainst(srv.URL)
	if env == nil {
		t.Fatal("expected env, got nil")
	}
	if env.Region != "eu-west-2" {
		t.Fatalf("region = %q, want eu-west-2", env.Region)
	}
	if env.InstanceType != "" {
		t.Fatalf("InstanceType = %q, want empty", env.InstanceType)
	}
}

func TestGCPProbeReturnsMachineType(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/computeMetadata/v1/instance/region":
			_, _ = w.Write([]byte("projects/123/regions/us-central1"))
		case "/computeMetadata/v1/instance/machine-type":
			_, _ = w.Write([]byte("projects/123/machineTypes/n2-standard-2"))
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	env := probeGCPAgainstWithMachineType(srv.URL)
	if env == nil {
		t.Fatal("expected env, got nil")
	}
	if env.Region != "us-central1" {
		t.Fatalf("region = %q", env.Region)
	}
	if env.InstanceType != "n2-standard-2" {
		t.Fatalf("InstanceType = %q, want n2-standard-2", env.InstanceType)
	}
}

func TestGCPProbeMachineTypeFailureDoesNotLoseRegion(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/computeMetadata/v1/instance/region":
			_, _ = w.Write([]byte("projects/123/regions/us-central1"))
		case "/computeMetadata/v1/instance/machine-type":
			http.Error(w, "simulated 404", http.StatusNotFound)
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	env := probeGCPAgainstWithMachineType(srv.URL)
	if env == nil {
		t.Fatal("expected env")
	}
	if env.Region != "us-central1" {
		t.Fatalf("region = %q", env.Region)
	}
	if env.InstanceType != "" {
		t.Fatalf("InstanceType = %q, want empty", env.InstanceType)
	}
}

func TestAzureProbeReturnsVMSize(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"compute":{"location":"eastus","vmSize":"Standard_D2s_v3"}}`))
	}))
	defer srv.Close()

	env := probeAzureAgainst(srv.URL)
	if env == nil {
		t.Fatal("expected env")
	}
	if env.Region != "eastus" {
		t.Fatalf("region = %q", env.Region)
	}
	if env.InstanceType != "Standard_D2s_v3" {
		t.Fatalf("InstanceType = %q, want Standard_D2s_v3", env.InstanceType)
	}
}

func TestAzureProbeMissingVMSizeReturnsEmptyInstanceType(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"compute":{"location":"eastus"}}`))
	}))
	defer srv.Close()

	env := probeAzureAgainst(srv.URL)
	if env == nil {
		t.Fatal("expected env")
	}
	if env.Region != "eastus" {
		t.Fatalf("region = %q", env.Region)
	}
	if env.InstanceType != "" {
		t.Fatalf("InstanceType = %q, want empty", env.InstanceType)
	}
}
