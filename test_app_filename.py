import app


def test_build_download_filename_uses_search_and_date():
    app.LAST_SEARCH_QUERY = "Business in Ikeja"
    filename = app._build_download_filename("csv")
    assert filename == "business in ikeja 2026-08-18.csv"


def test_build_download_filename_falls_back_when_no_search():
    app.LAST_SEARCH_QUERY = ""
    filename = app._build_download_filename("pdf")
    assert filename.startswith("search results ")
    assert filename.endswith(".pdf")
