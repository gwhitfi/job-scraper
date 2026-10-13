import pytest

from jobscraper import userdata


def test_round_trip_and_fields(tmp_path):
    path = tmp_path / "marks.json"
    marks = userdata.load(path)
    assert marks == {}
    userdata.update(marks, "a:1", "starred", True)
    userdata.update(marks, "a:1", "note", "  call recruiter  ")
    userdata.save(path, marks)
    loaded = userdata.load(path)
    assert loaded["a:1"]["starred"] is True
    assert loaded["a:1"]["note"] == "call recruiter"


def test_applied_date_kept_until_cleared():
    marks = {}
    first = userdata.update(marks, "a:1", "applied", True)["applied_at"]
    assert userdata.update(marks, "a:1", "applied", True)["applied_at"] == first
    assert "applied_at" not in userdata.update(marks, "a:1", "applied", False)


@pytest.mark.parametrize(("field", "value"), [("bogus", True), ("starred", "yes"), ("note", 3)])
def test_rejects_bad_input(field, value):
    with pytest.raises(ValueError):
        userdata.update({}, "a:1", field, value)


def test_note_trimmed_to_max():
    entry = userdata.update({}, "a:1", "note", "x" * (userdata.MAX_NOTE + 10))
    assert len(entry["note"]) == userdata.MAX_NOTE
