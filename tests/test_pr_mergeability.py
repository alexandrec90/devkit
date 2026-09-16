"""Tests for the third answer GitHub gives about merging.

The property under all of them: `UNKNOWN` must never be read as "merges fine". It is what
GitHub says while it is still computing, and a merge to the base branch puts every open PR
under it back there at once -- which is the same moment the conflicts a caller is looking
for are being created. Both regressions this module was extracted after were that reading:
treating the answer as clean, and then asking exactly one more time and treating the second
one as clean.

Nothing here touches `gh`: `settle` takes the ask as a callable, so the asks and the waits
are both countable.
"""

import pytest
from support import load_script

mergeability = load_script("scripts/pr_mergeability.py")


def row(**fields) -> dict:
    """One open, unjudged PR as `gh pr list` returns it, overridden field by field."""
    base = {"number": 412, "isDraft": False, "state": "OPEN", "mergeable": "UNKNOWN"}
    base.update(fields)
    return base


def answering(*replies, asked=None):
    """An ask that answers `replies` in turn, recording the numbers it was asked about."""
    answers = iter(replies)

    def view(number: int) -> dict:
        if asked is not None:
            asked.append(number)
        return next(answers)

    return view


@pytest.mark.parametrize(
    "fields,conflict",
    [
        ({"mergeable": "CONFLICTING"}, True),
        ({"mergeable": "UNKNOWN", "mergeStateStatus": "DIRTY"}, True),
        ({"mergeable": "MERGEABLE"}, False),
        ({"mergeable": "UNKNOWN"}, False),
        ({"mergeable": None}, False),
        ({"mergeable": "UNKNOWN", "mergeStateStatus": "BEHIND"}, False),
    ],
)
def test_either_field_can_report_the_conflict(fields, conflict):
    """`mergeStateStatus: DIRTY` is a second signal rather than a nicer spelling: it
    reports the conflict an ask before `mergeable` stops saying `UNKNOWN`."""
    assert mergeability.conflicted(row(**fields)) is conflict


@pytest.mark.parametrize(
    "fields,worth_asking",
    [
        ({"mergeable": "UNKNOWN"}, True),
        ({"mergeable": None}, True),
        ({"mergeable": "MERGEABLE"}, False),
        ({"mergeable": "CONFLICTING"}, False),
        ({"mergeStateStatus": "DIRTY"}, False),
        ({"isDraft": True}, False),
        ({"state": "CLOSED"}, False),
        ({"state": "MERGED"}, False),
    ],
)
def test_only_a_row_github_might_still_judge_is_worth_an_ask(fields, worth_asking):
    """A settled verdict needs no ask, a draft is nobody's row, and a PR that has left
    the open set is one GitHub will never judge -- so asking about one would spend the
    whole budget, waits included, on an answer that cannot arrive."""
    assert bool(mergeability.unresolved([row(**fields)])) is worth_asking


def test_a_row_with_no_number_cannot_be_asked_about():
    assert mergeability.unresolved([{"mergeable": "UNKNOWN"}]) == []


def test_a_settled_page_asks_nobody_and_waits_for_nothing():
    waited = []

    def explode(_number):
        raise AssertionError("a settled row must not be asked about")

    mergeability.settle(explode, [row(mergeable="MERGEABLE")], waited.append)
    assert waited == []


def test_the_ask_is_repeated_until_the_verdict_arrives():
    """The ask is also what triggers the calculation, so its first answer is routinely
    `UNKNOWN` again. Stopping there is what drew three rows for five broken PRs, 42
    seconds after three merges to `main` conflicted two of the missing ones."""
    asked, waited = [], []
    view = answering({"mergeable": "UNKNOWN"}, {"mergeable": "CONFLICTING"}, asked=asked)
    entry = row()
    mergeability.settle(view, [entry], waited.append)
    assert asked == [412, 412]
    assert waited == [mergeability.WAIT]  # nothing waits before the first ask
    assert mergeability.conflicted(entry)


def test_a_verdict_that_never_arrives_costs_the_budget_and_no_more():
    """A caller is usually a person watching an empty quick-pick, so the bound is a
    number of asks rather than however long GitHub takes. The row then reads as it did
    before -- the one case left here that can be wrong, and the one that is bounded."""
    asked, waited = [], []
    view = answering(*[{"mergeable": "UNKNOWN"}] * mergeability.ASKS, asked=asked)
    entry = row()
    mergeability.settle(view, [entry], waited.append)
    assert len(asked) == mergeability.ASKS
    assert len(waited) == mergeability.ASKS - 1
    assert not mergeability.conflicted(entry)


def test_an_ask_that_failed_leaves_the_rest_of_the_row_alone():
    """A failed `gh` answers `{}`, and the row can be carrying a known check failure and
    an `updatedAt` that the single-PR query does not even return."""
    entry = row(statusCheckRollup=[{"conclusion": "FAILURE"}], updatedAt="2026-09-04T09:00:00Z")
    mergeability.settle(answering(*[{}] * mergeability.ASKS), [entry], lambda _seconds: None)
    assert entry["statusCheckRollup"] == [{"conclusion": "FAILURE"}]
    assert entry["updatedAt"] == "2026-09-04T09:00:00Z"


def test_only_the_rows_still_unjudged_are_asked_about_again():
    """The unresolved set is rebuilt between rounds, so a row that settles on the first
    ask is not asked a second time and a settled neighbour is never asked at all."""
    asked = []
    answers = {7: iter([{"mergeable": "UNKNOWN"}, {"mergeable": "CONFLICTING"}])}

    def view(number: int) -> dict:
        asked.append(number)
        return next(answers[number])

    entries = [row(number=7), row(number=8, mergeable="MERGEABLE")]
    mergeability.settle(view, entries, lambda _seconds: None)
    assert asked == [7, 7]
    assert entries[1]["mergeable"] == "MERGEABLE"
