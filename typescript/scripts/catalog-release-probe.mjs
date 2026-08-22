import { writeFileSync, readFileSync } from "node:fs";

import {
  CATALOG_KINDS,
  CatalogReleaseClient,
  CatalogReleaseStore,
} from "../dist/index.js";

function parseArgs(args) {
  const values = {};
  for (let index = 0; index < args.length;) {
    const flag = args[index];
    if (flag === "--corrupt-active-store") {
      values[flag.slice(2)] = true;
      index += 1;
      continue;
    }
    const value = args[index + 1];
    if (!flag?.startsWith("--") || value === undefined || value.startsWith("--")) {
      throw new Error(`invalid argument ${flag ?? ""}`);
    }
    values[flag.slice(2)] = value;
    index += 2;
  }
  for (const required of ["store", "key-id", "public-key", "expect-release"]) {
    if (!values[required]) throw new Error(`--${required} is required`);
  }
  const operations = [
    values.endpoint,
    values["import-bundle"],
    values["reject-corrupt-bundle"],
    values["reject-expired-bundle"],
    values["corrupt-active-store"],
  ]
    .filter(Boolean).length;
  if (operations !== 1) {
    throw new Error(
      "specify exactly one of --endpoint, --import-bundle, --reject-corrupt-bundle, "
        + "--reject-expired-bundle, or --corrupt-active-store",
    );
  }
  const channel = values.channel ?? "stable";
  if (channel !== "stable" && channel !== "canary") {
    throw new Error("--channel must be stable or canary");
  }
  return { ...values, channel };
}

function corruptActiveManifest(path, channel) {
  const value = JSON.parse(readFileSync(path, "utf8"));
  const channelState = value.channels?.[channel]
    ?? (channel === "stable" ? { active: value.active, previous: value.previous } : undefined);
  if (!channelState?.active || !channelState.previous) {
    throw new Error("corruption probe requires both active and previous releases");
  }
  channelState.active.manifest = Buffer.from("{").toString("base64");
  if (value.channels?.[channel]) value.channels[channel] = channelState;
  else {
    value.active = channelState.active;
    value.previous = channelState.previous;
  }
  writeFileSync(path, JSON.stringify(value), "utf8");
}

const options = parseArgs(process.argv.slice(2));
if (options["corrupt-active-store"]) {
  // This intentionally mutates only a disposable probe-store copy.
  corruptActiveManifest(options.store, options.channel);
}
const store = new CatalogReleaseStore(options.store, {
  trustedKeys: { [options["key-id"]]: options["public-key"] },
  requireSignature: true,
});
let snapshot;
let status;
if (options["corrupt-active-store"]) {
  snapshot = store.bestAvailable(options.channel);
  if (!snapshot || snapshot.source !== "previous") {
    throw new Error("corrupt active release did not fall back to the previous release");
  }
  status = "active_corrupt_previous_preserved";
} else if (options["reject-corrupt-bundle"]) {
  const value = JSON.parse(readFileSync(options["reject-corrupt-bundle"], "utf8"));
  const encoded = value.artifacts_base64url.gpu_prices;
  value.artifacts_base64url.gpu_prices = `${encoded[0] === "A" ? "B" : "A"}${encoded.slice(1)}`;
  try {
    store.importBundle(Buffer.from(JSON.stringify(value)));
  } catch {
    snapshot = store.bestAvailable(options.channel);
    if (!snapshot) throw new Error("corrupt import discarded the last-known-good release");
    status = "corrupt_rejected_lkg_preserved";
  }
  if (!snapshot) throw new Error("corrupt catalog bundle was accepted");
} else if (options["reject-expired-bundle"]) {
  try {
    store.importBundle(readFileSync(options["reject-expired-bundle"]));
  } catch (error) {
    if (!(error instanceof Error) || !/expired/i.test(error.message)) {
      throw new Error("expired catalog bundle failed for an unexpected reason", { cause: error });
    }
    snapshot = store.bestAvailable(options.channel);
    if (!snapshot) throw new Error("expired import discarded the last-known-good release");
    status = "expired_rejected_lkg_preserved";
  }
  if (!snapshot) throw new Error("expired catalog bundle was accepted");
} else if (options["import-bundle"]) {
  snapshot = store.importBundle(readFileSync(options["import-bundle"]));
  status = "imported";
} else {
  const result = await new CatalogReleaseClient(
    options.endpoint,
    store,
    options.channel,
  ).refresh();
  if (!result.snapshot) throw new Error(`catalog refresh returned no snapshot: ${result.error}`);
  snapshot = result.snapshot;
  status = result.status;
}
if (snapshot.manifest.release_id !== options["expect-release"]) {
  throw new Error(
    `expected ${options["expect-release"]}, received ${snapshot.manifest.release_id}`,
  );
}
if (Object.keys(snapshot.artifacts).sort().join(",") !== [...CATALOG_KINDS].sort().join(",")) {
  throw new Error("catalog probe did not activate exactly seven families");
}
if (options["export-bundle"]) {
  writeFileSync(options["export-bundle"], store.exportBundle(options.channel, "active"));
}
process.stdout.write(`${JSON.stringify({
  implementation: "typescript",
  status,
  release_id: snapshot.manifest.release_id,
  release_sequence: snapshot.manifest.release_sequence,
  channel: snapshot.manifest.channel,
  source: snapshot.source,
  stale: snapshot.stale,
  signature_key_ids: snapshot.manifest.signatures.map((signature) => signature.key_id),
  artifact_sha256: Object.fromEntries(CATALOG_KINDS.map((kind) => [
    kind,
    snapshot.manifest.artifacts[kind].sha256,
  ])),
})}\n`);
