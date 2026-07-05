"""Centralized prompt loader for info2action AI prompts.

Loads prompt templates from prompts/ directory .md files.
Content after the first '---' separator is used as the prompt template.
Falls back to built-in defaults if file is missing.
"""

import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")


def load_prompt(filename, **kwargs):
    """Load a prompt template from prompts/ directory and apply variable substitution.

    Args:
        filename: e.g. '02_summary_breakdown.md'
        **kwargs: template variables to substitute (e.g. keywords='...')

    Returns:
        The prompt string with variables substituted, or None if file not found.
    """
    filepath = os.path.join(PROMPTS_DIR, filename)
    if not os.path.exists(filepath):
        return None

    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # Extract content after the first '---' separator
    parts = content.split('\n---\n', 1)
    if len(parts) == 2:
        prompt = parts[1].strip()
    else:
        # No separator, use full content
        prompt = content.strip()

    # Apply variable substitution if any kwargs provided
    if kwargs:
        for key, value in kwargs.items():
            prompt = prompt.replace('{' + key + '}', str(value))

    return prompt
