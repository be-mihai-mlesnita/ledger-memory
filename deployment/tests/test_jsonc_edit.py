import json
import math
import traceback

import pytest

from deployment.jsonc_edit import JsoncError, loads, remove_value, set_value


CONFIG = r'''// global configuration
{
  "url": "https://example.test/a//b", // keep URL
  "quoted": "say \"{hello}\"", /* braces aren't syntax */
  "mcp": {
    "ledger": {"enabled": true, "command": "python",}, // ledger note
    "other": {"enabled": false}, /* unrelated */
  },
  "permission": {"ledger_*": "allow",},
  "instructions": ["C:\\Users\\Some User\\rules.md", "/home/user/my rules.md",],
} // end
'''


def test_realistic_load_and_exact_noop():
    value = loads(CONFIG)
    assert value["url"] == "https://example.test/a//b"
    assert value["quoted"] == 'say "{hello}"'
    assert value["instructions"][0] == r"C:\Users\Some User\rules.md"
    assert set_value(CONFIG, [], value) == CONFIG
    assert set_value(CONFIG, ["mcp", "ledger"], value["mcp"]["ledger"]) == CONFIG
    assert remove_value(CONFIG, ["absent", "child"]) == CONFIG


def test_replace_and_unedit_preserves_every_other_byte():
    changed = set_value(CONFIG, ["mcp", "ledger", "enabled"], False)
    assert changed == CONFIG.replace('"enabled": true', '"enabled": false')
    assert set_value(changed, ["mcp", "ledger", "enabled"], True) == CONFIG
    changed = set_value(CONFIG, ["permission", "ledger_*"], "ask")
    assert changed == CONFIG.replace('"allow"', '"ask"')


@pytest.mark.parametrize("old,new", [(True, 1), (1, True), (False, 0), (1, 1.0),
                                         ([True], [1]), ({"a": True}, {"a": 1})])
def test_noop_distinguishes_types(old, new):
    original = json.dumps({"a": old})
    changed = set_value(original, ["a"], new)
    assert changed != original
    assert type(loads(changed)["a"]) is type(new)


@pytest.mark.parametrize("trailing", ["", ","])
@pytest.mark.parametrize("index", [0, 1, 2])
def test_array_removal_positions(index, trailing):
    original = '[/* lead */ 10 /* a */, // one\n20 /* b */, 30 /* c */' + trailing + '\n]'
    changed = remove_value(original, [index])
    expected = [10, 20, 30]
    expected.pop(index)
    assert loads(changed) == expected
    for comment in ("/* lead */", "/* a */", "// one", "/* b */", "/* c */"):
        assert comment in changed


@pytest.mark.parametrize("trailing", ["", ","])
@pytest.mark.parametrize("key", ["a", "b", "c"])
def test_object_removal_positions(key, trailing):
    original = '{/* lead */ "a": 10 /* a */, // one\n"b": 20 /* b */, "c": 30 /* c */' + trailing + '\n}'
    changed = remove_value(original, [key])
    expected = {"a": 10, "b": 20, "c": 30}
    del expected[key]
    assert loads(changed) == expected
    for comment in ("/* lead */", "/* a */", "// one", "/* b */", "/* c */"):
        assert comment in changed


@pytest.mark.parametrize("original,path,expected", [
    ('[/*before*/ 1 /*after*/]', [0], []),
    ('[/*before*/ 1 /*after*/, // tail\n]', [0], []),
    ('{/*before*/ "a": 1 /*after*/}', ["a"], {}),
    ('{/*before*/ "a": 1 /*after*/, // tail\n}', ["a"], {}),
])
def test_only_member_removal(original, path, expected):
    changed = remove_value(original, path)
    assert loads(changed) == expected
    assert "/*before*/" in changed and "/*after*/" in changed


@pytest.mark.parametrize("original,path,expected", [
    ('{"a":1 // tail\n}', ["b"], {"a": 1, "b": 2}),
    ('{"a":1, // tail\n}', ["b"], {"a": 1, "b": 2}),
    ('[1 // tail\n]', [1], [1, 2]),
    ('[1, // tail\n]', [1], [1, 2]),
    ('{/* tail */}', ["b"], {"b": 2}),
    ('[/* tail */]', [0], [2]),
    ('{}', ["b"], {"b": 2}),
    ('[]', [0], [2]),
    ('{"a":1}', ["b"], {"a": 1, "b": 2}),
    ('[1]', [1], [1, 2]),
    ('{"a":1,}', ["b"], {"a": 1, "b": 2}),
    ('[1,]', [1], [1, 2]),
    ('{"a":1/* tail */}', ["b"], {"a": 1, "b": 2}),
    ('[1,/* tail */]', [1], [1, 2]),
])
def test_append_after_comments(original, path, expected):
    changed = set_value(original, path, 2)
    assert loads(changed) == expected
    if "tail" in original:
        assert "tail" in changed
    if ", // tail" in original or ",/* tail" in original:
        assert changed.rstrip().endswith(("2,\n}", "2,\n]", "2,]"))


def test_nested_creation_and_array_append():
    changed = set_value(CONFIG, ["mcp", "new", "options", "enabled"], True)
    assert loads(changed)["mcp"]["new"] == {"options": {"enabled": True}}
    paths = [r"C:\Users\New User\rules.md", "/home/\u00e9l\u00e8ve/my rules.md"]
    changed = set_value(CONFIG, ["instructions"], paths)
    assert loads(changed)["instructions"] == paths
    assert set_value(changed, ["instructions"], paths) == changed
    assert loads(set_value("[]", [0, "nested", "key"], 4)) == [{"nested": {"key": 4}}]


