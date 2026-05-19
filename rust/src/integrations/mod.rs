//! Integrations with external observability platforms (Langfuse, LangSmith, OTel)
//! and LLM orchestration frameworks (LangChain).
//!
//! Use `traces::link_trace(provider, trace_id)` from inside an active task
//! context to associate a third-party trace with the current dexcost task.
//!
//! Use [`langchain::DexcostCallbackHandler`] to record LLM calls made through
//! a LangChain-style pipeline as dexcost cost events.

pub mod langchain;
pub mod traces;

pub use langchain::DexcostCallbackHandler;
