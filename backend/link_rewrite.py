"""Rewrite inter-note links when notes are renamed or moved.

Pure string transforms (no disk, no index singleton) so they can be unit
tested in isolation. `utils.move_note` / `move_folder` wrap these with the
file I/O and index refresh.

Two directions are handled:
  * rewrite_source_content       — a note that LINKS TO a moved note; repoints
                                   the link to the moved note's new location.
  * rewrite_moved_note_own_links — the MOVED note's own note-relative markdown
                                   links to other (unmoved) notes, whose
                                   relative paths change because the source
                                   moved.

Links inside fenced code blocks and inline code are never touched.
"""

from __future__ import annotations

import posixpath
import re
import urllib.parse
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

# [[target]] or [[target|display]] — target has no pipe/bracket/newline.
_WIKILINK_RE = re.compile(r'\[\[([^\]|\n]+)(?:\|([^\]\n]+))?\]\]')
# [text](path) excluding external/anchor/data links, mirroring note_index.MDLINK_RE.
_MDLINK_RE = re.compile(r'\[([^\]]+)\]\((?!https?://|mailto:|#|data:)([^)]+)\)')
_FENCE_RE = re.compile(r'^\s*(`{3,}|~{3,})')
_INLINE_CODE_RE = re.compile(r'`[^`]*`')


def folder_of(path: str) -> str:
    """Vault-relative folder of a note path ('' for root)."""
    return posixpath.dirname(path)


def _strip_md(path: str) -> str:
    return path[:-3] if path.endswith('.md') else path


def _split_anchor(href: str) -> Tuple[str, str]:
    if '#' in href:
        core, frag = href.split('#', 1)
        return core, '#' + frag
    return href, ''


def _transform_outside_code(content: str, transform) -> str:
    """Apply `transform` to text outside fenced code blocks and inline code."""
    lines = content.split('\n')
    out = []
    in_fence = False
    fence_char = ''
    fence_len = 0
    for line in lines:
        m = _FENCE_RE.match(line)
        if m:
            marker = m.group(1)
            if not in_fence:
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                in_fence = False
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        out.append(_transform_line(line, transform))
    return '\n'.join(out)


def _transform_line(line: str, transform) -> str:
    parts = []
    idx = 0
    for m in _INLINE_CODE_RE.finditer(line):
        parts.append(transform(line[idx:m.start()]))
        parts.append(m.group(0))
        idx = m.end()
    parts.append(transform(line[idx:]))
    return ''.join(parts)


def _new_wikilink_target(
    original: str,
    new_path: str,
    source_folder: str,
    resolver_after,
    name_counts_after: Dict[str, int],
) -> str:
    """New text for the inside of [[...]] after the target note moved."""
    new_no_ext = _strip_md(new_path)
    new_stem = Path(new_path).stem
    # Path-style links stay path-style.
    if '/' in original.strip():
        return new_no_ext
    # Bare name: keep it bare only when it unambiguously points at new_path.
    if name_counts_after.get(new_stem.lower(), 0) <= 1:
        return new_stem
    if resolver_after is not None and resolver_after.resolve_wikilink(new_stem, source_folder) == new_path:
        return new_stem
    return new_no_ext


def _new_mdlink_href(original_href: str, new_path: str, source_folder: str) -> str:
    """New href for a markdown link after the target note moved, preserving the
    original link's style (root-relative / note-relative / encoding / .md / #anchor)."""
    core, anchor = _split_anchor(original_href)
    was_encoded = '%' in core
    decoded = urllib.parse.unquote(core)
    had_md = decoded.endswith('.md')
    is_root = decoded.startswith('/') and not decoded.startswith('//')

    target = new_path if had_md else _strip_md(new_path)
    if is_root:
        new_rel = '/' + target
    else:
        base = source_folder if source_folder else '.'
        new_rel = posixpath.relpath(target, base)
        if decoded.startswith('./') and not new_rel.startswith('.'):
            new_rel = './' + new_rel
    if was_encoded:
        new_rel = urllib.parse.quote(new_rel, safe='/._-')
    return new_rel + anchor


def rewrite_source_content(
    content: str,
    source_folder: str,
    moved_map: Dict[str, str],
    resolver_before,
    resolver_after,
    name_counts_after: Dict[str, int],
) -> Tuple[str, int]:
    """Repoint links in one source note that resolve to any moved note.

    `moved_map` maps old note path -> new note path. Returns (new_content,
    number_of_links_rewritten).
    """
    changes = [0]

    def transform(seg: str) -> str:
        def wl(m: 're.Match[str]') -> str:
            target = m.group(1).strip()
            display = m.group(2)
            resolved = resolver_before.resolve_wikilink(target, source_folder)
            if resolved in moved_map:
                new_target = _new_wikilink_target(
                    target, moved_map[resolved], source_folder, resolver_after, name_counts_after
                )
                changes[0] += 1
                return f'[[{new_target}|{display}]]' if display is not None else f'[[{new_target}]]'
            return m.group(0)

        def md(m: 're.Match[str]') -> str:
            text, href = m.group(1), m.group(2)
            resolved = resolver_before.resolve_mdlink(href, source_folder)
            if resolved in moved_map:
                changes[0] += 1
                return f'[{text}]({_new_mdlink_href(href, moved_map[resolved], source_folder)})'
            return m.group(0)

        seg = _WIKILINK_RE.sub(wl, seg)
        seg = _MDLINK_RE.sub(md, seg)
        return seg

    return _transform_outside_code(content, transform), changes[0]


def rewrite_moved_note_own_links(
    content: str,
    old_folder: str,
    new_folder: str,
    resolver_before,
    moved_old_set: Set[str],
) -> Tuple[str, int]:
    """Recompute the moved note's own note-relative markdown links to targets
    that did NOT move, so they keep pointing at the same notes from the new
    location. Wikilinks (resolved by stem) and root-relative links are left
    as-is. Returns (new_content, number_of_links_rewritten)."""
    if old_folder == new_folder:
        return content, 0
    changes = [0]

    def transform(seg: str) -> str:
        def md(m: 're.Match[str]') -> str:
            text, href = m.group(1), m.group(2)
            core, anchor = _split_anchor(href)
            decoded = urllib.parse.unquote(core)
            if decoded.startswith('/'):
                return m.group(0)
            target = resolver_before.resolve_mdlink(core, old_folder)
            if not target or target in moved_old_set:
                return m.group(0)
            was_encoded = '%' in core
            had_md = decoded.endswith('.md')
            tgt = target if had_md else _strip_md(target)
            base = new_folder if new_folder else '.'
            new_rel = posixpath.relpath(tgt, base)
            if decoded.startswith('./') and not new_rel.startswith('.'):
                new_rel = './' + new_rel
            if was_encoded:
                new_rel = urllib.parse.quote(new_rel, safe='/._-')
            changes[0] += 1
            return f'[{text}]({new_rel}{anchor})'

        return _MDLINK_RE.sub(md, seg)

    return _transform_outside_code(content, transform), changes[0]