def test_crlf_indent_and_bom():
    original = '\ufeff{\r\n\t"a": 1, // keep\r\n}\r\n'
    changed = set_value(original, ["b"], 2)
    assert changed == '\ufeff{\r\n\t"a": 1, // keep\r\n\t"b": 2,\r\n}\r\n'
    assert loads(changed) == {"a": 1, "b": 2}
    assert set_value(original, ["a"], 1) == original
    with pytest.raises(JsoncError):
        loads(" \ufeff{}")


@pytest.mark.parametrize("original,path", [
    ('{"a":1}', ["a", "b"]), ('{"a":null}', ["a", "b"]),
    ('{"a":true}', ["a", 0]), ('[]', ["0"]), ('{}', [0]),
    ('[]', [-1]), ('[]', [True]), ('{}', [None]), ('{}', "a"),
])
def test_invalid_paths_and_scalar_parents(original, path):
    for edit in (lambda: set_value(original, path, 1), lambda: remove_value(original, path)):
        with pytest.raises(JsoncError):
            edit()


def test_missing_and_out_of_bounds():
    assert remove_value("[]", [5]) == "[]"
    assert remove_value(CONFIG, ["instructions", 50]) == CONFIG
    for original, path in [('[]', [1]), ('{}', ["missing", 0]), ('[]', [0, 0])]:
        with pytest.raises(JsoncError):
            set_value(original, path, 1)
    with pytest.raises(JsoncError):
        remove_value("{}", [])


@pytest.mark.parametrize("text", [
    '', ' ', '// only', '/* unterminated', '{', '[', '{,}', '[,]', '[1,,]',
    '{"a":1,,}', '{"a":1 "b":2}', '{"a" 1}', '{a:1}', '[1 2]', '[1}',
    '{"a":1,"a":2}', '{"a":1,"\\u0061":2}', '{"x":{"a":1,"a":2}}',
    'NaN', 'Infinity', '-Infinity', '1e999', '-1e999', '[NaN]', '{"x":Infinity}',
    '01', '+1', '.1', '1.', '1e', 'true false', '{} trailing', '"bad\\q"',
    '"unterminated', '"line\nbreak"', '{} /* unterminated', '\ufeff\ufeff{}',
])
def test_invalid_syntax(text):
    for operation in (lambda: loads(text), lambda: set_value(text, ["x"], 1),
                      lambda: remove_value(text, ["missing"])):
        with pytest.raises(JsoncError, match=r"^Invalid JSONC or edit at offset \d+$"):
            operation()


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, {1: "x"}, (1,), object(), [math.nan]])
def test_invalid_replacement(value):
    with pytest.raises(JsoncError):
        set_value("{}", ["key"], value)


def test_circular_value_and_deep_input_errors_are_safe():
    value = []
    value.append(value)
    with pytest.raises(JsoncError):
        set_value("{}", ["x"], value)
    with pytest.raises(JsoncError):
        loads("[" * 2000 + "]" * 2000)


@pytest.mark.parametrize("text", ['{"TOP_SECRET":}', '{"TOP_SECRET":1,"TOP_SECRET":2}',
                                '"TOP_SECRET\\q"', '/* TOP_SECRET'])
def test_errors_never_echo_secrets(text):
    try:
        loads(text)
    except JsoncError as error:
        assert "TOP_SECRET" not in str(error)
        assert "TOP_SECRET" not in repr(error)
        assert "TOP_SECRET" not in "".join(traceback.format_exception_only(error))
        assert error.__suppress_context__
    else:
        pytest.fail("Expected safe parsing failure")


@pytest.mark.parametrize("text,value", [('null', None), ('true', True), ('-1.25e2', -125.0),
                                      ('"//{}[]/* */"', '//{}[]/* */')])
def test_general_roots(text, value):
    assert loads(text) == value
    assert loads(set_value(text, [], {"root": []})) == {"root": []}


@pytest.mark.parametrize("object_mode", [False, True])
@pytest.mark.parametrize("count", range(5))
@pytest.mark.parametrize("trailing", [False, True])
@pytest.mark.parametrize("trivia", ["", " ", "/* keep */", " // keep\n  "])
def test_container_edit_matrix(object_mode, count, trailing, trivia):
    entries = [f'"k{i}": {i}' if object_mode else str(i) for i in range(count)]
    inside = ("," + trivia).join(entries)
    if entries and trailing:
        inside += ","
    original = ("{" if object_mode else "[") + trivia + inside + trivia + ("}" if object_mode else "]")
    expected = {f"k{i}": i for i in range(count)} if object_mode else list(range(count))
    assert loads(original) == expected
    key = f"k{count}" if object_mode else count
    appended = set_value(original, [key], count)
    after_append = dict(expected) if object_mode else list(expected)
    if object_mode:
        after_append[key] = count
    else:
        after_append.append(count)
    assert loads(appended) == after_append
    assert loads(remove_value(appended, [key])) == expected
    assert appended.count("keep") == original.count("keep")
    for index in range(count):
        key = f"k{index}" if object_mode else index
        removed = remove_value(original, [key])
        after_remove = dict(expected) if object_mode else list(expected)
        del after_remove[key]
        assert loads(removed) == after_remove
        assert removed.count("keep") == original.count("keep")
