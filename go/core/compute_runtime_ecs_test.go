// Sprint 2 Theme C / §3.1.3 Fix 3 — Fargate vs ECS-EC2 disambiguation.
//
// Pre-fix: any ECS env var (ECS_CONTAINER_METADATA_URI_V4 or
// ECS_CONTAINER_METADATA_URI) classified the runtime as Fargate. But
// ECS-on-EC2 tasks also receive these env vars; only Fargate
// additionally sets AWS_EXECUTION_ENV=AWS_ECS_FARGATE. Pre-fix the
// Go SDK silently billed ECS-EC2 customers at the (more expensive)
// Fargate pricing tier.

package core

import (
	"testing"
)

func TestResolveRuntime_FargateRequiresAWSExecutionEnv(t *testing.T) {
	// Fargate sets BOTH env vars.
	t.Setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc")
	t.Setenv("AWS_EXECUTION_ENV", "AWS_ECS_FARGATE")

	got := ResolveRuntime()
	if got != RuntimeFargate {
		t.Errorf("with AWS_EXECUTION_ENV=AWS_ECS_FARGATE expected RuntimeFargate, got %q", got)
	}
}

func TestResolveRuntime_ECSEc2DoesNotMisclassifyAsFargate(t *testing.T) {
	// ECS-EC2 sets ECS_CONTAINER_METADATA_URI_V4 but NOT AWS_EXECUTION_ENV
	// (or sets it to something other than AWS_ECS_FARGATE — e.g. EC2).
	t.Setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc")
	t.Setenv("AWS_EXECUTION_ENV", "AWS_ECS_EC2")

	got := ResolveRuntime()
	if got == RuntimeFargate {
		t.Errorf("ECS-EC2 task (AWS_EXECUTION_ENV=AWS_ECS_EC2) was misclassified as Fargate")
	}
	// Should fall through to the IaaS detection path; on this test host
	// cloud_detect won't have AWS metadata so it'll return Unknown.
	// That's acceptable — the key assertion is "not Fargate".
}

func TestResolveRuntime_ECSEc2MissingAWSExecutionEnv(t *testing.T) {
	// Some EC2-launched ECS tasks have ECS_CONTAINER_METADATA_URI_V4
	// but no AWS_EXECUTION_ENV. Still not Fargate.
	t.Setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc")
	t.Setenv("AWS_EXECUTION_ENV", "")

	got := ResolveRuntime()
	if got == RuntimeFargate {
		t.Errorf("ECS task without AWS_EXECUTION_ENV was misclassified as Fargate")
	}
}
