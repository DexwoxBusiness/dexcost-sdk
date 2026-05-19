import { describe, it, expect } from "vitest";
import { scanSource } from "../src/cli/scanner.js";

// ── Original tests (preserved) ───────────────────────────────────────

describe("TypeScript scanner", () => {
  it("detects openai.chat.completions.create", () => {
    const source =
      'const r = await openai.chat.completions.create({ model: "gpt-4o" });';
    const results = scanSource(source, "test.ts");
    expect(results.length).toBeGreaterThan(0);
    expect(results[0].provider).toBe("openai");
    expect(results[0].type).toBe("llm_call");
    expect(results[0].autoInstrumentable).toBe(true);
  });

  it("detects anthropic.messages.create", () => {
    const source =
      'const msg = await anthropic.messages.create({ model: "claude-sonnet-4-20250514" });';
    const results = scanSource(source, "test.ts");
    expect(results.length).toBeGreaterThan(0);
    expect(results[0].provider).toBe("anthropic");
    expect(results[0].type).toBe("llm_call");
    expect(results[0].autoInstrumentable).toBe(true);
  });

  it("detects vercel AI SDK generateText call", () => {
    const source = 'const result = await generateText({ model, prompt: "hi" });';
    const results = scanSource(source, "test.ts");
    expect(results.length).toBeGreaterThan(0);
    expect(results[0].provider).toBe("vercel-ai");
    expect(results[0].type).toBe("llm_call");
    expect(results[0].autoInstrumentable).toBe(true);
  });

  it("detects vercel AI SDK streamText call", () => {
    const source = 'const stream = await streamText({ model, prompt: "hi" });';
    const results = scanSource(source, "test.ts");
    expect(results.length).toBeGreaterThan(0);
    expect(results[0].provider).toBe("vercel-ai");
  });

  it("detects fetch to known AI domains", () => {
    const source =
      'const res = await fetch("https://api.openai.com/v1/chat/completions", { method: "POST" });';
    const results = scanSource(source, "test.ts");
    expect(results.length).toBeGreaterThan(0);
    expect(results[0].type).toBe("http_call");
    expect(results[0].provider).toBe("openai");
    expect(results[0].autoInstrumentable).toBe(false);
  });

  it("detects fetch to Anthropic API", () => {
    const source =
      'const res = await fetch("https://api.anthropic.com/v1/messages", { method: "POST" });';
    const results = scanSource(source, "test.ts");
    expect(results.length).toBeGreaterThan(0);
    expect(results[0].type).toBe("http_call");
    expect(results[0].provider).toBe("anthropic");
  });

  it("detects fetch to Cohere API", () => {
    const source =
      'const res = await fetch("https://api.cohere.ai/v1/chat", { method: "POST" });';
    const results = scanSource(source, "test.ts");
    expect(results.length).toBeGreaterThan(0);
    expect(results[0].provider).toBe("cohere");
  });

  it("detects AWS SDK imports", () => {
    const source =
      'import { BedrockRuntimeClient } from "@aws-sdk/client-bedrock-runtime";';
    const results = scanSource(source, "test.ts");
    expect(results.length).toBeGreaterThan(0);
    expect(results[0].provider).toBe("aws-bedrock");
    expect(results[0].type).toBe("sdk_import");
    expect(results[0].autoInstrumentable).toBe(true);
  });

  it("detects Google AI SDK imports", () => {
    const source =
      'import { GoogleGenerativeAI } from "@google/generative-ai";';
    const results = scanSource(source, "test.ts");
    expect(results.length).toBeGreaterThan(0);
    expect(results[0].provider).toBe("google-ai");
    expect(results[0].type).toBe("sdk_import");
  });

  it("returns empty for files with no cost points", () => {
    const source =
      "function add(a: number, b: number): number { return a + b; }";
    const results = scanSource(source, "utils.ts");
    expect(results.length).toBe(0);
  });

  it("reports correct line numbers", () => {
    const source = [
      "// line 1",
      "// line 2",
      'const r = await openai.chat.completions.create({ model: "gpt-4o" });',
    ].join("\n");
    const results = scanSource(source, "test.ts");
    expect(results.length).toBe(1);
    expect(results[0].line).toBe(3);
  });

  it("detects multiple cost points in one file", () => {
    const source = [
      'const r = await openai.chat.completions.create({ model: "gpt-4o" });',
      'const msg = await anthropic.messages.create({ model: "claude-sonnet-4-20250514" });',
    ].join("\n");
    const results = scanSource(source, "multi.ts");
    expect(results.length).toBe(2);
    expect(results[0].provider).toBe("openai");
    expect(results[1].provider).toBe("anthropic");
  });
});

// ── Additional LLM provider tests ────────────────────────────────────

