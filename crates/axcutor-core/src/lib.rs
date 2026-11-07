//! Pure, snapshot-based Axcutor compiler. No source loading or runtime execution.
pub mod ast;
pub mod compiler;
mod source;
pub use compiler::*;
