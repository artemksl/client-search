"""Persistence helpers for current prompt-answer output contracts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from clinic_geo.parsing.answer_files import merge_sort_key, parse_answer_markdown


def safe_filename(name: str):
    """Mirror `chat_engine.safe_filename` exactly."""
    name = re.sub(r"[^a-zA-Z0-9_-]+", "_", name)
    return name[:80] or "prompt"


def answer_filename(index: int, name: str) -> str:
    return f"{index:03d}_{safe_filename(name)}.md"


def render_answer_markdown(name, prompt, metadata, answer) -> str:
    """Render the exact Markdown shape currently written by `chat_engine.write_output`."""
    parts = [f"# Prompt ID: {name}", "", "## Prompt", "", prompt, ""]
    if metadata:
        if "citations" in metadata:
            citation_value = metadata["citations"]
        elif "citation" in metadata:
            citation_value = metadata["citation"]
        elif "source" in metadata:
            citation_value = metadata["source"]
        else:
            citation_value = metadata
        parts.extend([
            "## Citations",
            "",
            json.dumps(citation_value, indent=2) if not isinstance(citation_value, str) else str(citation_value),
            "",
        ])
    parts.extend(["## Answer", "", answer])
    return "\n".join(parts) + "\n"


def write_answer_file(output_dir, index, name, prompt, metadata, answer):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / answer_filename(index, name)
    output_file.write_text(render_answer_markdown(name, prompt, metadata, answer), encoding="utf-8")
    return output_file


@dataclass(frozen=True)
class MergeResult:
    files_seen: int
    merged: dict
    empty_ids: list
    duplicate_ids: list
    no_marker: list


def merge_answer_files(raw_answers_dir, include_empty=False) -> MergeResult:
    """Merge `*.md` answer files into the current `{id: answer}` object in memory."""
    raw_answers_dir = Path(raw_answers_dir)
    if not raw_answers_dir.exists():
        raise FileNotFoundError(f"Input dir not found: {raw_answers_dir}")
    files = sorted(raw_answers_dir.glob("*.md"), key=merge_sort_key)
    if not files:
        raise RuntimeError(f"No .md files found in {raw_answers_dir}")

    merged = {}
    empty_ids, duplicate_ids, no_marker = [], [], []
    for f in files:
        text = f.read_text(encoding="utf-8")
        parsed = parse_answer_markdown(text, f.stem)
        prompt_id, answer = parsed.prompt_id, parsed.answer

        if prompt_id in merged:
            duplicate_ids.append((prompt_id, f.name))
            continue
        if not answer:
            empty_ids.append((prompt_id, f.name))
            if not include_empty:
                continue
        if not parsed.has_prompt_id_header:
            no_marker.append(f.name)
        merged[prompt_id] = answer

    return MergeResult(
        files_seen=len(files),
        merged=merged,
        empty_ids=empty_ids,
        duplicate_ids=duplicate_ids,
        no_marker=no_marker,
    )


def write_merged_responses(path, merged):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def merge_responses_to_file(raw_answers_dir, merged_file, include_empty=False) -> MergeResult:
    result = merge_answer_files(raw_answers_dir, include_empty=include_empty)
    write_merged_responses(merged_file, result.merged)
    return result
