package main

import (
	"fmt"
	"math/rand"
	"strings"
)

// Runbook is a single synthetic incident-runbook document.
type Runbook struct {
	Title   string
	Content string
	Tags    []string
}

// generateRunbooks creates ≥100 synthetic Markdown/YAML runbooks covering
// common DevOps incident-response topics.
func generateRunbooks() []Runbook {
	categories := []struct {
		name     string
		services []string
		issues   []string
		actions  []string
	}{
		{
			name:     "kubernetes",
			services: []string{"kube-apiserver", "etcd", "kubelet", "coredns", "ingress-nginx", "calico", "prometheus-operator"},
			issues:   []string{"pod crash loop", "node not ready", "OOMKilled", "ImagePullBackOff", "high memory usage", "disk pressure", "network partition", "DNS resolution failure"},
			actions:  []string{"drain the node", "restart kubelet", "scale deployment", "check events", "describe pod", "roll back deployment", "increase memory limit", "clear image cache"},
		},
		{
			name:     "aws",
			services: []string{"EC2", "RDS", "S3", "Lambda", "ElastiCache", "ALB", "CloudFront", "SQS"},
			issues:   []string{"instance unreachable", "high CPU utilization", "connection timeout", "throttling exception", "5xx errors", "slow query", "replica lag", "bucket policy error"},
			actions:  []string{"restart instance", "scale ASG", "flush cache", "check security group", "update IAM policy", "create snapshot", "switch to standby", "increase provisioned capacity"},
		},
		{
			name:     "docker",
			services: []string{"dockerd", "containerd", "registry", "compose", "swarm", "buildkit"},
			issues:   []string{"container exit 1", "port conflict", "volume mount error", "registry auth failure", "layer cache miss", "network bridge down", "image layer corruption"},
			actions:  []string{"prune volumes", "restart daemon", "rebuild image", "login to registry", "inspect network", "remove stale containers", "update compose file"},
		},
		{
			name:     "ci-cd",
			services: []string{"GitHub Actions", "GitLab CI", "Jenkins", "ArgoCD", "CircleCI", "Tekton"},
			issues:   []string{"build timeout", "test flake", "artifact upload failure", "deploy rejected", "merge conflict", "runner offline", "secret rotation needed"},
			actions:  []string{"retry job", "increase runner size", "rotate secret", "approve gate", "rebase branch", "clear workspace", "update helm values"},
		},
		{
			name:     "monitoring",
			services: []string{"Prometheus", "Grafana", "PagerDuty", "Datadog", "New Relic", "ELK", "Jaeger"},
			issues:   []string{"alertmanager silenced", "metric cardinality explosion", "dashboard 404", "trace sampling drop", "log ingestion lag", "high error rate"},
			actions:  []string{"silence alert", "drop high-cardinality label", "refresh dashboard", "increase retention", "restart collector", "tune scrape interval"},
		},
		{
			name:     "database",
			services: []string{"PostgreSQL", "MySQL", "MongoDB", "Redis", "Elasticsearch", "Cassandra"},
			issues:   []string{"connection pool exhausted", "replication lag", "index bloat", "deadlock detected", "slow query", "checkpoint spike", "shard imbalance"},
			actions:  []string{"restart replica", "reindex table", "kill long query", "add connection pool", "reshard cluster", "vacuum analyze", "promote standby"},
		},
		{
			name:     "networking",
			services: []string{"nginx", "haproxy", "traefik", "envoy", "iptables", "BGP", "VPC"},
			issues:   []string{"TLS handshake failure", "upstream timeout", "route not found", "certificate expiry", "asymmetric routing", "SYN flood"},
			actions:  []string{"reload config", "renew certificate", "add upstream", "update ACL", "blackhole source", "failover VIP"},
		},
		{
			name:     "security",
			services: []string{"Vault", "OPA", "Falco", "Trivy", "SonarQube", "WAF"},
			issues:   []string{"unseal required", "policy deny", "CVE detected", "secret leak", "DDoS spike", "mfa bypass attempt"},
			actions:  []string{"unseal vault", "update policy", "patch image", "rotate credential", "enable rate limit", "block IP"},
		},
	}

	severities := []string{"P0-critical", "P1-high", "P2-medium", "P3-low"}
	environments := []string{"production", "staging", "development", "disaster-recovery"}

	var runbooks []Runbook
	rng := rand.New(rand.NewSource(42)) // deterministic seed for reproducibility

	id := 1
	for _, cat := range categories {
		for _, svc := range cat.services {
			for _, issue := range cat.issues {
				if rng.Float32() > 0.6 {
					continue // sparsify so we get ~100 docs
				}
				sev := severities[rng.Intn(len(severities))]
				env := environments[rng.Intn(len(environments))]
				action := cat.actions[rng.Intn(len(cat.actions))]

				content := fmt.Sprintf(`# Runbook: %s — %s in %s

## Metadata
- **Service:** %s
- **Category:** %s
- **Severity:** %s
- **Environment:** %s
- **Runbook ID:** RB-%04d

## Symptoms
- %s detected in %s environment.
- Alert fired from monitoring stack.
- Error rate elevated above SLO threshold.

## Diagnostic Steps
1. Verify service health via /health endpoint.
2. Check recent deployment or configuration change.
3. Review logs for the last 15 minutes.
4. Validate downstream dependency status.

## Resolution
- **Primary action:** %s
- **Rollback plan:** revert to previous stable version.
- **Escalation:** page on-call SRE if unresolved in 10 minutes.

## Post-Incident
- Update status page.
- Schedule blameless post-mortem within 48 hours.
- Capture timeline and root-cause analysis.
`, issue, svc, env, svc, cat.name, sev, env, id, issue, env, action)

				runbooks = append(runbooks, Runbook{
					Title:   fmt.Sprintf("[%s] %s — %s (%s)", sev, svc, issue, env),
					Content: content,
					Tags:    []string{cat.name, svc, sev, env},
				})
				id++
			}
		}
	}

	// Ensure we hit at least 100 docs by adding generic runbooks.
	for len(runbooks) < 100 {
		cat := categories[rng.Intn(len(categories))]
		svc := cat.services[rng.Intn(len(cat.services))]
		issue := cat.issues[rng.Intn(len(cat.issues))]
		sev := severities[rng.Intn(len(severities))]
		env := environments[rng.Intn(len(environments))]
		content := fmt.Sprintf("# Generic Runbook RB-%04d\n\nService: %s\nIssue: %s\nSeverity: %s\nEnv: %s\n\n## Steps\n1. Investigate.\n2. Remediate.\n3. Verify.\n", len(runbooks)+1, svc, issue, sev, env)
		runbooks = append(runbooks, Runbook{
			Title:   fmt.Sprintf("[%s] %s — %s (%s)", sev, svc, issue, env),
			Content: content,
			Tags:    []string{cat.name, svc, sev, env},
		})
	}

	return runbooks
}

// chunk splits a runbook into smaller passages for embedding.
func chunk(r Runbook, maxChars int) []string {
	var chunks []string
	parts := strings.Split(r.Content, "\n\n")
	var current strings.Builder
	for _, p := range parts {
		if current.Len()+len(p) > maxChars && current.Len() > 0 {
			chunks = append(chunks, current.String())
			current.Reset()
		}
		current.WriteString(p)
		current.WriteString("\n\n")
	}
	if current.Len() > 0 {
		chunks = append(chunks, current.String())
	}
	// Always include the title as its own chunk for searchability.
	chunks = append([]string{r.Title}, chunks...)
	return chunks
}
