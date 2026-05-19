"""Tests for dexcost scan code scanner (US-019)."""
from __future__ import annotations

from pathlib import Path

from dexcost.scanner import CostPoint, ScanResult, generate_stubs, scan_directory


# ── Original tests (preserved) ───────────────────────────────────────


class TestScanDirectory:
    def test_detects_openai_call(self, tmp_path: Path) -> None:
        code = '''
import openai
client = openai.OpenAI()
response = client.chat.completions.create(model="gpt-4o", messages=[])
'''
        (tmp_path / "agent.py").write_text(code)
        result = scan_directory(tmp_path)
        assert result.files_scanned == 1
        assert result.auto_count >= 1
        llm = [cp for cp in result.cost_points if cp.category == "llm"]
        assert len(llm) >= 1
        assert llm[0].auto_instrumented is True

    def test_detects_anthropic_call(self, tmp_path: Path) -> None:
        code = '''
import anthropic
client = anthropic.Anthropic()
msg = client.messages.create(model="claude-3", max_tokens=100, messages=[])
'''
        (tmp_path / "agent.py").write_text(code)
        result = scan_directory(tmp_path)
        llm = [cp for cp in result.cost_points if cp.category == "llm"]
        assert len(llm) >= 1

    def test_detects_litellm_call(self, tmp_path: Path) -> None:
        code = '''
import litellm
response = litellm.completion(model="gpt-4o", messages=[])
'''
        (tmp_path / "agent.py").write_text(code)
        result = scan_directory(tmp_path)
        llm = [cp for cp in result.cost_points if cp.category == "llm"]
        assert len(llm) >= 1

    def test_detects_requests_call(self, tmp_path: Path) -> None:
        code = '''
import requests
r = requests.post("https://api.example.com/search")
'''
        (tmp_path / "agent.py").write_text(code)
        result = scan_directory(tmp_path)
        http = [cp for cp in result.cost_points if cp.category == "http"]
        assert len(http) >= 1
        # requests is auto-instrumented via Session.send patch + domain matching
        assert http[0].auto_instrumented is True

    def test_detects_boto3_call(self, tmp_path: Path) -> None:
        code = '''
import boto3
s3 = boto3.client("s3")
s3.put_object(Bucket="b", Key="k", Body=b"data")
'''
        (tmp_path / "agent.py").write_text(code)
        result = scan_directory(tmp_path)
        aws = [cp for cp in result.cost_points if cp.category == "aws"]
        assert len(aws) >= 1

    def test_detects_pinecone(self, tmp_path: Path) -> None:
        code = '''
import pinecone
index = pinecone.Index("my-index")
results = index.query(vector=[0.1, 0.2], top_k=10)
'''
        (tmp_path / "agent.py").write_text(code)
        result = scan_directory(tmp_path)
        vdb = [cp for cp in result.cost_points if cp.category == "vector_db"]
        assert len(vdb) >= 1

    def test_detects_sendgrid(self, tmp_path: Path) -> None:
        code = '''
import sendgrid
sg = sendgrid.SendGridAPIClient()
sg.send(message)
'''
        (tmp_path / "agent.py").write_text(code)
        result = scan_directory(tmp_path)
        msg = [cp for cp in result.cost_points if cp.category == "messaging"]
        assert len(msg) >= 1

    def test_empty_directory(self, tmp_path: Path) -> None:
        result = scan_directory(tmp_path)
        assert result.files_scanned == 0
        assert result.auto_count == 0
        assert result.manual_count == 0

    def test_nonexistent_path(self, tmp_path: Path) -> None:
        result = scan_directory(tmp_path / "nonexistent")
        assert result.files_scanned == 0

    def test_syntax_error_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "bad.py").write_text("def broken(")
        (tmp_path / "good.py").write_text("x = 1\n")
        result = scan_directory(tmp_path)
        assert result.files_scanned == 2  # Both attempted

    def test_summary_counts(self, tmp_path: Path) -> None:
        code = '''
import openai
import pymongo
client = openai.OpenAI()
client.chat.completions.create(model="gpt-4o", messages=[])
db = pymongo.MongoClient()
db.mydb.mycol.find({})
'''
        (tmp_path / "mixed.py").write_text(code)
        result = scan_directory(tmp_path)
        assert result.auto_count >= 1  # openai is auto-instrumented
        assert result.manual_count >= 1  # pymongo is NOT auto-instrumented


