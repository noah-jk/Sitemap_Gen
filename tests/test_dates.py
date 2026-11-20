from datetime import date

import pytest

from app import dates


@pytest.mark.parametrize("path,kind,expected", [
    ("/news/2016/keynote-speaker", "url_date", ("2016-01-01", "2016-12-31")),
    ("/news/2024/05/12/story", "url_date", ("2024-05-12", "2024-05-12")),
    ("/news/2024/5/", "url_date", ("2024-05-01", "2024-05-31")),
    ("/news/2024-05-12-story", "url_date", ("2024-05-12", "2024-05-12")),
    ("/news/20240512-story", "url_date", ("2024-05-12", "2024-05-12")),
    ("/news/fri-01062023-1124-am/president", "url_date", ("2023-01-06", "2023-01-06")),  # MMDDYYYY
    ("/news/item-12345678", "url_date", None),            # 8 digits that aren't a date
    ("/news/2013-honors-convocation", "url_date", None),
    ("/news/2013-honors-convocation", "url_year", ("2013-01-01", "2013-12-31")),
    ("/news/Whittier_College_%202011_Lecture", "url_year", ("2011-01-01", "2011-12-31")),
    ("/about/history", "url_year", None),
])
def test_dates_from_urls(path, kind, expected):
    found = dates.from_url(path, kind)
    assert (found and (found.start.isoformat(), found.end.isoformat())) == (expected or None)


@pytest.mark.parametrize("text,expected", [
    ("Sun, 07 Jun 2026 21:43:55 GMT", "2026-06-07"),
    ("2024-03-05T10:00:00Z", "2024-03-05"),
    ("03/05/2024", "2024-03-05"),
    ("March 5, 2024", "2024-03-05"),
    ("June 2020", "2020-06"),
    ("2021", "2021"),
    ("not a date", None),
    ("", None),
])
def test_dates_from_text(text, expected):
    found = dates.parse_text(text, "x")
    assert (found.text if found else None) == expected


def test_before_and_after_need_the_whole_period():
    year = dates.from_url("/news/2021-x", "url_year")
    assert year.before(date(2022, 1, 1)) and not year.before(date(2021, 6, 1))
    assert year.after(date(2020, 12, 31)) and not year.after(date(2021, 6, 1))


def test_sources_in_order_and_validation():
    row = {"url_path": "/news/story", "last_modified": "Sun, 07 Jun 2026 21:43:55 GMT",
           "crawl_extra": '{"Published 1": "2019-04-02"}'}
    assert dates.page_date(row, ["url_date", "crawl:Published 1", "last_modified"]).text == "2019-04-02"
    assert dates.page_date(row, ["url_date", "last_modified"]).source == "last_modified"
    assert dates.page_date(row, ["url_date"]) is None
    assert dates.clean_sources(["url_year", "crawl:Published 1", "url_year"]) == ["url_year", "crawl:Published 1"]
    with pytest.raises(ValueError):
        dates.clean_sources(["sql:drop"])