describe("Additional LLM providers", () => {
  it("detects Groq SDK import", () => {
    const source = 'import Groq from "groq-sdk";';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "groq")).toBe(true);
  });

  it("detects Mistral chat.complete call", () => {
    const source = 'const response = await client.chat.complete({ model: "mistral-large" });';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "mistral")).toBe(true);
  });

  it("detects Mistral SDK import", () => {
    const source = 'import { Mistral } from "@mistralai/mistralai";';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "mistral")).toBe(true);
  });

  it("detects Together AI SDK import", () => {
    const source = 'import Together from "together-ai";';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "together")).toBe(true);
  });

  it("detects Replicate run call", () => {
    const source = 'const output = await replicate.run("meta/llama-2-7b", { input: {} });';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "replicate")).toBe(true);
  });

  it("detects Replicate SDK import", () => {
    const source = 'import Replicate from "replicate";';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "replicate")).toBe(true);
  });

  it("detects Cohere chat call", () => {
    const source = 'const response = cohere.chat({ message: "hello" });';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "cohere")).toBe(true);
  });

  it("detects Google generateContent", () => {
    const source = 'const result = await model.generateContent("hello");';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "google-ai")).toBe(true);
  });

  it("detects Ollama SDK import", () => {
    const source = 'import ollama from "ollama";';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "ollama")).toBe(true);
  });
});

// ── Framework detection tests ────────────────────────────────────────

describe("Framework detection", () => {
  it("detects LangChain OpenAI import", () => {
    const source = 'import { ChatOpenAI } from "@langchain/openai";';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "langchain")).toBe(true);
  });

  it("detects LangChain Anthropic import", () => {
    const source = 'import { ChatAnthropic } from "@langchain/anthropic";';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "langchain")).toBe(true);
  });

  it("detects .invoke() framework call", () => {
    const source = 'const result = await chain.invoke({ input: "hello" });';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.type === "framework_call")).toBe(true);
  });

  it("detects LangGraph import", () => {
    const source = 'import { StateGraph } from "@langchain/langgraph";';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "langgraph")).toBe(true);
  });

  it("detects Mastra import", () => {
    const source = 'import { Agent } from "@mastra/core";';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "mastra")).toBe(true);
  });
});

// ── Service detection tests ──────────────────────────────────────────

describe("Service detection", () => {
  it("detects Stripe import", () => {
    const source = 'import Stripe from "stripe";';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "stripe")).toBe(true);
  });

  it("detects Stripe API call", () => {
    const source = "await stripe.paymentIntents.create({ amount: 1000 });";
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "stripe" && r.type === "service_call")).toBe(true);
  });

  it("detects MongoDB import", () => {
    const source = 'import { MongoClient } from "mongodb";';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "mongodb")).toBe(true);
  });

  it("detects Supabase import", () => {
    const source = 'import { createClient } from "@supabase/supabase-js";';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "supabase")).toBe(true);
  });

  it("detects Firebase import", () => {
    const source = 'import * as admin from "firebase-admin";';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "firebase")).toBe(true);
  });

  it("detects Elasticsearch import", () => {
    const source = 'import { Client } from "@elastic/elasticsearch";';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "elasticsearch")).toBe(true);
  });

  it("detects Redis import", () => {
    const source = 'import Redis from "ioredis";';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "redis")).toBe(true);
  });

  it("detects Pinecone import", () => {
    const source = 'import { Pinecone } from "@pinecone-database/pinecone";';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "pinecone")).toBe(true);
  });

  it("detects Twilio import", () => {
    const source = 'import twilio from "twilio";';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "twilio")).toBe(true);
  });

  it("detects SendGrid import", () => {
    const source = 'import sgMail from "@sendgrid/mail";';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "sendgrid")).toBe(true);
  });

  it("detects Resend import", () => {
    const source = 'import { Resend } from "resend";';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "resend")).toBe(true);
  });

  it("detects Firecrawl import", () => {
    const source = 'import FirecrawlApp from "@mendable/firecrawl-js";';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "firecrawl")).toBe(true);
  });
});

// ── OpenAI multimodal tests ──────────────────────────────────────────

describe("OpenAI multimodal detection", () => {
  it("detects embeddings.create", () => {
    const source = 'await client.embeddings.create({ model: "text-embedding-3-small", input: "hi" });';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.type === "embedding")).toBe(true);
  });

  it("detects audio.transcriptions.create (Whisper)", () => {
    const source = 'await client.audio.transcriptions.create({ model: "whisper-1", file });';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.type === "speech")).toBe(true);
  });

  it("detects images.generate (DALL-E)", () => {
    const source = 'await client.images.generate({ model: "dall-e-3", prompt: "a cat" });';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.type === "image")).toBe(true);
  });
});

// ── HTTP API endpoint tests ──────────────────────────────────────────

describe("HTTP API endpoint detection", () => {
  it("detects fetch to Groq API", () => {
    const source = 'await fetch("https://api.groq.com/openai/v1/chat/completions", opts);';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "groq" && r.type === "http_call")).toBe(true);
  });

  it("detects fetch to Mistral API", () => {
    const source = 'await fetch("https://api.mistral.ai/v1/chat/completions", opts);';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "mistral" && r.type === "http_call")).toBe(true);
  });

  it("detects fetch to DeepSeek API", () => {
    const source = 'await fetch("https://api.deepseek.com/v1/chat/completions", opts);';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "deepseek" && r.type === "http_call")).toBe(true);
  });
});

// ── Edge case tests ──────────────────────────────────────────────────

describe("Edge cases", () => {
  it("handles client.chat.completions.create with variable name", () => {
    const source = 'const r = await client.chat.completions.create({ model: "gpt-4o" });';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "openai" && r.autoInstrumentable)).toBe(true);
  });

  it("handles require() style imports", () => {
    const source = 'const Groq = require("groq-sdk");';
    const results = scanSource(source, "test.ts");
    expect(results.some((r) => r.provider === "groq")).toBe(true);
  });
});
