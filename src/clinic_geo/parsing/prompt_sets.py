"""Load prompt sets using the current numbered-runner semantics."""

from __future__ import annotations

import json
from pathlib import Path


def make_prompt_entry(item, default_prefix, index, category_name=None):
    """Return `(prompt_id, text, metadata)` for one prompt item."""
    if isinstance(item, dict):
        text = item.get("text", "")
        prompt_id = item.get("id")
        if prompt_id is None:
            prompt_id = f"{category_name or default_prefix}_{index:03d}"
        metadata = {k: v for k, v in item.items() if k not in ("id", "text")}
        return (str(prompt_id), text, metadata)
    return (f"{default_prefix}_{index:03d}", str(item), {})


def load_prompts(path: Path):
    """Load prompts from a file or directory, mirroring `1_run_gpt_prompts.py`."""
    path = Path(path)
    if path.is_dir():
        files = sorted(list(path.glob("*.txt")) + list(path.glob("*.md")) + list(path.glob("*.json")))
        prompts = []
        for file in files:
            if file.suffix == ".json":
                try:
                    data = json.load(file.open(encoding="utf-8"))
                    if isinstance(data, list):
                        for i, item in enumerate(data, start=1):
                            prompts.append(make_prompt_entry(item, file.stem, i, None))
                    elif isinstance(data, dict):
                        if "categories" in data:
                            for cat in data["categories"]:
                                cat_name = cat.get("category_name", "")
                                for i, prompt in enumerate(cat.get("prompts", []), start=1):
                                    prompts.append(make_prompt_entry(prompt, file.stem, i, cat_name))
                        elif "prompts" in data and isinstance(data["prompts"], list):
                            for i, item in enumerate(data["prompts"], start=1):
                                prompts.append(make_prompt_entry(item, file.stem, i, None))
                except Exception as e:
                    print(f"Warning: Could not parse {file}: {e}")
            else:
                text = file.read_text(encoding="utf-8").strip()
                if text:
                    prompts.append((file.stem, text, {}))
        return prompts

    if path.is_file():
        if path.suffix == ".json":
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
            prompts = []
            if isinstance(data, list):
                return [make_prompt_entry(item, path.stem, i, None) for i, item in enumerate(data, start=1)]
            elif isinstance(data, dict):
                if "categories" in data:
                    for cat in data["categories"]:
                        cat_name = cat.get("category_name", "")
                        for i, prompt in enumerate(cat.get("prompts", []), start=1):
                            prompts.append(make_prompt_entry(prompt, path.stem, i, cat_name))
                elif "prompts" in data and isinstance(data["prompts"], list):
                    return [make_prompt_entry(item, path.stem, i, None) for i, item in enumerate(data["prompts"], start=1)]
            return prompts

        text = path.read_text(encoding="utf-8").strip()
        if "---PROMPT---" in text:
            parts = [p.strip() for p in text.split("---PROMPT---") if p.strip()]
            return [(f"prompt_{i:03d}", p, {}) for i, p in enumerate(parts, start=1)]
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return [(f"prompt_{i:03d}", line, {}) for i, line in enumerate(lines, start=1)]

    raise FileNotFoundError(f"Prompts path not found: {path}")
