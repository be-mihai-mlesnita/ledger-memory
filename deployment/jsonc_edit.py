"""Dependency-free JSONC span editing. A single leading BOM is accepted.

Paths use string object keys and nonnegative integer array indices. Missing
parents are objects only; removing the root is not supported. Errors contain
only a generic description and an offset, never configuration contents.
"""

import json
import math
from dataclasses import dataclass, field


class JsoncError(Exception):
    """Invalid JSONC, path, or replacement value."""


def _fail(offset=0):
    raise JsoncError(f"Invalid JSONC or edit at offset {offset}") from None


@dataclass
class _Node:
    start: int
    end: int
    value: object
    members: list = field(default_factory=list)


@dataclass
class _Member:
    key: object
    start: int
    node: _Node
    comma: int | None = None


class _Parser:
    def __init__(self, text):
        if not isinstance(text, str):
            _fail()
        self.text = text
        self.pos = int(text.startswith("\ufeff"))
        self.decoder = json.JSONDecoder(parse_constant=lambda _: _fail(self.pos))

    def trivia(self):
        text = self.text
        while self.pos < len(text):
            if text[self.pos] in " \t\r\n":
                self.pos += 1
            elif text.startswith("//", self.pos):
                self.pos += 2
                while self.pos < len(text) and text[self.pos] not in "\r\n":
                    self.pos += 1
            elif text.startswith("/*", self.pos):
                end = text.find("*/", self.pos + 2)
                if end < 0:
                    _fail(self.pos)
                self.pos = end + 2
            else:
                break

    def decode(self):
        try:
            value, end = self.decoder.raw_decode(self.text, self.pos)
        except (ValueError, RecursionError):
            _fail(self.pos)
        if isinstance(value, float) and not math.isfinite(value):
            _fail(self.pos)
        self.pos = end
        return value

    def node(self):
        self.trivia()
        start = self.pos
        token = self.text[start:start + 1]
        if not token:
            _fail(start)
        if token not in ("{", "["):
            value = self.decode()
            return _Node(start, self.pos, value)
        is_object = token == "{"
        close = "}" if is_object else "]"
        result = _Node(start, start, {} if is_object else [])
        self.pos += 1
        self.trivia()
        while self.text[self.pos:self.pos + 1] != close:
            member_start = self.pos
            key = len(result.members)
            if is_object:
                if self.text[self.pos:self.pos + 1] != '"':
                    _fail(self.pos)
                key = self.decode()
                if key in result.value:
                    _fail(member_start)
                self.trivia()
                if self.text[self.pos:self.pos + 1] != ":":
                    _fail(self.pos)
                self.pos += 1
            child = self.node()
            member = _Member(key, member_start, child)
            result.members.append(member)
            if is_object:
                result.value[key] = child.value
            else:
                result.value.append(child.value)
            self.trivia()
            if self.text[self.pos:self.pos + 1] != ",":
                break
            member.comma = self.pos
            self.pos += 1
            self.trivia()
        if self.text[self.pos:self.pos + 1] != close:
            _fail(self.pos)
        self.pos += 1
        result.end = self.pos
        return result

    def parse(self):
        try:
            root = self.node()
        except RecursionError:
            _fail(self.pos)
        self.trivia()
        if self.pos != len(self.text):
            _fail(self.pos)
        return root


def loads(text: str):
    """Decode JSON with comments and trailing commas; reject duplicate keys."""
    return _Parser(text).parse().value


def _dump(value):
    def check(item, ancestors):
        if type(item) in (dict, list):
            if id(item) in ancestors:
                _fail()
            ancestors = ancestors | {id(item)}
            if type(item) is dict and any(type(k) is not str for k in item):
                _fail()
            for child in item.values() if type(item) is dict else item:
                check(child, ancestors)
        elif type(item) not in (str, int, float, bool, type(None)):
            _fail()
    try:
        check(value, set())
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, OverflowError, RecursionError):
        _fail()


def _same(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same(v, right[k]) for k, v in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def _path(path):
    if type(path) is not list or any(
        type(k) not in (str, int) or (type(k) is int and k < 0) for k in path
    ):
        _fail()


def _member(node, key):
    if isinstance(node.value, dict) and type(key) is str:
        return next((m for m in node.members if m.key == key), None)
    if isinstance(node.value, list) and type(key) is int:
        return node.members[key] if key < len(node.members) else None
    _fail(node.start)


def _splice(text, edits):
    # Coincident insertions run in reverse registration order, too.
    for start, end, replacement in reversed(sorted(edits, key=lambda edit: edit[0])):
        text = text[:start] + replacement + text[end:]
    return text


def _append(text, node, key, encoded):
    close = node.end - 1
    entry = (_dump(key) + ": " if isinstance(node.value, dict) else "") + encoded
    edits = []
    trailing = bool(node.members and node.members[-1].comma is not None)
    if node.members and not trailing:
        end = node.members[-1].node.end
        edits.append((end, end, ","))
    if trailing:
        entry += ","
    line_start = text.rfind("\n", node.start, close) + 1
    if line_start > node.start and not text[line_start:close].strip():
        indent = text[line_start:close] + "  "
        if node.members:
            first = node.members[0].start
            first_line = text.rfind("\n", node.start, first) + 1
            if first_line > node.start and not text[first_line:first].strip():
                indent = text[first_line:first]
        newline = "\r\n" if "\r\n" in text[node.start:close] else "\n"
        edits.append((line_start, line_start, indent + entry + newline))
    else:
        edits.append((close, close, (" " if close > node.start + 1 else "") + entry))
    return _splice(text, edits)


def set_value(text: str, path: list[str | int], value) -> str:
    """Replace or append a value, creating only missing object parents."""
    node = _Parser(text).parse()
    _path(path)
    encoded = _dump(value)
    for index, key in enumerate(path):
        member = _member(node, key)
        if member is None:
            if isinstance(node.value, list) and key != len(node.members):
                _fail(node.start)
            for parent in reversed(path[index + 1:]):
                if type(parent) is not str:
                    _fail(node.start)
                encoded = "{" + _dump(parent) + ": " + encoded + "}"
            return _append(text, node, key, encoded)
        node = member.node
    if _same(node.value, value):
        return text
    return text[:node.start] + encoded + text[node.end:]


def remove_value(text: str, path: list[str | int]) -> str:
    """Remove a member/element; absent paths are unchanged. Root removal fails."""
    node = _Parser(text).parse()
    _path(path)
    if not path:
        _fail(node.start)
    for index, key in enumerate(path):
        member = _member(node, key)
        if member is None:
            return text
        if index == len(path) - 1:
            edits = [(member.start, member.node.end, "")]
            comma = member.comma
            if comma is None and len(node.members) > 1:
                comma = node.members[-2].comma
            if comma is not None:
                edits.append((comma, comma + 1, ""))
            return _splice(text, edits)
        node = member.node
    return text
