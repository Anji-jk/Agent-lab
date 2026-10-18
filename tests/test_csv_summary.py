import pytest

from csv_summary import summarize_csv


def test_summarizes_valid_csv(tmp_path):
    csv_file = tmp_path / "sample.csv"
    csv_file.write_text(
        "name,age\nAmit,25\nPriya,\n",
        encoding="utf-8",
    )

    result = summarize_csv(csv_file)

    assert result["rows"] == 2
    assert result["columns"] == 2
    assert result["column_names"] == ["name", "age"]
    assert result["missing_values"] == {
        "name": 0,
        "age": 1,
    }


def test_counts_whitespace_as_missing(tmp_path):
    csv_file = tmp_path / "spaces.csv"
    csv_file.write_text(
        "name,city\nAmit,   \n",
        encoding="utf-8",
    )

    result = summarize_csv(csv_file)

    assert result["missing_values"]["city"] == 1


def test_rejects_empty_file(tmp_path):
    csv_file = tmp_path / "empty.csv"
    csv_file.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="empty"):
        summarize_csv(csv_file)


def test_reports_missing_file(tmp_path):
    csv_file = tmp_path / "missing.csv"

    with pytest.raises(ValueError, match="Could not read"):
        summarize_csv(csv_file)
