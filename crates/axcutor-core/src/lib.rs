//! Pure, snapshot-based compiler and deterministic execution kernel. No I/O.
pub mod ast;
pub mod compiler;
pub mod kernel;
mod source;
pub use compiler::*;
