// Active compute-runtime resolver.
//
// Cascade priority (capture spec §5.5):
//
//  1. Serverless env vars — Lambda, Fargate, Cloud Run, Cloud Functions
//     Gen2, Azure Functions, Vercel
//  2. KUBERNETES_SERVICE_HOST → k8s_pod (wins over the underlying VM so
//     a pod-on-EC2 is billed once as k8s_pod, not twice as k8s_pod + ec2)
//  3. cloud_detect IaaS fallback — EC2 / GCE / Azure VM
//  4. UNKNOWN
//
// The discriminator value emitted on compute_cost events
// (details.billing_model) is derived from this enum in
// compute_accountant.billingModelFor.
//
// RuntimeKind string values MUST match python/src/dexcost/compute_runtime.py
// — events are cross-SDK portable through the Control Layer.

package core

import (
	"os"

	"github.com/DexwoxBusiness/dexcost-sdk/go/cloud"
)

// RuntimeKind is the active compute-runtime discriminator. String values
// match Python's RuntimeKind enum for cross-SDK event portability.
type RuntimeKind string

const (
	RuntimeLambda         RuntimeKind = "lambda"
	RuntimeFargate        RuntimeKind = "fargate"
	RuntimeEC2            RuntimeKind = "ec2"
	RuntimeCloudRun       RuntimeKind = "cloud_run"
	RuntimeCloudFunctions RuntimeKind = "cloud_functions"
	RuntimeGCE            RuntimeKind = "gce"
	RuntimeAzureFunctions RuntimeKind = "azure_functions"
	RuntimeAzureVM        RuntimeKind = "azure_vm"
	RuntimeVercel         RuntimeKind = "vercel_fluid"
	RuntimeK8sPod         RuntimeKind = "k8s_pod"
	RuntimeUnknown        RuntimeKind = "unknown"
)

// ResolveRuntime returns the active compute runtime for the current process.
func ResolveRuntime() RuntimeKind {
	// 1. Serverless env vars take highest priority — a Lambda is a Lambda
	//    even though it also runs on AWS infrastructure.
	if os.Getenv("AWS_LAMBDA_FUNCTION_NAME") != "" {
		return RuntimeLambda
	}
	// Sprint 2 Theme C / §3.1.3 Fix 3 — ECS-on-EC2 tasks ALSO receive
	// ECS_CONTAINER_METADATA_URI_V4 per AWS docs. Only Fargate sets
	// AWS_EXECUTION_ENV=AWS_ECS_FARGATE in addition. Without this
	// disambiguator the SDK silently billed ECS-EC2 customers at
	// the (more expensive) Fargate pricing tier; falling through to
	// the IaaS detection path (which returns RuntimeEC2) is correct
	// for ECS-on-EC2.
	if os.Getenv("ECS_CONTAINER_METADATA_URI_V4") != "" ||
		os.Getenv("ECS_CONTAINER_METADATA_URI") != "" {
		if os.Getenv("AWS_EXECUTION_ENV") == "AWS_ECS_FARGATE" {
			return RuntimeFargate
		}
		// Else: ECS-on-EC2 — fall through to IaaS detection below.
	}
	if os.Getenv("K_SERVICE") != "" {
		// Cloud Functions Gen2 sets BOTH K_SERVICE and FUNCTION_TARGET;
		// plain Cloud Run sets only K_SERVICE. Distinguish so downstream
		// dashboards can break out function-vs-service even though the
		// billing math is identical (Cloud Functions Gen2 IS Cloud Run
		// under the hood).
		if os.Getenv("FUNCTION_TARGET") != "" {
			return RuntimeCloudFunctions
		}
		return RuntimeCloudRun
	}
	if os.Getenv("FUNCTIONS_WORKER_RUNTIME") != "" {
		return RuntimeAzureFunctions
	}
	if os.Getenv("VERCEL") != "" {
		return RuntimeVercel
	}

	// 2. Kubernetes wins over the underlying VM. A pod on EC2 reports as
	//    k8s_pod (billed at pod-limits × duration); the EC2 instance share
	//    would double-count the same compute hour.
	if os.Getenv("KUBERNETES_SERVICE_HOST") != "" {
		return RuntimeK8sPod
	}

	// 3. Fall through to cloud_detect IaaS classification.
	env := cloud.GetCloudEnv()
	switch env.Provider {
	case "aws":
		return RuntimeEC2
	case "gcp":
		return RuntimeGCE
	case "azure":
		return RuntimeAzureVM
	}
	return RuntimeUnknown
}
