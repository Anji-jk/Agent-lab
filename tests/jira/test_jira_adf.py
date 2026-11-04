"""Jira's description format (ADF) read as plain text."""

from orchestrator.jira.adf import adf_to_text


def doc(*content):
    return {"type": "doc", "version": 1, "content": list(content)}


def text(value, *marks):
    node = {"type": "text", "text": value}
    if marks:
        node["marks"] = list(marks)
    return node


def para(*parts):
    return {"type": "paragraph", "content": list(parts)}


def bullets(*items):
    return {"type": "bulletList", "content": [{"type": "listItem", "content": list(item)}
                                              for item in items]}


def test_nothing_gives_an_empty_text():
    assert adf_to_text(None) == ""
    assert adf_to_text({"type": "doc", "content": []}) == ""


def test_a_plain_string_is_kept():
    assert adf_to_text("  As an Admin, I want...  ") == "As an Admin, I want..."


def test_paragraphs_and_headings():
    out = adf_to_text(doc({"type": "heading", "attrs": {"level": 3}, "content": [text("Goal")]},
                          para(text("As an "), text("Admin", {"type": "strong"}),
                               text(", I want to enrol employees."))))
    assert out == "### Goal\n\nAs an Admin, I want to enrol employees."


def test_bullet_lists_nest():
    out = adf_to_text(doc(
        para(text("Acceptance Criteria:", {"type": "strong"})),
        bullets([para(text("Admins can search for employees."))],
                [para(text("No duplicate enrolment.")),
                 bullets([para(text("same user and batch"))])]),
    ))
    assert out == ("Acceptance Criteria:\n\n- Admins can search for employees.\n"
                   "- No duplicate enrolment.\n  - same user and batch")


def test_ordered_lists_keep_their_numbers():
    ordered = {"type": "orderedList", "attrs": {"order": 3},
               "content": [{"type": "listItem", "content": [para(text("third"))]},
                           {"type": "listItem", "content": [para(text("fourth"))]}]}
    assert adf_to_text(doc(ordered)) == "3. third\n4. fourth"


def test_links_mentions_breaks_and_code():
    out = adf_to_text(doc(
        para(text("see "), text("the spec", {"type": "link", "attrs": {"href": "https://x.y/s"}}),
             {"type": "hardBreak"}, {"type": "mention", "attrs": {"text": "@Anji"}}),
        {"type": "codeBlock", "content": [text("GET /batches/{id}")]},
    ))
    assert out == "see the spec (https://x.y/s)\n@Anji\n\n```\nGET /batches/{id}\n```"


def test_tables_become_rows():
    cell = lambda value: {"type": "tableCell", "content": [para(text(value))]}  # noqa: E731
    table = {"type": "table", "content": [
        {"type": "tableRow", "content": [cell("Status"), cell("Meaning")]},
        {"type": "tableRow", "content": [cell("Enrolled"), cell("added to the batch")]}]}
    assert adf_to_text(doc(table)) == "| Status | Meaning |\n| Enrolled | added to the batch |"


def test_unknown_nodes_keep_their_text():
    out = adf_to_text(doc({"type": "somethingNew", "content": [para(text("still here"))]},
                          {"type": "mediaSingle", "content": [{"type": "media"}]}))
    assert out == "still here"