class TestGenerateStubs:
    def test_generates_for_manual_only(self) -> None:
        result = ScanResult(
            cost_points=[
                CostPoint("f.py", 10, "llm", True, "OpenAI", "openai"),
                CostPoint("f.py", 20, "http", False, "HTTP POST", "requests"),
            ]
        )
        stubs = generate_stubs(result)
        assert "requests" in stubs
        assert "dexcost.init" in stubs
        assert "dexcost.set_context" in stubs
        assert 't.record_cost("requests"' in stubs
        assert "Decimal" in stubs
        assert "dexcost.task(" in stubs
        # Auto-instrumented providers listed as confirmation
        assert "openai" in stubs
        assert "Auto-instrumented" in stubs

    def test_empty_result(self) -> None:
        result = ScanResult()
        assert generate_stubs(result) == ""

    def test_auto_only(self) -> None:
        result = ScanResult(
            cost_points=[
                CostPoint("f.py", 10, "llm", True, "OpenAI", "openai"),
            ]
        )
        stubs = generate_stubs(result)
        assert "dexcost.init" in stubs
        assert "\u2713 openai" in stubs
        assert "t.record_cost" not in stubs

    def test_manual_grouped_by_file(self) -> None:
        result = ScanResult(
            cost_points=[
                CostPoint("a.py", 10, "http", False, "HTTP POST", "requests"),
                CostPoint("b.py", 20, "payment", False, "Stripe", "stripe"),
            ]
        )
        stubs = generate_stubs(result)
        assert "a.py:10" in stubs
        assert "b.py:20" in stubs
        assert 't.record_cost("requests"' in stubs
        assert 't.record_cost("stripe"' in stubs


# ── Assignment tracking tests ─────────────────────────────────────────


class TestAssignmentTracking:
    """Variable assignment tracking: ``client = openai.OpenAI()`` means
    ``client.chat.completions.create(...)`` is detected."""

    def test_openai_client_variable(self, tmp_path: Path) -> None:
        code = '''
import openai
client = openai.OpenAI()
client.chat.completions.create(model="gpt-4o", messages=[])
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        llm = [cp for cp in result.cost_points if cp.category == "llm"]
        assert len(llm) >= 1
        assert llm[0].auto_instrumented is True

    def test_anthropic_client_variable(self, tmp_path: Path) -> None:
        code = '''
import anthropic
client = anthropic.Anthropic()
client.messages.create(model="claude-3", max_tokens=100, messages=[])
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        llm = [cp for cp in result.cost_points if cp.category == "llm"]
        assert len(llm) >= 1

    def test_requests_session(self, tmp_path: Path) -> None:
        """``session = requests.Session(); session.get(url)`` detected."""
        code = '''
import requests
session = requests.Session()
session.get("https://api.example.com/data")
session.post("https://api.example.com/submit")
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        http = [cp for cp in result.cost_points if cp.category == "http"]
        assert len(http) >= 2

    def test_httpx_client(self, tmp_path: Path) -> None:
        """``client = httpx.Client(); client.get(url)`` detected."""
        code = '''
import httpx
client = httpx.Client()
client.get("https://api.example.com")
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        http = [cp for cp in result.cost_points if cp.category == "http"]
        assert len(http) >= 1

    def test_httpx_async_client(self, tmp_path: Path) -> None:
        code = '''
import httpx
client = httpx.AsyncClient()
await client.post("https://api.example.com")
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        http = [cp for cp in result.cost_points if cp.category == "http"]
        assert len(http) >= 1


# ── Additional LLM provider tests ────────────────────────────────────


class TestAdditionalLLMProviders:
    def test_groq(self, tmp_path: Path) -> None:
        code = '''
