use std::fmt;

/// Error type for the dexcost SDK.
#[derive(Debug)]
pub enum DexcostError {
    /// The SDK has not been initialized. Call `init()` first.
    NotInitialized,
    /// The SDK has already been initialized.
    AlreadyInitialized,
    /// The API key has an invalid format.
    InvalidApiKey(String),
    /// The task has already been ended.
    TaskAlreadyEnded,
    /// An HTTP transport error occurred.
    Transport(String),
    /// A serialization/deserialization error occurred.
    Serialization(String),
    /// A configuration error occurred.
    Config(String),
    /// A storage error occurred (SQLite).
    Storage(String),
}

impl fmt::Display for DexcostError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            DexcostError::NotInitialized => {
                write!(f, "dexcost: init() must be called before using the SDK")
            }
            DexcostError::AlreadyInitialized => {
                write!(f, "dexcost: SDK already initialized")
            }
            DexcostError::InvalidApiKey(msg) => {
                write!(f, "dexcost: invalid API key: {}", msg)
            }
            DexcostError::TaskAlreadyEnded => {
                write!(f, "dexcost: task already ended")
            }
            DexcostError::Transport(msg) => {
                write!(f, "dexcost: transport error: {}", msg)
            }
            DexcostError::Serialization(msg) => {
                write!(f, "dexcost: serialization error: {}", msg)
            }
            DexcostError::Config(msg) => {
                write!(f, "dexcost: config error: {}", msg)
            }
            DexcostError::Storage(msg) => {
                write!(f, "dexcost: storage error: {}", msg)
            }
        }
    }
}

impl std::error::Error for DexcostError {}

impl From<reqwest::Error> for DexcostError {
    fn from(err: reqwest::Error) -> Self {
        DexcostError::Transport(err.to_string())
    }
}

impl From<serde_json::Error> for DexcostError {
    fn from(err: serde_json::Error) -> Self {
        DexcostError::Serialization(err.to_string())
    }
}
