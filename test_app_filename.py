import time
import app


def test_build_download_filename_uses_search_and_date():
    app.LAST_SEARCH_QUERY = "Business in Ikeja"
    filename = app._build_download_filename("csv")
    expected = f"business in ikeja {time.strftime('%Y-%m-%d')}.csv"
    assert filename == expected


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
        "call_status": "",
    }]

    response = app.download_doc()
    assert response.mimetype == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    content = response.get_data()
    assert b"Call Status" in content
    assert b"Acme Mart" in content
