/** Caller-asserted native-client billing eligibility, not invoice evidence. */
export type ProviderBillingProvider = "cohere" | "google";
export type ProviderBillingTier = "paid" | "free" | "unknown";
export interface ProviderBillingAssertion {
  provider: ProviderBillingProvider;
  tier: ProviderBillingTier;
  endpoint: string;
}
type Binding = ProviderBillingAssertion & { identity: object };
const bindings = new WeakMap<object, Binding>();

function owner(client: any, provider: string): object {
  if (provider === "google") return client?.apiClient ?? client?.models?.apiClient ?? client;
  // CohereClientV2 binds its methods to a generated V2Client instance.
  return provider === "cohere" ? (client?.clientV2 ?? client) : client;
}

function directEndpoint(endpoint: string, provider: string): string | undefined {
  try {
    const url = new URL(endpoint);
    const hosts: Record<string, string[]> = {
      cohere: ["api.cohere.com", "api.cohere.ai"], google: ["generativelanguage.googleapis.com"],
    };
    if (url.protocol !== "https:" || !hosts[provider]?.includes(url.hostname) ||
        url.port || url.username || url.password || url.search || url.hash || url.pathname !== "/") return;
    return url.origin;
  } catch { return; }
}

/**
 * Attest this native client's paid/free/unknown direct account tier.
 * Paid means caller-confirmed public PAYG eligibility, not cash actually owed.
 * No credentials are read. Rebind when changing the account or credentials.
 * The weak binding lasts until replaced, unbound, or the native owner is freed.
 * An old unbind callback cannot remove a newer assertion. In-flight streams may
 * keep their start-time snapshot. HTTP-only capture never inherits the binding.
 */
export function bindProviderBilling(client: object, assertion: ProviderBillingAssertion): () => void {
  const endpoint = directEndpoint(assertion.endpoint, assertion.provider);
  if (!endpoint || !["paid", "free", "unknown"].includes(assertion.tier)) {
    throw new Error("A supported direct endpoint and explicit billing tier are required");
  }
  const target = owner(client, assertion.provider);
  const weak = new WeakRef(target);
  const identity = {};
  bindings.set(target, { ...assertion, endpoint, identity });
  return () => {
    const current = weak.deref();
    if (current && bindings.get(current)?.identity === identity) bindings.delete(current);
  };
}

export function hasPaidProviderBilling(client: object, provider: string, endpoint: string): boolean {
  const binding = bindings.get(owner(client, provider));
  return binding?.provider === provider && binding.tier === "paid" &&
    binding.endpoint === directEndpoint(endpoint, provider);
}