import groq
client = groq.Groq()
client.chat.completions.create(model="llama3-8b", messages=[])
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        llm = [cp for cp in result.cost_points if cp.category == "llm"]
        assert len(llm) >= 1
        assert "Groq" in llm[0].description

    def test_mistral(self, tmp_path: Path) -> None:
        code = '''
from mistralai import Mistral
client = Mistral(api_key="x")
response = client.chat.complete(model="mistral-large", messages=[])
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        llm = [cp for cp in result.cost_points if cp.category == "llm"]
        assert len(llm) >= 1
        assert "Mistral" in llm[0].description

    def test_replicate(self, tmp_path: Path) -> None:
        code = '''
import replicate
output = replicate.run("meta/llama-2-7b", input={"prompt": "hello"})
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        llm = [cp for cp in result.cost_points if cp.category == "llm"]
        assert len(llm) >= 1

    def test_together(self, tmp_path: Path) -> None:
        code = '''
import together
client = together.Together()
response = client.chat.completions.create(model="meta-llama/Llama-3", messages=[])
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        llm = [cp for cp in result.cost_points if cp.category == "llm"]
        assert len(llm) >= 1

    def test_cohere(self, tmp_path: Path) -> None:
        code = '''
import cohere
co = cohere.Client("key")
co.chat(message="hello")
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        llm = [cp for cp in result.cost_points if cp.category == "llm"]
        assert len(llm) >= 1

    def test_google_gemini(self, tmp_path: Path) -> None:
        code = '''
import google.generativeai as genai
model = genai.GenerativeModel("gemini-pro")
model.generate_content("hello")
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        llm = [cp for cp in result.cost_points if cp.category == "llm"]
        assert len(llm) >= 1

    def test_ollama(self, tmp_path: Path) -> None:
        code = '''
import ollama
response = ollama.chat(model="llama3", messages=[{"role": "user", "content": "hi"}])
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        llm = [cp for cp in result.cost_points if cp.category == "llm"]
        assert len(llm) >= 1


# ── Framework detection tests ─────────────────────────────────────────


class TestFrameworkDetection:
    def test_langchain_invoke(self, tmp_path: Path) -> None:
        code = '''
from langchain_openai import ChatOpenAI
llm = ChatOpenAI(model="gpt-4o")
result = llm.invoke("hello")
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        fw = [cp for cp in result.cost_points if cp.category == "framework"]
        assert len(fw) >= 1
        assert "LangChain" in fw[0].description

    def test_langchain_chain_invoke(self, tmp_path: Path) -> None:
        code = '''
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
chain = ChatPromptTemplate.from_template("{input}") | ChatOpenAI()
chain.invoke({"input": "hello"})
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        fw = [cp for cp in result.cost_points if cp.category == "framework"]
        assert len(fw) >= 1

    def test_crewai_kickoff(self, tmp_path: Path) -> None:
        code = '''
from crewai import Crew, Agent, Task
crew = Crew(agents=[Agent(role="researcher")], tasks=[Task(description="research")])
crew.kickoff()
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        fw = [cp for cp in result.cost_points if cp.category == "framework"]
        assert len(fw) >= 1
        assert "CrewAI" in fw[0].description

    def test_llamaindex_query(self, tmp_path: Path) -> None:
        code = '''
from llama_index.core import VectorStoreIndex
index = VectorStoreIndex.from_documents(documents)
response = index.as_query_engine().query("What is X?")
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        fw = [cp for cp in result.cost_points if cp.category == "framework"]
        assert len(fw) >= 1
        assert "LlamaIndex" in fw[0].description


# ── Service detection tests ───────────────────────────────────────────


class TestServiceDetection:
    def test_stripe(self, tmp_path: Path) -> None:
        code = '''
import stripe
stripe.PaymentIntent.create(amount=1000, currency="usd")
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        svc = [cp for cp in result.cost_points if cp.category == "payment"]
        assert len(svc) >= 1

    def test_mongodb(self, tmp_path: Path) -> None:
        code = '''
import pymongo
client = pymongo.MongoClient("mongodb://localhost")
db = client.mydb
db.collection.find({"name": "test"})
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        svc = [cp for cp in result.cost_points if cp.category == "database"]
        assert len(svc) >= 1

    def test_elasticsearch(self, tmp_path: Path) -> None:
        code = '''
from elasticsearch import Elasticsearch
es = Elasticsearch()
es.search(index="my-index", query={"match_all": {}})
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        svc = [cp for cp in result.cost_points if cp.category == "search"]
        assert len(svc) >= 1

    def test_google_maps(self, tmp_path: Path) -> None:
        code = '''
