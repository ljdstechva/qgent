"""Trailing-question detection: the prose questions QGIS must surface."""
from __future__ import annotations

import pytest

from agent.question_detect import detect_question


def test_yes_no_offer_at_the_end_of_a_report():
    found = detect_question(
        "Created **site_buffer_100m** (1 feature, EPSG:32651).\n\n"
        "Would you like me to export it to a GeoPackage as well?")
    assert found["kind"] == "yes_no"
    assert found["options"] == ["Yes", "No"]
    assert found["question"].startswith("Would you like me to export")


def test_numbered_options_after_a_question():
    found = detect_question(
        "I found three candidate layers.\n\n"
        "Which one should I buffer?\n\n"
        "1. **roads_2024** — 1,204 lines\n"
        "2. **rivers** — 88 lines\n"
        "3. barangay_boundaries\n\n"
        "Reply with the number or the name.")
    assert found["kind"] == "choice"
    assert found["options"] == ["roads_2024 — 1,204 lines", "rivers — 88 lines",
                                "barangay_boundaries"]
    assert found["question"] == "Which one should I buffer?"


def test_list_before_a_which_question_supplies_the_options():
    found = detect_question(
        "Layers in the project:\n\n- roads\n- rivers\n\n"
        "Which of these should I clip to the site boundary?")
    assert found["options"] == ["roads", "rivers"]


def test_colon_lead_in_with_choice_words():
    found = detect_question(
        "Please choose a page size for the template:\n"
        "- A4 landscape\n- A3 landscape\n- A3 portrait")
    assert found["options"] == ["A4 landscape", "A3 landscape", "A3 portrait"]


@pytest.mark.parametrize("question, options", [
    ("Should I export it as PDF or PNG?", ["PDF", "PNG"]),
    ("Do you want A3, A4, or Letter?", ["A3", "A4", "Letter"]),
    ("Should I clip it or buffer it?", ["clip it", "buffer it"]),
])
def test_short_or_alternatives_become_buttons(question, options):
    found = detect_question("Done.\n\n" + question)
    assert found["kind"] == "choice"
    assert found["options"] == options


def test_open_question_gets_a_reply_box_only():
    found = detect_question("What title should appear in the title block?")
    assert found["kind"] == "open"
    assert found["options"] == []
    assert found["allow_other"] is True


def test_long_option_is_cut_at_its_description():
    item = ("**Esri World Imagery** — satellite basemap that shows the real "
            "site context, best for vicinity maps and ECC annexes")
    found = detect_question("Which basemap?\n\n1. " + item + "\n2. OSM")
    assert found["options"][0] == "Esri World Imagery"


@pytest.mark.parametrize("text", [
    "",
    "Buffer complete: 1 feature, EPSG:32651.",
    # A question mid-reply followed by more statements is not trailing.
    "Should I use UTM? I used EPSG:32651 because the site is in Luzon.",
    # Pleasantries are not questions QGent should surface.
    "Done — layer added.\n\nAnything else?",
    "Is there anything else you need?",
    # Code or a table at the end hides any earlier question.
    "Should I run this?\n\n```python\nprint('x')\n```",
    "Which layer?\n\n| a | b |\n|---|---|",
    # A heading ending in '?' is structure, not a question.
    "## Why EPSG:32651?",
    "See https://example.com/tiles?x=1 for the source.",
])
def test_ignores_non_questions(text):
    assert detect_question(text) is None


def test_long_list_yields_a_reply_box_not_partial_buttons():
    items = "\n".join(f"{n}. layer_{n}" for n in range(1, 11))
    found = detect_question("Which layer should I style?\n\n" + items)
    assert found["options"] == []
    assert found["kind"] == "open"


def test_options_are_deduplicated_and_clipped():
    found = detect_question(
        "Which one?\n- Same\n- same\n- " + "x" * 200)
    assert [len(option) <= 80 for option in found["options"]] == [True, True]
    assert found["options"][0] == "Same"


def test_lead_in_words_do_not_hide_a_yes_no_question():
    found = detect_question("Great — should I also add a legend?")
    assert found["options"] == ["Yes", "No"]
