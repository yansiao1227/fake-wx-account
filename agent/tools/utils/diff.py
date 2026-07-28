"""
Diff tools for file editing
Provides fuzzy matching and diff generation functionality
"""

import difflib
import re
from typing import Optional, Tuple


def strip_bom(text: str) -> Tuple[str, str]:
    """
    Remove BOM (Byte Order Mark)
    
    :param text: Original text
    :return: (BOM, text after removing BOM)
    """
    if text.startswith('\ufeff'):
        return '\ufeff', text[1:]
    return '', text


def detect_line_ending(text: str) -> str:
    """
    Detect line ending type
    
    :param text: Text content
    :return: Line ending type ('\r\n' or '\n')
    """
    if '\r\n' in text:
        return '\r\n'
    return '\n'


def normalize_to_lf(text: str) -> str:
    """
    Normalize all line endings to LF (\n)
    
    :param text: Original text
    :return: Normalized text
    """
    return text.replace('\r\n', '\n').replace('\r', '\n')


def restore_line_endings(text: str, original_ending: str) -> str:
    """
    Restore original line endings
    
    :param text: LF normalized text
    :param original_ending: Original line ending
    :return: Text with restored line endings
    """
    if original_ending == '\r\n':
        return text.replace('\n', '\r\n')
    return text


def normalize_for_fuzzy_match(text: str) -> str:
    """
    Normalize text for fuzzy matching
    Remove excess whitespace but preserve basic structure
    
    :param text: Original text
    :return: Normalized text
    """
    # Compress multiple spaces to one
    text = re.sub(r'[ \t]+', ' ', text)
    # Remove trailing spaces
    text = re.sub(r' +\n', '\n', text)
    # Remove leading spaces (but preserve indentation structure, only remove excess)
    lines = text.split('\n')
    normalized_lines = []
    for line in lines:
        # Preserve indentation but normalize to multiples of single spaces
        stripped = line.lstrip()
        if stripped:
            indent_count = len(line) - len(stripped)
            # Normalize indentation (convert tabs to spaces)
            normalized_indent = ' ' * indent_count
            normalized_lines.append(normalized_indent + stripped)
        else:
            normalized_lines.append('')
    return '\n'.join(normalized_lines)


class FuzzyMatchResult:
    """Fuzzy match result"""
    
    def __init__(self, found: bool, index: int = -1, match_length: int = 0, content_for_replacement: str = ""):
        self.found = found
        self.index = index
        self.match_length = match_length
        self.content_for_replacement = content_for_replacement


def _build_fuzzy_pattern(old_text: str) -> Optional[str]:
    """
    Build the whitespace-flexible regex used to locate ``old_text`` fuzzily.

    Returns ``None`` when ``old_text`` has no non-whitespace content to match.
    This is the single source of truth for fuzzy matching, so that *finding* a
    match (:func:`fuzzy_find_text`) and *counting* occurrences
    (:func:`count_matches`) always use the exact same rules.
    """
    stripped = old_text.strip('\n')
    if not stripped.strip():
        return None

    source_lines = stripped.split('\n')
    line_patterns = []
    for i, line in enumerate(source_lines):
        tokens = line.split()
        if not tokens:
            line_patterns.append(r'[ \t]*')
            continue
        # Tolerate any run of blanks between tokens.
        core = r'[ \t]+'.join(re.escape(tok) for tok in tokens)
        # First-line leading whitespace is folded into the match only when
        # old_text itself was indented here; otherwise it stays OUTSIDE the
        # match so a no-indent old_text preserves (does not swallow and drop)
        # the file's existing indentation -- mirroring an exact substring
        # match. Inner lines always tolerate indentation: it sits inside the
        # matched region and is re-supplied by new_text.
        if i > 0 or line[:1] in (' ', '\t'):
            core = r'[ \t]*' + core
        line_patterns.append(core + r'[ \t]*')
    return '\n'.join(line_patterns)


def fuzzy_find_text(content: str, old_text: str) -> FuzzyMatchResult:
    """
    Find text in content, try exact match first, then fuzzy match
    
    :param content: Content to search in
    :param old_text: Text to find
    :return: Match result
    """
    # First try exact match
    index = content.find(old_text)
    if index != -1:
        return FuzzyMatchResult(
            found=True,
            index=index,
            match_length=len(old_text),
            content_for_replacement=content
        )

    # Fuzzy match: the exact substring was not found, most likely because the
    # whitespace differs (indentation, spaces around operators, trailing
    # spaces). Locate the region in the ORIGINAL content using a
    # whitespace-flexible pattern and return offsets into that original
    # content.
    #
    # This must NOT replace inside a whitespace-normalized copy of the file:
    # doing so previously returned the normalized copy as
    # content_for_replacement, which caused the whole file to be rewritten
    # with collapsed indentation (every untouched line got reformatted).
    pattern = _build_fuzzy_pattern(old_text)
    if pattern is not None:
        match = re.search(pattern, content)
        if match:
            return FuzzyMatchResult(
                found=True,
                index=match.start(),
                match_length=match.end() - match.start(),
                content_for_replacement=content
            )

    # Not found
    return FuzzyMatchResult(found=False)


def count_matches(content: str, old_text: str) -> int:
    """
    Count occurrences of ``old_text`` using the SAME strategy as
    :func:`fuzzy_find_text`: an exact substring when one is present, otherwise
    the whitespace-flexible fuzzy regex.

    The edit tool's uniqueness guard must agree with the matcher that actually
    performs the replacement. Counting through a separate normalization pass
    (the previous approach) could disagree with the regex used to locate and
    replace, so both paths now share :func:`_build_fuzzy_pattern`.
    """
    if not old_text:
        return 0
    # Mirror fuzzy_find_text: prefer exact matching when it applies.
    if content.find(old_text) != -1:
        return content.count(old_text)
    pattern = _build_fuzzy_pattern(old_text)
    if pattern is None:
        return 0
    return len(re.findall(pattern, content))


def generate_diff_string(old_content: str, new_content: str) -> dict:
    """
    Generate unified diff string
    
    :param old_content: Old content
    :param new_content: New content
    :return: Dictionary containing diff and first changed line number
    """
    old_lines = old_content.split('\n')
    new_lines = new_content.split('\n')
    
    # Generate unified diff
    diff_lines = list(difflib.unified_diff(
        old_lines,
        new_lines,
        lineterm='',
        fromfile='original',
        tofile='modified'
    ))
    
    # Find first changed line number
    first_changed_line = None
    for line in diff_lines:
        if line.startswith('@@'):
            # Parse @@ -1,3 +1,3 @@ format
            match = re.search(r'@@ -\d+,?\d* \+(\d+)', line)
            if match:
                first_changed_line = int(match.group(1))
                break
    
    diff_string = '\n'.join(diff_lines)
    
    return {
        'diff': diff_string,
        'first_changed_line': first_changed_line
    }