import googlemaps
gmaps = googlemaps.Client(key="x")
gmaps.geocode("1600 Amphitheatre Parkway")
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        svc = [cp for cp in result.cost_points if cp.category == "geo"]
        assert len(svc) >= 1

    def test_openai_embeddings(self, tmp_path: Path) -> None:
        code = '''
import openai
client = openai.OpenAI()
client.embeddings.create(model="text-embedding-3-small", input="hello")
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        svc = [cp for cp in result.cost_points if cp.category == "embedding"]
        assert len(svc) >= 1

    def test_openai_whisper(self, tmp_path: Path) -> None:
        code = '''
import openai
client = openai.OpenAI()
client.audio.transcriptions.create(model="whisper-1", file=audio_file)
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        svc = [cp for cp in result.cost_points if cp.category == "speech"]
        assert len(svc) >= 1

    def test_openai_dalle(self, tmp_path: Path) -> None:
        code = '''
import openai
client = openai.OpenAI()
client.images.generate(model="dall-e-3", prompt="a cat")
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        svc = [cp for cp in result.cost_points if cp.category == "image"]
        assert len(svc) >= 1

    def test_firecrawl(self, tmp_path: Path) -> None:
        code = '''
from firecrawl import FirecrawlApp
app = FirecrawlApp(api_key="x")
app.scrape_url("https://example.com")
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        svc = [cp for cp in result.cost_points if cp.category == "scraping"]
        assert len(svc) >= 1


# ── Directory exclusion tests ─────────────────────────────────────────


class TestDirectoryExclusions:
    def test_skips_venv(self, tmp_path: Path) -> None:
        venv = tmp_path / ".venv" / "lib"
        venv.mkdir(parents=True)
        (venv / "site.py").write_text("import openai\nopenai.chat.completions.create()")
        (tmp_path / "app.py").write_text("x = 1\n")
        result = scan_directory(tmp_path)
        assert result.files_scanned == 1  # Only app.py

    def test_skips_pycache(self, tmp_path: Path) -> None:
        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "mod.py").write_text("import openai")
        (tmp_path / "app.py").write_text("x = 1\n")
        result = scan_directory(tmp_path)
        assert result.files_scanned == 1

    def test_skips_node_modules(self, tmp_path: Path) -> None:
        nm = tmp_path / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "helper.py").write_text("import openai")
        (tmp_path / "app.py").write_text("x = 1\n")
        result = scan_directory(tmp_path)
        assert result.files_scanned == 1


# ── Edge case tests ───────────────────────────────────────────────────


class TestEdgeCases:
    def test_no_duplicate_on_same_line(self, tmp_path: Path) -> None:
        """A single call should not produce multiple cost points."""
        code = '''
import openai
client = openai.OpenAI()
client.chat.completions.create(model="gpt-4o", messages=[])
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        # Only 1 detection for the create call (line 4), not multiple
        assert len(result.cost_points) == 1

    def test_import_alias(self, tmp_path: Path) -> None:
        """``import openai as oai`` still detected via assignment tracking."""
        code = '''
import openai as oai
client = oai.OpenAI()
client.chat.completions.create(model="gpt-4o", messages=[])
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        llm = [cp for cp in result.cost_points if cp.category == "llm"]
        assert len(llm) >= 1

    def test_from_import(self, tmp_path: Path) -> None:
        """``from openai import OpenAI`` still detected."""
        code = '''
from openai import OpenAI
client = OpenAI()
client.chat.completions.create(model="gpt-4o", messages=[])
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        llm = [cp for cp in result.cost_points if cp.category == "llm"]
        assert len(llm) >= 1

    def test_no_false_positive_without_import(self, tmp_path: Path) -> None:
        """Calls matching patterns but without the import are NOT flagged."""
        code = '''
# No openai import
my_client.chat.completions.create(model="local", messages=[])
'''
        (tmp_path / "a.py").write_text(code)
        result = scan_directory(tmp_path)
        assert len(result.cost_points) == 0
