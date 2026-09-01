from __future__ import annotations

from typing import Sequence
from codepulse.models import Symbol


class CodeChunk:
    """Represents a chunk of code prepared for embedding."""

    def __init__(
        self,
        symbol_name: str,
        file_path: str,
        chunk_type: str,  # 'signature_doc', 'body_chunk', 'full'
        content: str,
        start_line: int,
        end_line: int,
        chunk_index: int = 0,
    ) -> None:
        self.symbol_name = symbol_name
        self.file_path = file_path
        self.chunk_type = chunk_type
        self.content = content
        self.start_line = start_line
        self.end_line = end_line
        self.chunk_index = chunk_index

    def __repr__(self) -> str:
        return f"<CodeChunk {self.symbol_name} ({self.chunk_type}) lines={self.start_line}-{self.end_line}>"


class SemanticChunker:
    """
    AST-aware semantic chunker for code symbols.
    
    Splits symbols based on logical boundaries (signature + docstring vs body chunks)
    rather than naive arbitrary character/token splits.
    """

    def __init__(
        self,
        max_tokens: int | None = None,
        chunk_overlap_lines: int | None = None,
    ) -> None:
        from codepulse.config import get_settings
        settings = get_settings()
        self.max_tokens = max_tokens if max_tokens is not None else settings.chunk_max_tokens
        self.chunk_overlap_lines = (
            chunk_overlap_lines if chunk_overlap_lines is not None else settings.chunk_overlap_lines
        )

    def chunk_symbol(self, symbol: Symbol) -> list[CodeChunk]:
        """
        Creates semantic chunks for an extracted code Symbol.
        
        Strategy:
        1. Always create a high-signal header chunk: Signature + Docstring.
        2. If the body is short (< max_tokens roughly ~ 1200 chars), create a 'full' chunk.
        3. If the body is large, create overlapping window chunks of the body.
        """
        chunks: list[CodeChunk] = []

        # 1. Signature + Docstring Chunk (highest semantic signal for query matching)
        header_parts = []
        if symbol.signature:
            header_parts.append(symbol.signature)
        if symbol.docstring:
            header_parts.append(f'"""\n{symbol.docstring}\n"""')

        header_content = "\n".join(header_parts).strip()
        if header_content:
            chunks.append(
                CodeChunk(
                    symbol_name=symbol.name,
                    file_path=symbol.file_path,
                    chunk_type="signature_doc",
                    content=header_content,
                    start_line=symbol.start_line,
                    end_line=symbol.start_line + len(header_content.splitlines()),
                    chunk_index=0,
                )
            )

        # If there is no body or body is very short, we are done
        body_text = (symbol.body or "").strip()
        if not body_text:
            return chunks

        # Estimate tokens (~4 chars per token for code)
        estimated_tokens = len(body_text) // 4

        if estimated_tokens <= self.max_tokens:
            # Short body -> Single full chunk
            chunks.append(
                CodeChunk(
                    symbol_name=symbol.name,
                    file_path=symbol.file_path,
                    chunk_type="full",
                    content=body_text,
                    start_line=symbol.start_line,
                    end_line=symbol.end_line,
                    chunk_index=1 if chunks else 0,
                )
            )
        else:
            # Large body -> Break into overlapping line windows with context prefix
            lines = body_text.splitlines()
            # Approx 25 lines per chunk
            lines_per_chunk = 25
            stride = max(5, lines_per_chunk - self.chunk_overlap_lines)

            chunk_idx = len(chunks)
            for i in range(0, len(lines), stride):
                chunk_lines = lines[i : i + lines_per_chunk]
                if not chunk_lines:
                    break

                chunk_content = f"{symbol.signature}\n" + "\n".join(chunk_lines)
                actual_start = symbol.start_line + i
                actual_end = min(symbol.end_line, symbol.start_line + i + len(chunk_lines))

                chunks.append(
                    CodeChunk(
                        symbol_name=symbol.name,
                        file_path=symbol.file_path,
                        chunk_type="body_chunk",
                        content=chunk_content,
                        start_line=actual_start,
                        end_line=actual_end,
                        chunk_index=chunk_idx,
                    )
                )
                chunk_idx += 1

                if i + lines_per_chunk >= len(lines):
                    break

        return chunks
