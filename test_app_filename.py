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


def test_download_doc_has_word_compatible_headers_and_note_column():
    app.LAST_SEARCH_QUERY = "Business in Ikeja"
    app.LAST_RESULTS = [{
        "name": "Acme Mart",
        "phone": "08012345678",
        "address": "Ikeja, Lagos",
        "website": "https://example.com",
        "email": "hello@example.com",
        "all_emails": "hello@example.com",
        "website_phone": "08099999999",
        "rating": "4.8",
        "maps_url": "https://maps.google.com/123",
    }]

    response = app.download_doc()
    assert response.mimetype == "application/msword"
    content = response.get_data(as_text=True)
    assert "Call Status" in content
    assert "Acme Mart" in content
