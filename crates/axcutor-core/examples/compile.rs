//! Minimal host adapter: a CompileRequest on stdin, a CompileOutput on stdout.
use std::io::{Read, Write};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut request = String::new();
    std::io::stdin().read_to_string(&mut request)?;
    let result = axcutor_core::compile_workflow_json(&request).map_err(std::io::Error::other)?;
    std::io::stdout().write_all(result.as_bytes())?;
    Ok(())
}
